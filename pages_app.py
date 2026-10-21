"""
頁面模組 — 庫存、進退貨登錄、盤點、品項管理、數據分析、叫貨出表、系統設定
v2: per-clinic 廠牌/啟用、整數數量、庫存日期欄、盤點歷史可編輯、進退貨可修改刪除
"""

import io
import math
import hashlib
import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from collections import defaultdict
from pypinyin import pinyin, Style
from database import get_supabase_client, fetch_all, with_retry, NETWORK_ERRORS
import html as _html


# ══════════════════════════════════════════════
#  注音首碼搜尋（鍵盤對應版）
# ══════════════════════════════════════════════

BOPOMOFO_TO_KEY = {
    "ㄅ": "1", "ㄆ": "q", "ㄇ": "a", "ㄈ": "z",
    "ㄉ": "2", "ㄊ": "w", "ㄋ": "s", "ㄌ": "x",
    "ㄍ": "e", "ㄎ": "d", "ㄏ": "c",
    "ㄐ": "r", "ㄑ": "f", "ㄒ": "v",
    "ㄓ": "5", "ㄔ": "t", "ㄕ": "g", "ㄖ": "b",
    "ㄗ": "y", "ㄘ": "h", "ㄙ": "n",
    "ㄧ": "u", "ㄨ": "j", "ㄩ": "m",
    "ㄚ": "8", "ㄛ": "i", "ㄜ": "k", "ㄝ": ",",
    "ㄞ": "9", "ㄟ": "o", "ㄠ": "l", "ㄡ": ".",
    "ㄢ": "0", "ㄣ": "p", "ㄤ": ";", "ㄥ": "/",
    "ㄦ": "-",
}


def get_keyboard_initials(text: str) -> str:
    result = []
    for char_bpmf in pinyin(text, style=Style.BOPOMOFO):
        bpmf = char_bpmf[0]
        if bpmf and bpmf[0] in BOPOMOFO_TO_KEY:
            result.append(BOPOMOFO_TO_KEY[bpmf[0]])
    return "".join(result)


def get_bopomofo_sort_key(name: str) -> str:
    result = []
    for char_bpmf in pinyin(name, style=Style.BOPOMOFO):
        result.append(char_bpmf[0])
    return "".join(result)


def sort_products_by_bopomofo(products: list, cs_map: dict | None = None) -> list:
    """排序：分類 → 首字注音 → 櫃位（cs_map 提供時，無櫃位排到後面）→ 全字注音。
    cs_map: {product_id: {"cabinet": "C1" 或 None}}。不傳則跳過櫃位排序層（澤豐情境）。"""
    def sort_key(p):
        cat_id = p.get("category_id", 0)
        first_bpmf = get_bopomofo_initial(p["name"])
        full_bpmf = get_bopomofo_sort_key(p["name"])
        if cs_map is not None:
            cs = cs_map.get(p["id"], {}) or {}
            cabinet = (cs.get("cabinet") or "").strip()
            # 沒櫃位的排在同首字組的最後
            cabinet_key = cabinet if cabinet else "￿"
            return (cat_id, first_bpmf, cabinet_key, full_bpmf)
        return (cat_id, first_bpmf, full_bpmf)
    return sorted(products, key=sort_key)


@st.cache_data(ttl=300)
def build_bopomofo_index(product_names: tuple) -> dict:
    index = {}
    for name in product_names:
        keys = get_keyboard_initials(name)
        stripped = name
        for prefix in ("特.", "特級"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break
        keys_stripped = get_keyboard_initials(stripped) if stripped != name else ""
        index[name] = (keys, keys_stripped)
    return index


def match_search(name: str, key_index: tuple, search_text: str) -> bool:
    search_lower = search_text.lower().strip()
    if not search_lower:
        return True
    if search_lower in name.lower():
        return True
    keys_full, keys_stripped = key_index
    if search_lower in keys_full:
        return True
    if keys_stripped and search_lower in keys_stripped:
        return True
    return False


def get_bopomofo_initial(name: str) -> str:
    """取得品項名稱第一個字的注音聲母，如 '葛根' -> 'ㄍ'"""
    for prefix in ("特.", "特級"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    bpmf_list = pinyin(name, style=Style.BOPOMOFO)
    if bpmf_list and bpmf_list[0]:
        first = bpmf_list[0][0]
        return first[0] if first else ""
    return ""


def short_date(d: str) -> str:
    """'2026-03-27' -> '03/27'"""
    return d[5:7] + "/" + d[8:10] if d and len(d) >= 10 else d or "-"


# 顯示時 0 視為空白的欄位（活動量類，不含庫存盤點數）
ZERO_HIDE_PATTERNS = ("進(", "耗(", "進貨", "耗用")
ZERO_HIDE_EXACT = {"建議叫貨", "叫貨", "平均耗用", "建議數量", "叫貨數量"}
# 以整數顯示的欄位（建議叫貨為四捨五入後的整數）
INT_DISPLAY_COLS = {"建議叫貨", "建議數量"}


def _should_hide_zero(col_name) -> bool:
    if not isinstance(col_name, str):
        return False
    if col_name in ZERO_HIDE_EXACT:
        return True
    return any(col_name.startswith(p) for p in ZERO_HIDE_PATTERNS)


def hide_zeros_in_cols(df: pd.DataFrame, cols=None) -> pd.DataFrame:
    """把指定欄位的 0 換成 None（顯示為空白）。cols=None 時自動偵測活動量欄位。"""
    df = df.copy()
    if cols is None:
        cols = [c for c in df.columns if _should_hide_zero(c)]
    for c in cols:
        if c in df.columns:
            df[c] = df[c].apply(lambda v: None if pd.notna(v) and v == 0 else v)
    return df


def style_banded(df, highlight_col=None, bold_cols=None, big_bold_col=None):
    """每 5 行加粗底線分隔；highlight_col 加重點色；bold_cols 粗體；big_bold_col 大字粗體。
    數字欄位 0 自動隱藏（活動量類）。"""
    bold_cols = set(bold_cols or [])
    hide_zero_cols = {c for c in df.columns if _should_hide_zero(c)}

    def row_style(row):
        idx = row.name if isinstance(row.name, int) else 0
        border = "border-bottom: 2.5px solid #6A5ACD" if (idx + 1) % 5 == 0 else ""
        styles = [border] * len(row)
        for ci, col in enumerate(df.columns):
            extra = []
            if col in bold_cols:
                extra.append("font-weight: bold")
            if col == big_bold_col:
                extra.append("font-weight: bold; font-size: 1.15em; color: #6A5ACD")
            if col == highlight_col:
                extra.append("background-color: #E8E0F0; font-weight: bold; border-left: 3px solid #6A5ACD")
            if extra:
                base = styles[ci]
                styles[ci] = "; ".join([base] + extra) if base else "; ".join(extra)
        return styles

    num_cols = df.select_dtypes(include=["number", "float", "int"]).columns.tolist()
    fmt = {}
    for c in num_cols:
        dec = 0 if c in INT_DISPLAY_COLS else 1
        if c in hide_zero_cols:
            fmt[c] = lambda v, d=dec: "" if pd.isna(v) or v == 0 else f"{v:.{d}f}"
        else:
            fmt[c] = lambda v, d=dec: "" if pd.isna(v) else f"{v:.{d}f}"
    return df.style.apply(row_style, axis=1).format(fmt)


# ══════════════════════════════════════════════
#  共用資料載入函式
# ══════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_products():
    data = with_retry(lambda: get_supabase_client().table("products").select(
        "id, name, category_id, unit_id, spec_note, "
        "categories(name), units(name)"
    ).execute().data)
    return sort_products_by_bopomofo(data)

@st.cache_data(ttl=30)
def load_brands():
    return with_retry(lambda: get_supabase_client().table("brands").select("id, name").order("id").execute().data)

# migration_v8 新增：依櫃位 A/B/C/D 分別設定的倍數 / 係數
CATEGORY_V8_COLS = ("by_cabinet", "multiplier_a", "multiplier_b", "multiplier_c", "multiplier_d",
                    "safety_a", "safety_b", "safety_c", "safety_d")


@st.cache_data(ttl=30)
def load_categories():
    base_cols = ("id, name, default_unit_id, default_spec_note, "
                 "stock_target_multiplier, safety_factor")

    def _query(cols):
        return with_retry(lambda: get_supabase_client().table("categories").select(cols).order("id").execute().data)

    try:
        return _query(base_cols + ", " + ", ".join(CATEGORY_V8_COLS))
    except Exception:
        # migration_v8_cabinet_params.sql 尚未執行（沒有依櫃位欄位）→ 不帶 v8 欄位，計算時一律不分櫃位
        return _query(base_cols)


def categories_v8_missing(cats: list) -> bool:
    """True = 尚未執行 migration_v8（分類列沒有 by_cabinet 欄位）。"""
    return bool(cats) and all("by_cabinet" not in c for c in cats)

@st.cache_data(ttl=30)
def load_units():
    return with_retry(lambda: get_supabase_client().table("units").select("id, name").order("id").execute().data)

@st.cache_data(ttl=3600, show_spinner=False)
def get_clinic_id(clinic_name: str) -> int | None:
    resp = with_retry(lambda: get_supabase_client().table("clinics").select("id").eq("name", clinic_name).execute())
    return resp.data[0]["id"] if resp.data else None


def recalc_stock(product_id: int, clinic_id: int):
    """重算即時庫存 = 最後一次盤點數量 + 該盤點日期之後的進貨（依 tx_date）"""
    sb = get_supabase_client()
    # 找最後一次盤點（同日多筆時以 id 大者為準＝最後寫入的那筆，避免順序隨機）
    last_log = sb.table("inventory_logs").select(
        "current_count_qty, log_date"
    ).eq("product_id", product_id).eq(
        "clinic_id", clinic_id
    ).order("log_date", desc=True).order("id", desc=True).limit(1).execute().data

    if last_log:
        base_qty = float(last_log[0]["current_count_qty"])
        last_date = last_log[0]["log_date"]
    else:
        base_qty = 0
        last_date = "1900-01-01"

    # 加上最後盤點日期之後的進貨
    tx_after = sb.table("transactions").select(
        "change_qty"
    ).eq("product_id", product_id).eq(
        "clinic_id", clinic_id
    ).gt("tx_date", last_date).execute().data
    restock = sum(float(t["change_qty"]) for t in tx_after)

    new_stock = round(base_qty + restock, 1)
    sb.table("clinic_stock").update(
        {"current_stock": new_stock}
    ).eq("product_id", product_id).eq("clinic_id", clinic_id).execute()
    invalidate_stock_cache()
    return new_stock


def recalc_consumed_for_product(product_id: int, clinic_id: int):
    """重算單一品項在指定診所的所有 inventory_logs 的 last/restock/consumed，並更新 clinic_stock。

    規則：
    - 第一次盤點：last=0, restock=0, consumed=0（沒基準）
    - 後續盤點：last=上一筆 current_count，restock=兩筆之間 transactions 加總，consumed=last+restock-current
    """
    sb = get_supabase_client()
    logs = sb.table("inventory_logs").select(
        "id, log_date, current_count_qty"
    ).eq("product_id", product_id).eq(
        "clinic_id", clinic_id
    ).order("log_date", desc=False).order("id", desc=False).execute().data

    if not logs:
        recalc_stock(product_id, clinic_id)
        return

    txs = sb.table("transactions").select(
        "change_qty, tx_date"
    ).eq("product_id", product_id).eq(
        "clinic_id", clinic_id
    ).execute().data

    prev_log_date = None
    prev_count = 0.0

    for log in logs:
        log_date = log["log_date"]
        current = float(log["current_count_qty"]) if log["current_count_qty"] is not None else 0.0

        if prev_log_date is None:
            last, restock, consumed = 0.0, 0.0, 0.0
        else:
            last = prev_count
            restock = sum(
                float(t["change_qty"]) for t in txs
                if prev_log_date < t["tx_date"] <= log_date
            )
            consumed = last + restock - current

        sb.table("inventory_logs").update({
            "last_count_qty": round(last, 1),
            "restock_qty_since_last": round(restock, 1),
            "consumed_qty": round(consumed, 1),
        }).eq("id", log["id"]).execute()

        prev_log_date = log_date
        prev_count = current

    recalc_stock(product_id, clinic_id)


def recalc_all_consumed_in_clinic(clinic_id: int) -> int:
    """重算指定診所所有品項的 inventory_logs。回傳處理品項數。"""
    sb = get_supabase_client()
    pids = fetch_all(sb.table("inventory_logs").select(
        "product_id"
    ).eq("clinic_id", clinic_id))
    pid_set = {p["product_id"] for p in pids}
    for pid in pid_set:
        recalc_consumed_for_product(int(pid), int(clinic_id))
    return len(pid_set)


def calc_avg_consumption(consumed_list_recent_first: list, max_n: int = 6) -> float:
    """近 max_n 次耗用平均：負數當 0，加總 / 實際筆數（最多 max_n）。
    consumed_list_recent_first: 由新到舊排序的耗用值清單。"""
    if not consumed_list_recent_first:
        return 0.0
    recent = consumed_list_recent_first[:max_n]
    total = sum(max(0.0, float(v)) for v in recent)
    return total / len(recent)


def build_recent_consumed_map(logs_desc: list, n: int = 6, window_start: str | None = None) -> dict:
    """logs_desc 已按 log_date desc 排序。回傳 {pid: [近 n 筆 consumed_qty 由新到舊]}。
    重要：排除每品項最早的那一筆 — 首次盤點的 consumed=0 是人為 placeholder（沒基準），不應計入平均。

    window_start：logs_desc 若只抓了「近 K 次盤點日期」（見 load_recent_logs），傳入該視窗最早的日期。
      品項若在 window_start 當天有盤點，代表它更早就存在（診所每次盤點都盤全部品項），
      其視窗內最早一筆是真實數據，不排除；反之（最早一筆晚於 window_start）就是首盤 placeholder，排除。"""
    by_pid_full = defaultdict(list)
    for log in logs_desc:
        by_pid_full[log["product_id"]].append(log)

    result = {}
    for pid, plogs in by_pid_full.items():
        # plogs 已是 desc，最後一筆是最早的
        earliest = plogs[-1]
        has_older_history = window_start is not None and earliest["log_date"] <= window_start
        if has_older_history:
            non_first = plogs
        else:
            non_first = plogs[:-1] if len(plogs) > 1 else []
        recent = non_first[:n]
        result[pid] = [
            float(log.get("consumed_qty") or 0)
            for log in recent
        ]
    return result


# ── 參數 ──
# 全域參數（system_settings，migration_v8 起）：平均耗用計算次數、異常耗用警示閾值
DEFAULT_AVG_WINDOW = 6
DEFAULT_THRESHOLD = 1.5
# 分類參數預設值（不分櫃位）：目標庫存備貨倍數、建議叫貨安全係數
DEFAULT_CAT_PARAMS = {"multiplier": 2.0, "safety": 1.2}
# 櫃位群組：取櫃位第一個字母；空白或非 A–D 開頭一律比照 B
CABINET_GROUPS = ("A", "B", "C", "D")
DEFAULT_CABINET_GROUP = "B"

# 庫存頁顯示的盤點次數（盤/進/耗 欄）
MAX_DISPLAY_DATES = 6


def cabinet_group(cabinet) -> str:
    """櫃位 → 群組字母。A、A2、A70 → A；空白或非 A–D 開頭 → B（不改動櫃位欄位本身）。"""
    s = (cabinet or "").strip().upper()
    return s[0] if s and s[0] in CABINET_GROUPS else DEFAULT_CABINET_GROUP


@st.cache_data(ttl=30)
def load_global_params() -> dict:
    """全域參數：avg_n=平均耗用計算次數（近 N 次盤點）、threshold=異常耗用警示閾值（倍數）。
    存於 system_settings（key/value）；缺值套預設。"""
    rows = with_retry(lambda: get_supabase_client().table("system_settings").select("key, value").execute().data)
    kv = {r["key"]: r["value"] for r in rows}

    def _get(key, default, cast):
        try:
            return cast(kv[key])
        except (KeyError, TypeError, ValueError):
            return default

    return {
        "avg_n": max(1, _get("avg_window", DEFAULT_AVG_WINDOW, lambda v: int(float(v)))),
        "threshold": _get("anomaly_threshold", DEFAULT_THRESHOLD, float),
    }


def save_global_params(avg_n: int, threshold: float) -> None:
    sb = get_supabase_client()
    sb.table("system_settings").upsert([
        {"key": "avg_window", "value": str(int(avg_n)), "description": "平均耗用計算次數（近 N 次盤點）"},
        {"key": "anomaly_threshold", "value": str(float(threshold)), "description": "異常耗用警示閾值（倍數）"},
    ]).execute()
    load_global_params.clear()


def build_category_params(categories: list) -> dict:
    """{分類名稱: {"multiplier","safety","by_cabinet","multiplier_cab":{A..D},"safety_cab":{A..D}}}；缺值套預設。
    multiplier=目標庫存備貨倍數、safety=建議叫貨安全係數（不分櫃位值）；
    by_cabinet=True 時改用 *_cab 依櫃位群組取值（migration_v8）。"""
    def _f(v, d):
        return float(v) if v not in (None, "") else d
    out = {}
    for c in categories:
        m = _f(c.get("stock_target_multiplier"), DEFAULT_CAT_PARAMS["multiplier"])
        s = _f(c.get("safety_factor"), DEFAULT_CAT_PARAMS["safety"])
        out[c["name"]] = {
            "multiplier": m,
            "safety": s,
            "by_cabinet": bool(c.get("by_cabinet")),
            "multiplier_cab": {g: _f(c.get(f"multiplier_{g.lower()}"), m) for g in CABINET_GROUPS},
            "safety_cab": {g: _f(c.get(f"safety_{g.lower()}"), s) for g in CABINET_GROUPS},
        }
    return out


def effective_params(cat_params: dict, cat_name: str, cabinet) -> tuple[float, float]:
    """回傳該品項適用的 (目標庫存備貨倍數, 建議叫貨安全係數)：分類開啟「依櫃位」時依櫃位群組取值。"""
    p = cat_params.get(cat_name)
    if not p:
        return DEFAULT_CAT_PARAMS["multiplier"], DEFAULT_CAT_PARAMS["safety"]
    if p["by_cabinet"]:
        g = cabinet_group(cabinet)
        return p["multiplier_cab"][g], p["safety_cab"][g]
    return p["multiplier"], p["safety"]


def suggest_order_qty(avg_c: float, current: float, multiplier: float, safety: float) -> int:
    """建議叫貨數量（整數）。
    啟動條件：平均耗用 > 0 且 目前庫存 < 平均耗用 × 安全係數；
    數量 = 四捨五入(平均耗用 × 備貨倍數 − 目前庫存)；結果 ≤ 0（例如 0.3 捨去成 0）就不建議，回傳 0。"""
    if avg_c <= 0 or current >= avg_c * safety:
        return 0
    qty = math.floor(avg_c * multiplier - current + 0.5)
    return int(qty) if qty > 0 else 0


def avg_window_dates(avg_n: int) -> int:
    """需要抓取的盤點日期數：max(顯示次數, 平均耗用計算次數 + 1)。
    +1 是因為每品項最早一筆（首盤 placeholder）會被排除，要多留一筆才湊得滿 N 次。"""
    return max(MAX_DISPLAY_DATES, int(avg_n) + 1)


# ── 庫存頁資料快取 ──
# Why: 每次 rerun（改字級、搜尋、換分類）都重打約 10 次 Supabase 查詢，而且偶爾單一請求會卡住數秒，
#      是庫存頁「每個動作都要等」的主因。資料改以 cache_data 保存 10 分鐘，
#      任何寫入（盤點/進退貨/改錯/品項設定）後呼叫 invalidate_stock_cache() 立即失效。

@st.cache_data(ttl=600, show_spinner="讀取庫存資料中…")
def load_stock_snapshot(clinic_id: int) -> tuple[list, list]:
    """(clinic_stock 列, products 列)。"""
    def _do():
        sb = get_supabase_client()
        cs_data = sb.table("clinic_stock").select(
            "product_id, current_stock, brand1_id, brand2_id, is_active, cabinet"
        ).eq("clinic_id", clinic_id).execute().data
        products = sb.table("products").select(
            "id, name, category_id, categories(name), units(name)"
        ).execute().data
        return cs_data, products
    return with_retry(_do)


@st.cache_data(ttl=600, show_spinner="讀取盤點紀錄中…")
def load_recent_logs(clinic_id: int, k_dates: int, columns: str) -> tuple[list, str | None]:
    """只抓最近 k_dates 個盤點日期的 inventory_logs（log_date desc, id desc）。
    回傳 (logs, window_start)；window_start=None 表示盤點日期數不足 k_dates，已含全部歷史。
    同日多筆時以 id 大者為新，確保 build_recent_consumed_map 取到最新一筆。"""
    def _do():
        sb = get_supabase_client()
        sess = sb.table("inventory_sessions").select("session_date").eq("clinic_id", clinic_id).execute().data
        dates_desc = sorted({s["session_date"] for s in sess}, reverse=True)
        window_start = dates_desc[k_dates - 1] if len(dates_desc) >= k_dates else None
        q = sb.table("inventory_logs").select(columns).eq("clinic_id", clinic_id)
        if window_start:
            q = q.gte("log_date", window_start)
        logs = fetch_all(q.order("log_date", desc=True).order("id", desc=True))
        return logs, window_start
    return with_retry(_do)


@st.cache_data(ttl=600, show_spinner=False)
def load_transactions_since(clinic_id: int, since: str | None) -> list:
    """進退貨紀錄（tx_date asc）；since 給定時只抓該日（含）之後。"""
    def _do():
        q = get_supabase_client().table("transactions").select("product_id, change_qty, tx_date").eq("clinic_id", clinic_id)
        if since:
            q = q.gte("tx_date", since)
        return fetch_all(q.order("tx_date"))
    return with_retry(_do)


def invalidate_stock_cache():
    """清除庫存頁資料快取。任何改變庫存/盤點/進退貨/品項設定的寫入後呼叫。"""
    for fn in (load_stock_snapshot, load_recent_logs, load_transactions_since):
        fn.clear()


# ══════════════════════════════════════════════
#  廠牌縮寫
# ══════════════════════════════════════════════
BRAND_ABBR = {"莊松榮": "松", "港香蘭": "港", "天一": "一", "科達": "科", "順天堂": "順", "仙豐": "仙"}

def abbr_brand(name):
    return BRAND_ABBR.get(name, name) if name and name != "-" else ""


# ══════════════════════════════════════════════
#  庫存表 Excel 產生器
# ══════════════════════════════════════════════

def _build_stock_excel(df, clinic_name, recent_dates, product_log_map, tx_by_product, cs_map, brand_map):
    """產生庫存表 Excel — 列印格式：注音 → 品項 → 櫃 → 廠 → 3 次歷史盤點(舊→新) → 進貨欄。
    df 必須含 _pid 欄位。recent_dates: 最近 3 個盤點日期，舊→新。
    每分類一頁，注音分組 + 雙線分隔，全格線，A4 直式，C 開頭櫃位紅色標示。"""
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl import Workbook

    thin = Side(style="thin")
    double = Side(style="double")
    border_thin = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_bold_font = Font(bold=True, size=13)
    header_norm_font = Font(size=13)  # 進貨/耗用 header 用普通字以視覺區隔
    data_font = Font(size=13)
    data_bold_font = Font(bold=True, size=13)  # 盤點欄資料粗體
    cat_title_font = Font(bold=True, size=14)
    red_font = Font(size=13, bold=True, color="C00000")
    red_fill = PatternFill("solid", fgColor="FFE5E5")
    center = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")

    # 欄位定義 — col_specs: [(header, source_key, width)]
    # 順序：注音、品項、櫃、廠1、廠2、(日期、進貨、耗用)×(N-1)、最末日期、進貨(到今)、耗用(留空供手算)
    col_specs = [
        ("注", "_initial", 4),
        ("品項", "name", 16),
        ("櫃", "cabinet", 5),
        ("廠1", "b1", 4),
        ("廠2", "b2", 4),
    ]
    for i, d in enumerate(recent_dates):
        sd = short_date(d)
        col_specs.append((sd, ("inv", d), 6.8))
        if i < len(recent_dates) - 1:
            d_next = recent_dates[i + 1]
            col_specs.append(("進貨", ("restock", d, d_next), 6.8))
            col_specs.append(("耗用", ("consume", d, d_next), 6.8))
    if recent_dates:
        # 最末盤點後：進貨(到今) + 耗用(空欄供系統異常時手算)
        col_specs.append(("進貨", ("restock_now", recent_dates[-1]), 6.8))
        col_specs.append(("耗用", ("consume_now", recent_dates[-1]), 6.8))

    MERGE_CATS = {"水藥材", "高貴藥材", "非健保藥材"}

    def _cell_value(pid, p_name, src, initial):
        cs = cs_map.get(pid, {}) or {}
        p_logs = product_log_map.get(pid, {})
        if src == "_initial":
            return initial
        if src == "name":
            return p_name
        if src == "cabinet":
            return cs.get("cabinet") or ""
        if src == "b1":
            return abbr_brand(brand_map.get(cs.get("brand1_id"), "-"))
        if src == "b2":
            return abbr_brand(brand_map.get(cs.get("brand2_id"), "-"))
        if isinstance(src, tuple):
            t = src[0]
            if t == "inv":
                lg = p_logs.get(src[1])
                if lg and lg.get("current_count_qty") is not None:
                    return round(float(lg["current_count_qty"]), 1)
                return ""
            if t == "restock":
                d_a, d_b = src[1], src[2]
                v = sum(float(tx["change_qty"]) for tx in tx_by_product.get(pid, [])
                        if d_a < tx["tx_date"] <= d_b)
                return round(v, 1) if v else ""
            if t == "consume":
                d_a, d_b = src[1], src[2]
                lg_a = p_logs.get(d_a)
                lg_b = p_logs.get(d_b)
                if lg_a and lg_b:
                    restock = sum(float(tx["change_qty"]) for tx in tx_by_product.get(pid, [])
                                  if d_a < tx["tx_date"] <= d_b)
                    v = round(float(lg_a["current_count_qty"]) + restock - float(lg_b["current_count_qty"]), 1)
                    return v if v else ""
                return ""
            if t == "restock_now":
                v = sum(float(tx["change_qty"]) for tx in tx_by_product.get(pid, [])
                        if tx["tx_date"] > src[1])
                return round(v, 1) if v else ""
            if t == "consume_now":
                # 系統無法計算（需下次盤點才知），保留空白供手算
                return ""
        return ""

    def _setup_page(ws):
        ws.page_setup.orientation = "portrait"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "1:1"
        ws.page_margins.left = 0.3
        ws.page_margins.right = 0.3
        ws.page_margins.top = 0.4
        ws.page_margins.bottom = 0.4
        ws.page_margins.header = 0.2
        ws.page_margins.footer = 0.2

    def _is_inv_src(src):
        return isinstance(src, tuple) and src[0] == "inv"

    def _is_flow_src(src):
        return isinstance(src, tuple) and src[0] in ("restock", "consume", "restock_now", "consume_now")

    def _write_header(ws):
        for ci, (header, src, width) in enumerate(col_specs, 1):
            cell = ws.cell(row=1, column=ci, value=header)
            # 進貨/耗用 header 用普通字體；其他（注/品項/櫃/廠/盤點）粗體
            cell.font = header_norm_font if _is_flow_src(src) else header_bold_font
            cell.alignment = center
            cell.border = border_thin
            ws.column_dimensions[get_column_letter(ci)].width = width
        ws.row_dimensions[1].height = 22

    def _write_cat_data(ws, cat_data, start_row):
        row_num = start_row
        prev_initial = None
        for _, r in cat_data.iterrows():
            initial = get_bopomofo_initial(r["品項"])
            # 注音不同 → 上一行底加雙線
            if prev_initial is not None and initial != prev_initial:
                for ci in range(1, len(col_specs) + 1):
                    cell = ws.cell(row=row_num - 1, column=ci)
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=double)
            show_initial = initial if initial != prev_initial else ""
            prev_initial = initial

            pid = r.get("_pid")
            p_name = r["品項"]
            cabinet_val = (r.get("櫃位") or "").strip()
            is_c_cabinet = cabinet_val.startswith("C") or cabinet_val.startswith("c")

            for ci, (_, src, _) in enumerate(col_specs, 1):
                val = _cell_value(pid, p_name, src, show_initial)
                cell = ws.cell(row=row_num, column=ci, value=val)
                cell.border = border_thin
                cell.alignment = left_align if src == "name" else center
                # C 開頭櫃位 → 紅字紅底
                if src == "cabinet" and is_c_cabinet:
                    cell.font = red_font
                    cell.fill = red_fill
                elif _is_inv_src(src):
                    # 盤點欄資料粗體（與耗用/進貨區分）
                    cell.font = data_bold_font
                else:
                    cell.font = data_font
            ws.row_dimensions[row_num].height = 22
            row_num += 1
        return row_num

    buf = io.BytesIO()
    wb = Workbook()
    wb.remove(wb.active)

    grouped = df.groupby("分類", sort=False)
    merge_groups = []

    for cat_name, cat_group in grouped:
        if cat_name in MERGE_CATS:
            merge_groups.append((cat_name, cat_group))
        else:
            ws = wb.create_sheet(title=str(cat_name)[:31])
            _setup_page(ws)
            _write_header(ws)
            _write_cat_data(ws, cat_group.reset_index(drop=True), 2)

    if merge_groups:
        ws = wb.create_sheet(title="其他藥材")
        _setup_page(ws)
        _write_header(ws)
        row_num = 2
        for idx, (cat_name, cat_group) in enumerate(merge_groups):
            cell = ws.cell(row=row_num, column=1, value=f"【{cat_name}】")
            cell.font = cat_title_font
            ws.merge_cells(start_row=row_num, start_column=1,
                           end_row=row_num, end_column=len(col_specs))
            ws.row_dimensions[row_num].height = 24
            row_num += 1
            row_num = _write_cat_data(ws, cat_group.reset_index(drop=True), row_num)
            if idx < len(merge_groups) - 1:
                row_num += 2

    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ══════════════════════════════════════════════
#  叫貨單：廠牌順序、分組、Excel 輸出
# ══════════════════════════════════════════════
# 步驟二／叫貨單的廠牌順序：固定前段 → 其他廠牌（依建立順序）→ 自研粉劑 → 未指定
BRAND_ORDER_HEAD = ["莊松榮", "科達", "港香蘭", "天一", "順天堂", "大墩", "駿賀", "上賀漢聖"]
BRAND_ORDER_TAIL = ["自研粉劑"]
BRAND_UNSPECIFIED = "未指定"
BRAND_NO_REORDER = "用完不補"  # 此廠牌的品項預設不勾選、不匯出
ORDER_NO_OTHER = "（無）"  # 叫貨出表「其他廠牌」下拉的未選值（data_editor 會把缺值顯示成 "None"）


def brand_order_key_map(brands: list) -> dict:
    """{廠牌名稱: 排序鍵}；未列入固定清單的廠牌（含未來新增）排在固定前段之後、自研粉劑之前，依建立順序。"""
    keys = {}
    for i, name in enumerate(BRAND_ORDER_HEAD):
        keys[name] = (0, i)
    for b in sorted(brands, key=lambda b: b["id"]):
        keys.setdefault(b["name"], (1, b["id"]))
    for i, name in enumerate(BRAND_ORDER_TAIL):
        keys[name] = (2, i)
    keys[BRAND_UNSPECIFIED] = (3, 0)
    return keys


def group_order_items(items: list, brand_keys: dict) -> list:
    """items: [{"品項","數量","category_id","廠牌"}, ...]（已依 分類→注音/櫃位 排序）。
    回傳 [(廠牌名稱, [item, ...]), ...]：廠牌依 brand_order_key_map 排序；
    廠牌為「用完不補」或數量 ≤ 0 者略過。各廠牌內維持原順序（分類順序 → 注音/櫃位）。"""
    groups = defaultdict(list)
    for it in items:
        brand = it.get("廠牌") or BRAND_UNSPECIFIED
        if brand == BRAND_NO_REORDER:
            continue
        try:
            qty = float(it.get("數量") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        groups[brand].append(it)
    default_key = (1, 10 ** 9)
    return sorted(groups.items(), key=lambda kv: brand_keys.get(kv[0], default_key))


def resolve_order_brand(row) -> str:
    """步驟一的廠牌三選一 → 出單廠牌：其他廠牌 > ✓2 > ✓1；三個都沒選（漏勾）以廠牌1 出單，沒有廠牌1 則「未指定」。"""
    other = row.get("其他廠牌")
    if isinstance(other, str) and other.strip() and other.strip() != ORDER_NO_OTHER:
        return other.strip()
    if row.get("✓2") and row.get("廠牌2"):
        return row["廠牌2"]
    if row.get("✓1") and row.get("廠牌1"):
        return row["廠牌1"]
    return row.get("廠牌1") or BRAND_UNSPECIFIED


def roc_date_str(d: date) -> str:
    """民國日期字串，例：2026-10-21 → 115/10/21（不補零，同範例）。"""
    return f"{d.year - 1911}/{d.month}/{d.day}"


def _build_order_excel(clinic_name: str, groups: list, order_date: date | None = None) -> bytes:
    """叫貨單 Excel（格式依「叫貨單範例.xlsx」工作表1）：
    第 1 列民國日期；每個廠牌一區塊：「{診所}中醫」、廠牌名稱、品項/數量每列 4 組（A/B、C/D、E/F、G/H）；
    品項由左到右再換列，分類不同時換新列（不加分類標題）；區塊間空一列；全部同一工作表、不分頁。
    groups: [(廠牌, [{"品項","數量","category_id"}, ...]), ...]（已排序）。數量為整數寫整數、有小數才寫小數。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.worksheet.properties import PageSetupProperties

    wb = Workbook()
    ws = wb.active
    ws.title = "叫貨單"
    font = Font(name="新細明體", size=12)
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    item_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
    qty_align = Alignment(horizontal="center", vertical="center")
    for col, w in zip("ABCDEFGH", (13.9, 6.3, 12.7, 6.9, 12.7, 7.7, 12.7, 7.7)):
        ws.column_dimensions[col].width = w

    def put(r, c, v, align=None, boxed=False):
        cell = ws.cell(row=r, column=c, value=v)
        cell.font = font
        if align is not None:
            cell.alignment = align
        if boxed:
            cell.border = border
        return cell

    def qty_value(q):
        try:
            f = float(q)
        except (TypeError, ValueError):
            return q
        return int(f) if f.is_integer() else round(f, 1)

    r = 1
    put(r, 1, roc_date_str(order_date or date.today()))
    r += 1
    for brand, items in groups:
        put(r, 1, f"{clinic_name}中醫")
        r += 1
        put(r, 1, brand)
        r += 1
        slot = 0  # 本列已填入第幾組（0~3）
        last_cat = None
        for it in items:
            cat = it.get("category_id")
            if last_cat is not None and cat != last_cat and slot != 0:
                r += 1  # 分類不同 → 換新列
                slot = 0
            last_cat = cat
            if slot == 0:
                for c in range(1, 9):  # 整列先畫框線（同範例：未填滿的格也有框）
                    put(r, c, None, boxed=True)
            put(r, slot * 2 + 1, it["品項"], item_align, boxed=True)
            put(r, slot * 2 + 2, qty_value(it["數量"]), qty_align, boxed=True)
            slot += 1
            if slot == 4:
                r += 1
                slot = 0
        if slot != 0:
            r += 1
        r += 1  # 區塊間空一列

    ws.page_setup.orientation = "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════
#  庫存表 HTML 渲染器
# ══════════════════════════════════════════════
# Why: Streamlit 內建表格（data_editor / dataframe）做不到行底色交替、每 5 行粗線、
#      異常格紅字、常駐捲軸、預設捲到最右。改以自訂 HTML + 少量 JS 嵌在 components.html
#      的 iframe 內，外觀與捲動行為可完全掌控。表格為純顯示，叫貨請至「叫貨出表」頁。

STOCK_TABLE_ZOOM_OPTIONS = [100, 110, 120, 130, 140, 150]
STOCK_TABLE_ZOOM_DEFAULT = 130
STOCK_TEXT_COLS = {"品項", "廠牌", "廠牌1", "廠牌2", "分類", "櫃位"}


def is_abnormal_consumed(consumed, avg_c: float, threshold: float) -> bool:
    """單格耗用是否異常：為負數；或 平均耗用>0 且 耗用>平均×全域閾值 且 耗用>1（避免小量誤判）。"""
    if consumed is None:
        return False
    try:
        v = float(consumed)
    except (TypeError, ValueError):
        return False
    if pd.isna(v):
        return False
    return v < 0 or (avg_c > 0 and v > avg_c * threshold and v > 1)


def _stock_header_html(col_name: str) -> str:
    """'盤(03/24)' -> 盤 + 小字日期；'進(03/24~04/07)' 同理；其餘原樣。"""
    for prefix in ("盤", "進", "耗"):
        if col_name.startswith(prefix + "(") and col_name.endswith(")"):
            return f"{prefix}<small>{_html.escape(col_name[2:-1])}</small>"
    return _html.escape(str(col_name))


def _fmt_cell(v, int_col: bool = False) -> str:
    """數值 1 位小數（int_col=True 時整數）；None/NaN 顯示空白；文字跳脫。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return _html.escape(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _html.escape(str(v))
    if pd.isna(f):
        return ""
    return f"{f:.0f}" if int_col else f"{f:.1f}"


_STOCK_TABLE_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: transparent; }
#wrap {
  font-family: "Segoe UI", "Microsoft JhengHei", "PingFang TC", "Noto Sans TC", system-ui, sans-serif;
  font-size: __FONT__px; color: #111; line-height: 1.25;
  --l0: 0px; --l1: 0px; --l2: 0px; --l3: 0px; --l4: 0px;
}
.hbar { overflow-x: scroll; overflow-y: hidden; height: 18px; margin: 0 0 4px 0; }
.hbar > div { height: 1px; }
#box { overflow: auto; height: __BOXH__px; border: 1px solid #C9BEE8; border-radius: 6px; background: #F7F4FC; }
table { border-collapse: separate; border-spacing: 0; white-space: nowrap; font-variant-numeric: tabular-nums; }
th, td { padding: 0.28em 0.5em; border-bottom: 1px solid #D5CBEC; border-right: 1px solid #E3DCF3; }
th { position: sticky; top: 0; z-index: 2; background: #D9CFF0; color: #111; font-weight: 700;
     text-align: center; vertical-align: bottom; border-bottom: 2px solid #6A5ACD; }
th small { display: block; font-size: 0.72em; font-weight: 600; color: #3B2E7E; }
td.n { text-align: right; }
tr.o td { background: #F7F4FC; }
tr.e td { background: #ECE6F7; }
tr.b5 td { border-bottom: 3px solid #6A5ACD; }
td.p, th.p { position: sticky; z-index: 1; }
th.p { z-index: 3; }
.p0 { left: var(--l0); } .p1 { left: var(--l1); } .p2 { left: var(--l2); }
.p3 { left: var(--l3); } .p4 { left: var(--l4); }
td.plast, th.plast { border-right: 3px solid #6A5ACD; }
td.name { font-weight: 600; }
td.stock { font-weight: 700; font-size: 1.06em; }
td.sug:not(:empty) { font-weight: 700; color: #4B3BA8; }
td.abn { color: #D32F2F; font-weight: 800; }
/* 常駐、加粗的捲軸（不需滑鼠移過才出現） */
.hbar::-webkit-scrollbar, #box::-webkit-scrollbar { height: 16px; width: 16px; }
.hbar::-webkit-scrollbar-track, #box::-webkit-scrollbar-track { background: #E6E0F2; border-radius: 8px; }
.hbar::-webkit-scrollbar-thumb, #box::-webkit-scrollbar-thumb {
  background: #8E7CC3; border-radius: 8px; border: 3px solid #E6E0F2; }
.hbar::-webkit-scrollbar-thumb:hover, #box::-webkit-scrollbar-thumb:hover { background: #6A5ACD; }
#box::-webkit-scrollbar-corner { background: #E6E0F2; }
"""

_STOCK_TABLE_JS = """
(function () {
  var wrap = document.getElementById('wrap');
  var box = document.getElementById('box');
  var top = document.getElementById('top');
  var topin = document.getElementById('topin');
  var tbl = document.getElementById('tbl');
  var PIN = __PIN__;
  var CAP = __CAP__;

  // 同源 iframe 可直接調整自身高度；若取不到就退回 Python 給的固定高度
  var frameOK = false;
  try { frameOK = !!window.frameElement; } catch (e) { frameOK = false; }

  // 固定欄的 left 偏移：依實際表頭寬度計算，字級/字型變動都不會跑掉
  function pinOffsets() {
    var ths = tbl.tHead.rows[0].cells, left = 0;
    for (var i = 0; i < PIN && i < ths.length; i++) {
      wrap.style.setProperty('--l' + i, left + 'px');
      left += ths[i].getBoundingClientRect().width;
    }
  }
  // 表格高度配合瀏覽器視窗：頁面往下捲到表格頂端時，含底部捲軸整個都在畫面內
  function fit() {
    var full = Math.ceil(tbl.getBoundingClientRect().height) + 20;
    var cap = CAP;
    try {
      var vh = window.parent.innerHeight;
      if (vh) cap = Math.max(320, vh - 140);
    } catch (e) {}
    if (!frameOK) cap = Math.min(cap, window.innerHeight - top.offsetHeight - 8);
    var h = Math.max(120, Math.min(full, cap));
    box.style.height = h + 'px';
    // 上方捲軸的寬度對齊表格可視區（扣掉垂直捲軸），兩者的最大捲動量才一致
    top.style.width = box.clientWidth + 'px';
    topin.style.width = box.scrollWidth + 'px';
    if (frameOK) {
      try {
        var fe = window.frameElement;
        var total = (h + top.offsetHeight + 10) + 'px';
        fe.style.height = total;
        // Streamlit 的元件容器有自己的固定高度，一併撐開，否則 iframe 會壓到下方元件
        var holder = fe.closest ? fe.closest('[data-testid="stElementContainer"]') : null;
        (holder || fe.parentElement).style.height = total;
      } catch (e) {}
    }
  }
  // 上方捲軸與表格捲動同步；並記住使用者是否停在最右端（視窗縮放/改變大小後維持在最右）
  var atRight = true;
  function noteEdge() { atRight = (box.scrollLeft >= box.scrollWidth - box.clientWidth - 2); }
  top.addEventListener('scroll', function () { box.scrollLeft = top.scrollLeft; noteEdge(); });
  box.addEventListener('scroll', function () { top.scrollLeft = box.scrollLeft; noteEdge(); });

  function layout(scrollRight) {
    pinOffsets(); fit();
    if (scrollRight) { box.scrollLeft = box.scrollWidth; }
    top.scrollLeft = box.scrollLeft;
    noteEdge();
  }
  layout(true);
  window.addEventListener('resize', function () { layout(atRight); });
  if (document.fonts && document.fonts.ready) { document.fonts.ready.then(function () { layout(true); }); }
  setTimeout(function () { layout(true); }, 150);
})();
"""


def render_stock_table(df: pd.DataFrame, columns: list, pinned: int, abn_flags: list | None,
                       zoom_pct: int, max_height: int = 650):
    """以 HTML 表格渲染庫存表（純顯示）。
    columns: 顯示欄位（依序）；pinned: 左側固定欄數（最多 5）；
    abn_flags: 與 df 列對齊的 set 清單（該列異常耗用的欄名），None 表示不標示；
    zoom_pct: 表格字級 100~150。
    視覺：單雙行淡紫交替、每 5 行粗紫線、文字近黑、異常耗用格紅色粗體。
    捲動：上下兩條常駐橫向捲軸同步、載入後自動捲到最右、高度自動配合瀏覽器視窗。"""
    font_px = 14 * zoom_pct / 100
    n_rows = len(df)
    pinned = max(0, min(pinned, len(columns), 5))

    def _cls(i, c):
        cls = []
        if c not in STOCK_TEXT_COLS:
            cls.append("n")
        if i < pinned:
            cls.append(f"p p{i}")
            if i == pinned - 1:
                cls.append("plast")
        return cls

    head_cells = []
    for i, c in enumerate(columns):
        head_cells.append(f'<th class="{" ".join(_cls(i, c))}">{_stock_header_html(c)}</th>')
    thead = "<tr>" + "".join(head_cells) + "</tr>"

    body = []
    for r_idx, vals in enumerate(df[columns].itertuples(index=False, name=None)):
        flags = abn_flags[r_idx] if abn_flags else ()
        row_cls = "o" if r_idx % 2 == 0 else "e"
        if (r_idx + 1) % 5 == 0:
            row_cls += " b5"
        cells = []
        for i, (c, v) in enumerate(zip(columns, vals)):
            cls = _cls(i, c)
            if c == "品項":
                cls.append("name")
            elif c == "即時庫存":
                cls.append("stock")
            elif c == "建議叫貨":
                cls.append("sug")
            if c in flags:
                cls.append("abn")
            cells.append(f'<td class="{" ".join(cls)}">{_fmt_cell(v, int_col=c in INT_DISPLAY_COLS)}</td>')
        body.append(f'<tr class="{row_cls}">' + "".join(cells) + "</tr>")

    # 初始高度估算（JS 取得視窗高度後會再調整）
    row_h = font_px * 1.81 + 1
    est_total = int(n_rows * row_h + font_px * 3.2 + 20 + 22 + 10)
    py_height = min(est_total, max_height + 40)
    box_h = min(int(n_rows * row_h + font_px * 3.2 + 20), max_height)

    css = (_STOCK_TABLE_CSS
           .replace("__FONT__", f"{font_px:.1f}")
           .replace("__BOXH__", str(box_h)))
    js = (_STOCK_TABLE_JS
          .replace("__PIN__", str(pinned))
          .replace("__CAP__", str(max_height)))

    html_doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{css}</style></head><body>"
        '<div id="wrap">'
        '<div id="top" class="hbar"><div id="topin"></div></div>'
        '<div id="box"><table id="tbl">'
        f"<thead>{thead}</thead><tbody>{''.join(body)}</tbody>"
        "</table></div></div>"
        f"<script>{js}</script></body></html>"
    )
    if hasattr(st, "iframe"):
        st.iframe(html_doc, height=py_height)
    else:  # 舊版 Streamlit 沒有 st.iframe
        import streamlit.components.v1 as components
        components.html(html_doc, height=py_height, scrolling=False)


# ══════════════════════════════════════════════
#  1. 庫存總覽頁面
# ══════════════════════════════════════════════

def page_stock_overview():
    st.header("📦 庫存")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")
    if selected_clinic == "合併檢視":
        st.info("請先選擇單一診所查看庫存。")
        return

    clinic_id = get_clinic_id(selected_clinic)
    if not clinic_id:
        st.error("找不到該診所")
        return

    sb = get_supabase_client()
    brands_data = load_brands()
    brand_map = {b["id"]: b["name"] for b in brands_data}
    categories = load_categories()

    col1, col2, col3, col4 = st.columns([2, 3, 1.1, 1])
    with col1:
        cat_filter = st.selectbox("分類", ["全部"] + [c["name"] for c in categories], key="stock_cat")
    with col2:
        search = st.text_input("搜尋", placeholder="中文 或 注音首碼（ee=葛根, 2e=當歸）", key="stock_search")
    with col3:
        zoom_pct = st.selectbox(
            "表格字級", STOCK_TABLE_ZOOM_OPTIONS,
            index=STOCK_TABLE_ZOOM_OPTIONS.index(STOCK_TABLE_ZOOM_DEFAULT),
            format_func=lambda v: f"{v}%", key="stock_zoom",
            help="只放大表格本身，與瀏覽器縮放（Ctrl+滾輪）互不影響",
        )
    with col4:
        simple_view = st.toggle("📱 簡化檢視", value=False, key="stock_simple",
                                 help="行動版精簡欄位（品項/廠牌/即時庫存/平均耗用）")

    # 載入 per-clinic 資料與品項（快取；先載入以便排序時用櫃位）
    try:
        cs_data, products = load_stock_snapshot(int(clinic_id))
    except NETWORK_ERRORS:
        st.error("連線資料庫逾時（已自動重試 3 次）。請稍候按下方「重新讀取資料」，或檢查網路。")
        if st.button("🔄 重新讀取資料", key="stock_retry_snapshot"):
            invalidate_stock_cache()
            st.rerun()
        return
    cs_map = {s["product_id"]: s for s in cs_data}

    # 依（分類→首字注音→櫃位→全字注音）排序
    products = sort_products_by_bopomofo(products, cs_map=cs_map)

    # 搜尋索引
    all_names = tuple(p["name"] for p in products)
    key_index = build_bopomofo_index(all_names)

    # 載入參數：全域（平均耗用計算次數 / 異常閾值）＋ 各分類（備貨倍數 / 安全係數，可依櫃位）
    gp = load_global_params()
    cat_params = build_category_params(categories)

    # 載入盤點紀錄：只抓近 K 次盤點日期（K = max(顯示 6 次, 平均耗用計算次數 + 1)），快取
    try:
        all_logs, window_start = load_recent_logs(
            int(clinic_id), avg_window_dates(gp["avg_n"]),
            "id, product_id, current_count_qty, consumed_qty, log_date, session_id",
        )
        all_tx = load_transactions_since(int(clinic_id), window_start)
    except NETWORK_ERRORS:
        st.error("讀取盤點紀錄逾時（已自動重試 3 次）。請稍候按下方「重新讀取資料」，或檢查網路。")
        if st.button("🔄 重新讀取資料", key="stock_retry_logs"):
            invalidate_stock_cache()
            st.rerun()
        return

    # 每品項近 N 次耗用（負數當 0，含 0 算分母；N = 全域「平均耗用計算次數」）
    consumed_recent_map = build_recent_consumed_map(all_logs, n=gp["avg_n"], window_start=window_start)

    # 取最近 MAX_DISPLAY_DATES 次不重複盤點日期（asc：舊→新）
    seen_d = set()
    recent_desc = []
    for log in all_logs:  # already desc by log_date
        d = log["log_date"]
        if d not in seen_d:
            seen_d.add(d)
            recent_desc.append(d)
        if len(recent_desc) >= MAX_DISPLAY_DATES:
            break
    all_dates = sorted(recent_desc)

    # 動態建立欄位定義 [(col_name, ctype, meta), ...]
    # ctype: "inv"=盤點 / "restock"=兩盤之間進貨 / "consume"=兩盤之間耗用 / "restock_now"=最後盤點到今的進貨
    inv_col_specs = []
    for i, d in enumerate(all_dates):
        sd = short_date(d)
        inv_col_specs.append((f"盤({sd})", "inv", d))
        if i < len(all_dates) - 1:
            d_next = all_dates[i + 1]
            sd_next = short_date(d_next)
            inv_col_specs.append((f"進({sd}~{sd_next})", "restock", (d, d_next)))
            inv_col_specs.append((f"耗({sd}~{sd_next})", "consume", (d, d_next)))
    if all_dates:
        inv_col_specs.append((f"進(~今)", "restock_now", all_dates[-1]))

    # 每品項盤點紀錄
    product_log_map = defaultdict(dict)
    for log in all_logs:
        pid = log["product_id"]
        d = log["log_date"]
        if d not in product_log_map[pid]:
            product_log_map[pid][d] = log

    # 進退貨（只需視窗內的；已於上方與盤點紀錄一併載入）
    tx_by_product = defaultdict(list)
    for tx in all_tx:
        tx_by_product[tx["product_id"]].append(tx)

    # 組裝表格
    rows = []
    product_id_list = []
    abn_flags = []  # 與 rows 對齊：每列異常耗用的欄名集合
    order_items = []  # 匯出叫貨單用：全部品項（不受分類/搜尋篩選）中建議叫貨 > 0 者，廠牌＝廠牌1

    for p in products:
        pid = p["id"]
        cs = cs_map.get(pid)
        if not cs or not cs.get("is_active", True):
            continue

        cat_name = p["categories"]["name"]
        current_stock = float(cs["current_stock"]) if cs else 0
        recent_consumed = consumed_recent_map.get(pid, [])
        avg_c = calc_avg_consumption(recent_consumed, max_n=gp["avg_n"])
        multiplier, safety = effective_params(cat_params, cat_name, cs.get("cabinet"))
        suggested = suggest_order_qty(avg_c, current_stock, multiplier, safety)
        if suggested > 0:
            order_items.append({
                "品項": p["name"], "數量": suggested, "category_id": p.get("category_id"),
                "廠牌": brand_map.get(cs.get("brand1_id")) or BRAND_UNSPECIFIED,
            })

        if cat_filter != "全部" and cat_name != cat_filter:
            continue
        if search and not match_search(p["name"], key_index.get(p["name"], ("", "")), search):
            continue

        p_logs = product_log_map.get(pid, {})
        abn_cols = set()  # 此列中耗用異常的欄名（含歷史；修正盤點數/進退貨後重算即消失）

        row = {
            "_pid": pid,
            "品項": p["name"],
            "廠牌1": brand_map.get(cs.get("brand1_id"), "-"),
            "廠牌2": brand_map.get(cs.get("brand2_id"), "-"),
            "分類": cat_name,
            "櫃位": cs.get("cabinet") or "",
        }

        # 動態盤點/進貨/耗用欄
        for col_name, ctype, meta in inv_col_specs:
            if ctype == "inv":
                lg = p_logs.get(meta)
                row[col_name] = round(float(lg["current_count_qty"]), 1) if lg else None
            elif ctype == "restock":
                d_a, d_b = meta
                row[col_name] = round(sum(
                    float(t["change_qty"]) for t in tx_by_product.get(pid, [])
                    if d_a < t["tx_date"] <= d_b
                ), 1)
            elif ctype == "consume":
                d_a, d_b = meta
                lg_a = p_logs.get(d_a)
                lg_b = p_logs.get(d_b)
                if lg_a and lg_b:
                    restock = sum(
                        float(t["change_qty"]) for t in tx_by_product.get(pid, [])
                        if d_a < t["tx_date"] <= d_b
                    )
                    consumed = round(float(lg_a["current_count_qty"]) + restock - float(lg_b["current_count_qty"]), 1)
                    row[col_name] = consumed
                    # 每一格耗用都套用異常規則（不只最近一次），以紅色粗體標示
                    if is_abnormal_consumed(consumed, avg_c, gp["threshold"]):
                        abn_cols.add(col_name)
                else:
                    row[col_name] = None
            elif ctype == "restock_now":
                row[col_name] = round(sum(
                    float(t["change_qty"]) for t in tx_by_product.get(pid, [])
                    if t["tx_date"] > meta
                ), 1)

        row["即時庫存"] = current_stock
        row["平均耗用"] = round(avg_c, 1)
        row["建議叫貨"] = suggested

        rows.append(row)
        product_id_list.append(pid)
        abn_flags.append(abn_cols)

    if not rows:
        st.info("沒有符合條件的品項")
        return

    df = pd.DataFrame(rows)
    df_display = hide_zeros_in_cols(df)

    # ── 簡化檢視（手機）──
    if simple_view:
        simple_df = df_display[["品項", "廠牌1", "即時庫存", "平均耗用"]].rename(columns={"廠牌1": "廠牌"})
        render_stock_table(simple_df, list(simple_df.columns), pinned=0, abn_flags=None, zoom_pct=zoom_pct)
        st.caption(f"共 {len(df)} 個品項 — {selected_clinic}（簡化檢視｜匯出請關閉簡化檢視）")
        return

    # ── 主表格（HTML，純顯示）──
    # 左側固定：品項、廠牌1、廠牌2、即時庫存、平均耗用；右側尾端：…耗、盤、進(~今)、建議叫貨、櫃位、分類
    inv_col_names = [c[0] for c in inv_col_specs]
    visible_cols = ["品項", "廠牌1", "廠牌2", "即時庫存", "平均耗用"] + inv_col_names + ["建議叫貨", "櫃位", "分類"]

    st.caption("💡 上下兩條捲軸皆可左右拉動，載入時自動停在最右（最新盤點）；左側 5 欄固定。"
               "紅色粗體 = 該次耗用異常（為負數，或超過平均耗用×警示閾值）。")
    render_stock_table(df_display, visible_cols, pinned=5, abn_flags=abn_flags, zoom_pct=zoom_pct)
    if st.button("🔄 重新讀取資料", help="資料已快取 10 分鐘；盤點/進退貨/改錯存檔後會自動更新，若懷疑畫面過期可手動重讀"):
        invalidate_stock_cache()
        st.rerun()

    # 匯出：庫存表（取最近 3 次盤點 + 進貨）／叫貨單（全部品項的建議叫貨 × 廠牌1，不受篩選）
    st.divider()
    exp1, exp2 = st.columns(2)
    with exp1:
        if st.button("📥 匯出庫存表 (.xlsx)", use_container_width=True):
            # 最近 3 個盤點日期（舊→新）
            recent_dates = all_dates[-3:] if all_dates else []
            buf = _build_stock_excel(df, selected_clinic, recent_dates,
                                      product_log_map, tx_by_product, cs_map, brand_map)
            st.download_button(
                "📥 下載庫存表",
                data=buf,
                file_name=f"庫存表_{selected_clinic}_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, type="primary",
            )
    with exp2:
        order_groups = group_order_items(order_items, brand_order_key_map(brands_data))
        n_order = sum(len(items) for _, items in order_groups)
        st.download_button(
            f"📥 匯出叫貨單 (.xlsx)　{len(order_groups)} 個廠牌 / {n_order} 項",
            data=_build_order_excel(selected_clinic, order_groups),
            file_name=f"叫貨單_{selected_clinic}_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, disabled=n_order == 0,
            help="全部品項（不受上方分類/搜尋篩選）中建議叫貨 > 0 者：數量＝建議叫貨、廠牌＝廠牌1；"
                 "廠牌1 為「用完不補」者不列入。要調整數量或廠牌請到「叫貨出表」。",
        )

    # 改錯存檔
    with st.expander("🔧 改錯存檔（修正盤點數量）"):
        st.caption("僅供事後檢討發現錯誤時修正盤點數量")
        if not product_id_list:
            return

        product_names = {rows[i]["品項"]: product_id_list[i] for i in range(len(rows))}
        selected_name = st.selectbox("選擇品項", list(product_names.keys()), key="fix_product")
        fix_pid = product_names[selected_name]

        fix_logs = sb.table("inventory_logs").select(
            "id, current_count_qty, log_date, session_id"
        ).eq("product_id", fix_pid).eq("clinic_id", clinic_id).order("log_date", desc=True).limit(5).execute().data

        if not fix_logs:
            st.info("該品項尚無盤點紀錄")
        else:
            fix_options = {f"{l['log_date']} — 數量: {round(float(l['current_count_qty']), 1)}": l for l in fix_logs}
            selected_fix = st.selectbox("選擇要修正的盤點紀錄", list(fix_options.keys()), key="fix_log")
            fix_log = fix_options[selected_fix]

            new_qty = st.number_input("修正後數量", value=float(fix_log["current_count_qty"]), step=0.1, format="%.1f", key="fix_qty")

            if st.button("改錯存檔", type="primary"):
                try:
                    sb.table("inventory_logs").update(
                        {"current_count_qty": new_qty}
                    ).eq("id", fix_log["id"]).execute()

                    recalc_consumed_for_product(int(fix_pid), int(clinic_id))
                    st.success(f"已修正：{selected_name} {fix_log['log_date']} → {new_qty}（已自動重算後續耗用量）")
                except Exception as e:
                    st.error(f"修正失敗：{e}")

    st.caption(f"共 {len(df)} 個品項 — {selected_clinic}")


# ══════════════════════════════════════════════
#  2. 進退貨登錄頁面
# ══════════════════════════════════════════════

def page_transactions():
    st.header("📥 進退貨登錄")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")
    user = st.session_state.user

    if selected_clinic == "合併檢視":
        st.info("請先選擇單一診所。")
        return

    clinic_id = get_clinic_id(selected_clinic)
    if not clinic_id:
        st.error("找不到該診所")
        return

    tab_add, tab_history = st.tabs(["➕ 新增紀錄", "📜 歷史紀錄"])

    with tab_add:
        sb = get_supabase_client()
        # 先載 per-clinic 含 cabinet（排序用）
        cs_data = sb.table("clinic_stock").select(
            "product_id, current_stock, is_active, cabinet"
        ).eq("clinic_id", clinic_id).execute().data
        cs_map = {s["product_id"]: s for s in cs_data}

        products = sb.table("products").select(
            "id, name, category_id, categories(name), units(name)"
        ).execute().data
        products = sort_products_by_bopomofo(products, cs_map=cs_map)
        products = [p for p in products if cs_map.get(p["id"], {}).get("is_active", True)]

        stock_map = {s["product_id"]: float(s["current_stock"]) for s in cs_data}

        categories = load_categories()
        col_filter, col_search = st.columns([1, 2])
        with col_filter:
            cat_filter = st.selectbox("分類", ["全部"] + [c["name"] for c in categories], key="tx_cat")
        with col_search:
            search = st.text_input("搜尋品項", placeholder="中文 或 注音首碼", key="tx_search")

        tx_names = tuple(p["name"] for p in products)
        tx_key_index = build_bopomofo_index(tx_names)

        filtered = products
        if cat_filter != "全部":
            filtered = [p for p in filtered if p["categories"]["name"] == cat_filter]
        if search:
            filtered = [p for p in filtered if match_search(p["name"], tx_key_index.get(p["name"], ("", "")), search)]

        if not filtered:
            st.warning("沒有符合條件的品項")
            return

        product_options = {f"{p['name']}（{p['categories']['name']}）": p for p in filtered}
        selected = st.selectbox("選擇品項", list(product_options.keys()), key="tx_product")
        product = product_options[selected]

        current_stock = stock_map.get(product["id"], 0)
        st.metric(f"📍 {selected_clinic} 目前庫存", f"{current_stock} {product['units']['name']}")

        # 不用 st.form：避免 Enter 誤觸送出，必須點按鈕才會登錄
        st.caption("💡 填完後請**點下方「確認登錄」按鈕**，按 Enter 不會送出")
        col1, col2, col3 = st.columns(3)
        with col1:
            tx_type = st.selectbox("異動類型", ["進貨", "調撥", "退貨/廢棄"], key="tx_type_input")
        with col2:
            qty = st.number_input("數量（負數=調出或退貨/廢棄）", value=0.0, step=0.1, format="%.1f", key="tx_qty_input")
        with col3:
            tx_date = st.date_input("日期", value=date.today(), key="tx_date_input")

        col4, col5 = st.columns(2)
        with col4:
            tx_brands = load_brands()
            tx_brand_options = ["-"] + [b["name"] for b in tx_brands]
            tx_brand = st.selectbox("廠牌", tx_brand_options, key="tx_brand")
        with col5:
            note = st.text_input("備註（選填）", key="tx_note_input")

        if st.button("✅ 確認登錄", type="primary", use_container_width=True, key="tx_submit_btn"):
            if qty == 0:
                st.error("數量不可為 0")
            else:
                # 進貨類型：同日同品項已存在 → 拒絕登錄
                dup_resp = []
                if tx_type == "進貨":
                    dup_resp = sb.table("transactions").select("id, change_qty").eq(
                        "product_id", product["id"]).eq(
                        "clinic_id", clinic_id).eq(
                        "tx_date", str(tx_date)).eq(
                        "tx_type", "進貨").execute().data

                if dup_resp:
                    existing = ", ".join(f"{float(t['change_qty']):+.1f}" for t in dup_resp)
                    st.error(
                        f"❌ 拒絕登錄：{tx_date} 已有 **{product['name']}** 的進貨紀錄"
                        f"（{len(dup_resp)} 筆，數量：{existing}）。\n\n"
                        f"請切換到上方「📜 歷史紀錄」分頁，找到該筆修改數量；"
                        f"或先刪除後再重新登錄。"
                    )
                else:
                    try:
                        full_note = f"[{tx_brand}] {note}".strip() if tx_brand != "-" else (note or None)
                        sb.table("transactions").insert({
                            "product_id": product["id"], "clinic_id": clinic_id,
                            "change_qty": qty, "tx_date": str(tx_date),
                            "tx_type": tx_type, "note": full_note, "created_by": user["id"],
                        }).execute()

                        recalc_consumed_for_product(int(product["id"]), int(clinic_id))
                        new_stock = sb.table("clinic_stock").select("current_stock").eq(
                            "product_id", product["id"]).eq("clinic_id", clinic_id).execute().data[0]["current_stock"]
                        st.success(f"已登錄：{product['name']} {qty:+.1f}（即時庫存：{new_stock}）")
                    except Exception as e:
                        st.error(f"登錄失敗：{e}")

    with tab_history:
        sb = get_supabase_client()
        col1, col2, col3 = st.columns([2, 2, 3])
        with col1:
            days_filter = st.selectbox("時間範圍", ["最近 30 天", "最近 7 天", "最近 90 天", "全部"], key="tx_days")
        with col2:
            type_filter = st.selectbox("類型", ["全部", "進貨", "調撥", "退貨/廢棄"], key="tx_type_filter")
        with col3:
            name_search = st.text_input("🔍 搜尋品項", placeholder="輸入品項名稱（可注音首碼，如 ee=葛根）", key="tx_hist_search")

        query = sb.table("transactions").select(
            "id, product_id, change_qty, tx_date, tx_type, note, "
            "products(name, units(name)), users(display_name)"
        ).eq("clinic_id", clinic_id).order("tx_date", desc=True)

        if days_filter != "全部":
            days = {"最近 7 天": 7, "最近 30 天": 30, "最近 90 天": 90}[days_filter]
            since = (date.today() - timedelta(days=days)).isoformat()
            query = query.gte("tx_date", since)
        if type_filter != "全部":
            query = query.eq("tx_type", type_filter)

        resp = query.limit(500).execute()

        # 品項名稱搜尋（含注音首碼）
        filtered_resp = resp.data
        if name_search and resp.data:
            unique_names = tuple({t["products"]["name"] for t in resp.data})
            name_idx = build_bopomofo_index(unique_names)
            filtered_resp = [
                t for t in resp.data
                if match_search(t["products"]["name"], name_idx.get(t["products"]["name"], ("", "")), name_search)
            ]

        # 重複進貨偵測（同診所、同品項、同日期 ≥ 2 筆）
        with st.expander("⚠️ 重複進貨偵測（同日期同品項 ≥ 2 筆）"):
            st.caption(f"範圍：{selected_clinic} 的所有 transactions（不受上方時間/類型/搜尋篩選影響）")
            all_tx_for_dup = fetch_all(sb.table("transactions").select(
                "id, tx_date, change_qty, tx_type, note, product_id, products(name)"
            ).eq("clinic_id", clinic_id).order("tx_date", desc=True))
            dup_groups = defaultdict(list)
            for t in all_tx_for_dup:
                dup_groups[(t["tx_date"], t["product_id"])].append(t)
            dup_rows = []
            for (d, pid), items in dup_groups.items():
                if len(items) >= 2:
                    for t in items:
                        dup_rows.append({
                            "id": t["id"],
                            "日期": t["tx_date"],
                            "品項": t["products"]["name"],
                            "類型": t["tx_type"],
                            "數量": float(t["change_qty"]),
                            "備註": t["note"] or "",
                        })
            if dup_rows:
                dup_df = pd.DataFrame(dup_rows).sort_values(["日期", "品項", "id"]).reset_index(drop=True)
                st.dataframe(style_banded(dup_df, bold_cols=["數量"]),
                             use_container_width=True, hide_index=True)
                st.caption(f"共 {len(dup_rows)} 筆涉及 {len({(r['日期'], r['品項']) for r in dup_rows})} 組重複，請以 id 在下方表格找到後修改/刪除")
            else:
                st.success("沒有重複進貨")

        if not filtered_resp:
            st.info("沒有符合條件的異動紀錄")
        else:
            hist_rows = [{
                "id": t["id"],
                "日期": t["tx_date"], "品項": t["products"]["name"], "類型": t["tx_type"],
                "數量": float(t["change_qty"]), "單位": t["products"]["units"]["name"],
                "備註": t["note"] or "", "操作人": t["users"]["display_name"] if t.get("users") else "-",
            } for t in filtered_resp]

            hist_df = pd.DataFrame(hist_rows)
            styled = style_banded(hist_df, bold_cols=["數量"])
            styled = styled.map(
                lambda v: "color:#DC3545;font-weight:bold" if isinstance(v, (int, float)) and v < 0 else (
                    "color:#28A745;font-weight:bold" if isinstance(v, (int, float)) and v > 0 else ""),
                subset=["數量"],
            )
            st.caption(f"共 {len(hist_rows)} 筆 — 點選表格中的列即可在下方編輯")
            event = st.dataframe(
                styled, use_container_width=True, hide_index=True, height=400,
                on_select="rerun", selection_mode="single-row", key="tx_hist_table",
            )

            # 修改/刪除（自動帶入點選列）
            st.divider()
            st.subheader("修改或刪除紀錄")

            tx_options = {
                f"#{t['id']} | {t['tx_date']} | {t['products']['name']} | {float(t['change_qty']):+.1f}": t
                for t in filtered_resp
            }
            options_keys = list(tx_options.keys())

            # 表格選取列 → 同步寫入 session_state（必須在 selectbox 渲染前）
            if event.selection.rows:
                new_idx = event.selection.rows[0]
                if 0 <= new_idx < len(options_keys):
                    st.session_state["tx_hist_select"] = options_keys[new_idx]

            # 若 session_state 的值已不在 options（例如改了搜尋條件），重置為第一個
            if (
                "tx_hist_select" not in st.session_state
                or st.session_state.get("tx_hist_select") not in options_keys
            ):
                st.session_state["tx_hist_select"] = options_keys[0]

            selected_tx_key = st.selectbox(
                "選擇紀錄（點上方表格自動選取）",
                options_keys,
                key="tx_hist_select",
            )
            tx_item = tx_options[selected_tx_key]

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                edit_qty = st.number_input("數量", value=float(tx_item["change_qty"]), step=0.1, format="%.1f", key="tx_edit_qty")
            with col_b:
                type_list = ["進貨", "調撥", "退貨/廢棄"]
                # 防呆：若舊資料尚有未遷移的值（如「廢棄」），不讓 .index 例外造成當機
                cur_idx = type_list.index(tx_item["tx_type"]) if tx_item["tx_type"] in type_list else 0
                edit_type = st.selectbox("類型", type_list,
                                         index=cur_idx, key="tx_edit_type")
            with col_c:
                edit_note = st.text_input("備註", value=tx_item["note"] or "", key="tx_edit_note")

            col_save, col_del = st.columns(2)
            with col_save:
                if st.button("💾 修改此筆", type="primary", key="tx_save_btn"):
                    try:
                        sb.table("transactions").update({
                            "change_qty": edit_qty, "tx_type": edit_type,
                            "note": edit_note or None,
                        }).eq("id", tx_item["id"]).execute()
                        recalc_consumed_for_product(int(tx_item["product_id"]), int(clinic_id))
                        st.success("已修改（已自動重算耗用量）")
                        st.rerun()
                    except Exception as e:
                        st.error(f"修改失敗：{e}")

            with col_del:
                if st.button("🗑️ 刪除此筆", type="secondary", key="tx_del_btn"):
                    try:
                        sb.table("transactions").delete().eq("id", tx_item["id"]).execute()
                        recalc_consumed_for_product(int(tx_item["product_id"]), int(clinic_id))
                        st.success("已刪除（已自動重算耗用量）")
                        st.rerun()
                    except Exception as e:
                        st.error(f"刪除失敗：{e}")


# ══════════════════════════════════════════════
#  3. 執行盤點頁面
# ══════════════════════════════════════════════

def page_inventory():
    st.header("📝 執行盤點")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")
    user = st.session_state.user

    if selected_clinic == "合併檢視":
        st.info("請先選擇單一診所。")
        return

    clinic_id = get_clinic_id(selected_clinic)
    if not clinic_id:
        st.error("找不到該診所")
        return

    sb = get_supabase_client()

    tab_do, tab_photo, tab_print, tab_history = st.tabs(["📝 執行盤點", "📸 照片盤點", "🖨️ 列印盤點表", "📜 盤點歷史"])

    # ── 列印盤點表（含歷史資料）──
    with tab_print:
        st.subheader("🖨️ 列印盤點表")
        st.caption("含歷史盤點/進貨/耗用，最右欄為新盤點數量填寫欄")

        print_cat = st.selectbox("分類", ["全部"] + [c["name"] for c in load_categories()], key="print_cat")

        # 載入 per-clinic 資料（先載以供排序）
        cs_print = sb.table("clinic_stock").select(
            "product_id, current_stock, is_active, cabinet, brand1_id, brand2_id"
        ).eq("clinic_id", clinic_id).execute().data
        cs_print_map = {s["product_id"]: s for s in cs_print}

        # 載入品項並排序
        all_prods = sb.table("products").select(
            "id, name, category_id, categories(name), units(name)"
        ).execute().data
        all_prods = sort_products_by_bopomofo(all_prods, cs_map=cs_print_map)

        brands_data = load_brands()
        brand_map_print = {b["id"]: b["name"] for b in brands_data}

        # 載入歷史盤點
        print_logs = fetch_all(sb.table("inventory_logs").select(
            "product_id, current_count_qty, log_date"
        ).eq("clinic_id", clinic_id).order("log_date", desc=True))

        # 最近 3 個盤點日期
        p_dates = []
        for lg in print_logs:
            if lg["log_date"] not in p_dates:
                p_dates.append(lg["log_date"])
            if len(p_dates) >= 3:
                break
        p_display = list(reversed(p_dates))  # 最舊→最新

        # 每品項盤點
        p_log_map = defaultdict(dict)
        for lg in print_logs:
            pid = lg["product_id"]
            d = lg["log_date"]
            if d not in p_log_map[pid]:
                p_log_map[pid][d] = lg

        # 進退貨
        print_tx = fetch_all(sb.table("transactions").select(
            "product_id, change_qty, tx_date"
        ).eq("clinic_id", clinic_id))
        tx_by_p = defaultdict(list)
        for tx in print_tx:
            tx_by_p[tx["product_id"]].append(tx)

        active_prods = [p for p in all_prods
                        if cs_print_map.get(p["id"], {}).get("is_active", True)]
        if print_cat != "全部":
            active_prods = [p for p in active_prods if p["categories"]["name"] == print_cat]

        if active_prods:
            # 用 _build_stock_excel 的邏輯產生 Excel
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
            from openpyxl.utils import get_column_letter

            thin = Side(style="thin")
            double = Side(style="double")
            border_thin = Border(left=thin, right=thin, top=thin, bottom=thin)
            hdr_bold_font = Font(bold=True, size=13)  # 盤點/品項/廠等
            hdr_norm_font = Font(size=13)  # 進貨/耗用 header 普通字體以區分
            dat_font = Font(size=12)
            dat_bold_font = Font(bold=True, size=12)  # 盤點欄資料粗體
            red_font = Font(size=12, bold=True, color="C00000")
            red_fill = PatternFill("solid", fgColor="FFE5E5")
            ctr = Alignment(horizontal="center", vertical="center")
            lft = Alignment(horizontal="left", vertical="center")

            # 組欄位
            # 順序：注、品項、櫃、廠1、廠2、(日期、進貨、耗用)×(N-1)、最末日期、進貨(到今)、耗用(空欄手算)、日期:
            pcols = []  # (header, width, data_key)
            pcols.append(("注", 3, "_initial"))
            pcols.append(("品項", 14, "_name"))
            pcols.append(("櫃", 4, "_cabinet"))
            pcols.append(("廠1", 4, "_b1"))
            pcols.append(("廠2", 4, "_b2"))

            for i, d in enumerate(p_display):
                sd = short_date(d)
                pcols.append((sd, 6.5, f"_inv_{d}"))
                if i < len(p_display) - 1:
                    pcols.append(("進貨", 6.5, f"_restock_{d}"))
                    pcols.append(("耗用", 6.5, f"_consume_{d}"))
            # 最末盤點後：進貨(到今) + 耗用(空欄供系統異常時手算)
            if p_display:
                pcols.append(("進貨", 6.5, f"_restock_{p_display[-1]}"))
                pcols.append(("耗用", 6.5, f"_consume_{p_display[-1]}"))

            pcols.append(("日期:", 12, "_new"))  # 新盤點手寫欄

            buf = io.BytesIO()
            wb = Workbook()
            wb.remove(wb.active)

            MERGE_CATS = {"水藥材", "高貴藥材", "非健保藥材"}
            cat_groups = defaultdict(list)
            for p in active_prods:
                cat_groups[p["categories"]["name"]].append(p)

            def write_print_sheet(ws, prods_list, sheet_title=None):
                ws.page_setup.orientation = "portrait"
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 0
                ws.sheet_properties.pageSetUpPr.fitToPage = True
                # 每頁列印都重複第 1 行表頭
                ws.print_title_rows = "1:1"
                # 縮小邊距
                ws.page_margins.left = 0.3
                ws.page_margins.right = 0.3
                ws.page_margins.top = 0.4
                ws.page_margins.bottom = 0.4
                ws.page_margins.header = 0.2
                ws.page_margins.footer = 0.2

                # 表頭：進貨/耗用 用普通字體；其他用粗體
                for ci, (h, w, key) in enumerate(pcols, 1):
                    cell = ws.cell(row=1, column=ci, value=h)
                    if key.startswith("_restock_") or key.startswith("_consume_"):
                        cell.font = hdr_norm_font
                    else:
                        cell.font = hdr_bold_font
                    cell.alignment = ctr
                    cell.border = border_thin
                    ws.column_dimensions[get_column_letter(ci)].width = w
                ws.row_dimensions[1].height = 22

                row_num = 2
                prev_initial = None
                for p in prods_list:
                    pid = p["id"]
                    cs = cs_print_map.get(pid, {})
                    initial = get_bopomofo_initial(p["name"])

                    if prev_initial is not None and initial != prev_initial:
                        for ci in range(1, len(pcols) + 1):
                            cell = ws.cell(row=row_num - 1, column=ci)
                            cell.border = Border(left=thin, right=thin, top=thin, bottom=double)

                    show_init = initial if initial != prev_initial else ""
                    prev_initial = initial

                    p_logs_data = p_log_map.get(pid, {})

                    for ci, (_, _, key) in enumerate(pcols, 1):
                        if key == "_initial":
                            val = show_init
                        elif key == "_name":
                            val = p["name"]
                        elif key == "_cabinet":
                            val = cs.get("cabinet") or ""
                        elif key == "_b1":
                            val = abbr_brand(brand_map_print.get(cs.get("brand1_id"), "-"))
                        elif key == "_b2":
                            val = abbr_brand(brand_map_print.get(cs.get("brand2_id"), "-"))
                        elif key and key.startswith("_inv_"):
                            d = key[5:]
                            lg = p_logs_data.get(d)
                            val = round(float(lg["current_count_qty"]), 1) if lg else ""
                        elif key and key.startswith("_restock_"):
                            d = key[9:]
                            idx = p_display.index(d) if d in p_display else -1
                            if idx < len(p_display) - 1:
                                next_d = p_display[idx + 1]
                                val = round(sum(float(t["change_qty"]) for t in tx_by_p.get(pid, [])
                                                if d < t["tx_date"] <= next_d), 1)
                            else:
                                # 最新盤點後到今天
                                val = round(sum(float(t["change_qty"]) for t in tx_by_p.get(pid, [])
                                                if t["tx_date"] > d), 1)
                        elif key and key.startswith("_consume_"):
                            d = key[9:]
                            idx = p_display.index(d) if d in p_display else -1
                            lg_this = p_logs_data.get(d)
                            if idx < len(p_display) - 1:
                                next_d = p_display[idx + 1]
                                lg_next = p_logs_data.get(next_d)
                                restock = sum(float(t["change_qty"]) for t in tx_by_p.get(pid, [])
                                              if d < t["tx_date"] <= next_d)
                                if lg_this and lg_next:
                                    val = round(float(lg_this["current_count_qty"]) + restock - float(lg_next["current_count_qty"]), 1)
                                else:
                                    val = ""
                            else:
                                val = ""  # 最新盤點的耗用待新盤點才知道
                        elif key == "_new":
                            val = ""  # 空白待填
                        else:
                            val = ""

                        cell = ws.cell(row=row_num, column=ci, value=val)
                        cell.alignment = lft if key == "_name" else ctr
                        cell.border = border_thin
                        # C 開頭櫃位 → 紅字紅底
                        if key == "_cabinet" and isinstance(val, str) and val and val[0] in ("C", "c"):
                            cell.font = red_font
                            cell.fill = red_fill
                        elif key.startswith("_inv_"):
                            # 盤點欄資料粗體（與耗用/進貨區分）
                            cell.font = dat_bold_font
                        else:
                            cell.font = dat_font
                    ws.row_dimensions[row_num].height = 22
                    row_num += 1
                return row_num

            # 正常分類各自一頁
            merge_prods = []
            for cat_name, prods in cat_groups.items():
                if cat_name in MERGE_CATS:
                    merge_prods.append((cat_name, prods))
                else:
                    ws = wb.create_sheet(title=str(cat_name)[:31])
                    write_print_sheet(ws, prods)

            # 合併小分類
            if merge_prods:
                ws = wb.create_sheet(title="其他藥材")
                ws.page_setup.orientation = "portrait"
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 0
                ws.sheet_properties.pageSetUpPr.fitToPage = True
                ws.print_title_rows = "1:1"
                ws.page_margins.left = 0.3
                ws.page_margins.right = 0.3
                ws.page_margins.top = 0.4
                ws.page_margins.bottom = 0.4
                # 表頭：進貨/耗用 普通字體；其他粗體
                for ci, (h, w, key) in enumerate(pcols, 1):
                    cell = ws.cell(row=1, column=ci, value=h)
                    if key.startswith("_restock_") or key.startswith("_consume_"):
                        cell.font = hdr_norm_font
                    else:
                        cell.font = hdr_bold_font
                    cell.alignment = ctr
                    cell.border = border_thin
                    ws.column_dimensions[get_column_letter(ci)].width = w
                ws.row_dimensions[1].height = 22
                row_num = 2
                cat_title_f = Font(bold=True, size=14)
                for idx_m, (cat_name, prods) in enumerate(merge_prods):
                    cell = ws.cell(row=row_num, column=1, value=f"【{cat_name}】")
                    cell.font = cat_title_f
                    ws.merge_cells(start_row=row_num, start_column=1,
                                   end_row=row_num, end_column=len(pcols))
                    row_num += 1
                    prev_initial = None
                    for p in prods:
                        pid = p["id"]
                        cs = cs_print_map.get(pid, {})
                        initial = get_bopomofo_initial(p["name"])
                        if prev_initial is not None and initial != prev_initial:
                            for ci in range(1, len(pcols) + 1):
                                cell = ws.cell(row=row_num - 1, column=ci)
                                cell.border = Border(left=thin, right=thin, top=thin, bottom=double)
                        show_init = initial if initial != prev_initial else ""
                        prev_initial = initial
                        p_logs_data = p_log_map.get(pid, {})
                        for ci, (_, _, key) in enumerate(pcols, 1):
                            if key == "_initial": val = show_init
                            elif key == "_name": val = p["name"]
                            elif key == "_cabinet": val = cs.get("cabinet") or " "
                            elif key == "_b1":
                                bid = cs.get("brand1_id")
                                val = abbr_brand(brand_map_print.get(bid, "-")) if bid else " "
                            elif key == "_b2":
                                bid = cs.get("brand2_id")
                                val = abbr_brand(brand_map_print.get(bid, "-")) if bid else " "
                            elif key and key.startswith("_inv_"):
                                d = key[5:]
                                lg = p_logs_data.get(d)
                                val = round(float(lg["current_count_qty"]), 1) if lg else ""
                            elif key and key.startswith("_restock_"):
                                d = key[9:]
                                idx = p_display.index(d) if d in p_display else -1
                                if idx < len(p_display) - 1:
                                    next_d = p_display[idx + 1]
                                    val = round(sum(float(t["change_qty"]) for t in tx_by_p.get(pid, [])
                                                    if d < t["tx_date"] <= next_d), 1)
                                else:
                                    val = round(sum(float(t["change_qty"]) for t in tx_by_p.get(pid, [])
                                                    if t["tx_date"] > d), 1)
                            elif key and key.startswith("_consume_"):
                                d = key[9:]
                                idx = p_display.index(d) if d in p_display else -1
                                lg_this = p_logs_data.get(d)
                                if idx < len(p_display) - 1:
                                    next_d = p_display[idx + 1]
                                    lg_next = p_logs_data.get(next_d)
                                    restock = sum(float(t["change_qty"]) for t in tx_by_p.get(pid, [])
                                                  if d < t["tx_date"] <= next_d)
                                    if lg_this and lg_next:
                                        val = round(float(lg_this["current_count_qty"]) + restock - float(lg_next["current_count_qty"]), 1)
                                    else:
                                        val = ""
                                else:
                                    val = ""
                            elif key == "_new":
                                val = ""
                            else:
                                val = ""
                            cell = ws.cell(row=row_num, column=ci, value=val)
                            cell.alignment = lft if key == "_name" else ctr
                            cell.border = border_thin
                            if key == "_cabinet" and isinstance(val, str) and val and val.strip() and val[0] in ("C", "c"):
                                cell.font = red_font
                                cell.fill = red_fill
                            elif key.startswith("_inv_"):
                                cell.font = dat_bold_font
                            else:
                                cell.font = dat_font
                        ws.row_dimensions[row_num].height = 22
                        row_num += 1
                    if idx_m < len(merge_prods) - 1:
                        row_num += 2

            wb.save(buf)
            buf.seek(0)

            st.download_button(
                f"📥 下載盤點表（{len(active_prods)} 品項）",
                data=buf.getvalue(),
                file_name=f"盤點表_{selected_clinic}_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, type="primary",
            )

    # ── 照片盤點 ──
    with tab_photo:
        st.subheader("📸 照片盤點")
        st.caption("上傳手寫盤點表照片，AI 自動辨識品項與數量")

        photo_date = st.date_input("預設盤點日期", value=date.today(), key="photo_date")
        uploaded_files = st.file_uploader(
            "上傳盤點表照片（最多 20 張）",
            type=["jpg", "jpeg", "png", "heic"],
            accept_multiple_files=True,
            key="photo_upload",
        )

        # 模型選項與雙重檢視
        col_m1, col_m2 = st.columns([1, 1])
        with col_m1:
            photo_model_choice = st.selectbox(
                "AI 模型",
                ["gemini-2.5-pro（準確，建議）", "gemini-2.5-flash（快、省）"],
                index=0,
                key="photo_model_choice",
                help="Pro 比 Flash 在手寫數字辨識上準確很多。每張成本約 $0.01–0.03 美元。",
            )
        with col_m2:
            photo_double_check = st.toggle(
                "🔍 雙重檢視（每張跑兩次比對）",
                value=True,
                key="photo_double_check",
                help="第一次辨識後再請 AI 對照原圖核對，找出錯誤並修正。慢一倍、貴一倍，但準確度顯著提升。",
            )

        if uploaded_files and len(uploaded_files) > 20:
            st.error("最多上傳 20 張照片")
            uploaded_files = uploaded_files[:20]

        # 初始化 session_state
        if "photo_results" not in st.session_state:
            st.session_state.photo_results = None

        # 步驟 1：辨識
        if uploaded_files and st.button("🔍 開始辨識", type="primary", key="photo_recognize"):
            try:
                from google import genai
                import json as json_mod
                client = genai.Client(api_key=st.secrets["gemini"]["api_key"])

                model_id = "gemini-2.5-pro" if "pro" in photo_model_choice else "gemini-2.5-flash"

                # 撈該診所所有 active 品項，組成 prompt 中的可用清單
                prompt_prods_db = sb.table("products").select(
                    "id, name, category_id, categories(name)"
                ).execute().data
                prompt_cs = sb.table("clinic_stock").select(
                    "product_id, is_active"
                ).eq("clinic_id", clinic_id).execute().data
                prompt_active_pids = {s["product_id"] for s in prompt_cs if s.get("is_active", True)}
                prompt_active_prods = [p for p in prompt_prods_db if p["id"] in prompt_active_pids]
                prompt_active_prods = sort_products_by_bopomofo(prompt_active_prods)
                _list_by_cat = defaultdict(list)
                for p in prompt_active_prods:
                    cat = (p.get("categories") or {}).get("name") or "其他"
                    initial = get_bopomofo_initial(p["name"]) or " "
                    _list_by_cat[cat].append(f"  {initial} {p['name']}")
                product_list_lines = []
                for cat, lines in _list_by_cat.items():
                    product_list_lines.append(f"【{cat}】")
                    product_list_lines.extend(lines)
                product_list_text = "\n".join(product_list_lines)

                PROMPT_EXTRACT = ("""你是高精度盤點數據抽取機器人。任務：從手寫盤點表照片，提取每個藥品對應的【最右欄手寫數量】。

【可用品項清單】（這份盤點表上的藥品全部來自此清單，前綴為注音首字）
""" + product_list_text + """

【name 欄位規則 — 非常重要】
- name 必須**逐字照抄**自上述清單，不可自行改字、補字、刪字、合併、拆分
- 若品項欄印刷字被遮擋、模糊、或截斷（例如照片只看到「桂枝芍藥知...」），請從清單中找**前綴或部分字符唯一匹配**的完整 name 並使用該完整 name
  範例：照片只讀到「桂枝芍藥知」→ 清單裡只有一個「桂枝芍藥知母湯」匹配 → name 填「桂枝芍藥知母湯」
- 若多個品名都能匹配前綴（如「四物湯」與「四物湯加味」皆以「四物湯」開頭），在 raw 加上「[截斷不確定]」標記，name 仍盡力選擇最像的一個
- 若清單中找不到任何匹配（陌生藥名），照原樣抄寫圖中讀到的字，並在 raw 前加「[未匹配]」標記

【表格結構說明】""")
                PROMPT_EXTRACT += """
從左至右大致為：注音首字 / 品項（印刷體藥名）/ 櫃位 / 廠1 / 廠2 / 多個歷史盤點與進貨欄（已印好的數字）/ 「日期:」欄（最右側，本次手寫盤點數量）

【提取目標】
只看「日期:」欄（最右欄）的手寫數字，與「品項」欄的印刷藥名配對。其他欄位的數字（歷史盤點、進貨）一律忽略。

【嚴謹處理流程，請逐步思考】

步驟 1 — 定位「日期:」欄：表格最右側，標題寫「日期:」（其後可能有手寫日期）。

步驟 2 — 對每一列藥品依序處理：
  a) 讀取「品項」欄的印刷體藥名（例：加味逍遙散）
  b) 讀取「日期:」欄該列的手寫字符
  c) 將原始字符照抄到 raw 欄位（包括 +、-、.、刪除線等記號）
  d) 計算最終數量 qty

步驟 3 — qty 計算規則：
  - 加法：「3+7」→ 10、「2+1+0.5」→ 3.5、「8+7」→ 15
  - 小數：「0.5」→ 0.5、「1.7」→ 1.7（注意小數點不要漏看）
  - 修正/刪除線：取最後寫的數字
  - 整數：直接照抄

步驟 4 — qty = null 的情況（嚴格區分）：
  - 完全空白 → null（不要寫 0）
  - 「/」（單獨斜線）→ null
  - 「-」（單獨破折號）→ null
  - 「K」「k」→ null
  - 「？」「?」「不確定」→ null
  - 寫了字但無法判讀 → null

步驟 5 — 自我檢查（內部執行，不輸出）：
  逐筆重新對照原圖檢查，特別注意這些常見錯誤：
  ✓ 加法是否算對？把 raw 中的數字再算一次
  ✓ 是否漏看小數點？「3.5」可能誤看為「35」
  ✓ 手寫常混淆：1 vs 7、0 vs 6 vs 9、3 vs 5 vs 8、4 vs 9 — 多看兩眼
  ✓ 對齊：手寫數字真的對應到該列藥品嗎？
  ✓ 空白 ≠ 0：空白應為 null
  ✓ 漏列：是否每個藥品都處理到了？

步驟 6 — 辨識日期（最右欄表頭「日期:」後的手寫日期，如 3/25、4/8）。看不清就 null。

【輸出格式】
嚴格 JSON，無任何解釋、無 markdown 標記：
{"date":"4/8","items":[{"name":"加味逍遙散","raw":"8+7","qty":15},{"name":"附子","raw":"0.5","qty":0.5},{"name":"葛根湯","raw":"/","qty":null}]}

每筆必須有 name、raw、qty 三個欄位。raw 是你看到的手寫原始字符（用於人工核對），qty 是最終數量。"""

                PROMPT_VERIFY = ("""以下是 AI 第一次辨識的結果。請對照原圖，**逐筆重新核對**每一個 name、raw 與 qty，找出辨識錯誤並修正。

【可用品項清單】（每個 name 必須是清單中的完整品名；若 name 是截斷字串如「桂枝芍藥知...」請從清單補全）
""" + product_list_text + """

【第一次辨識結果】
__VERIFY_RESULT__

【核對重點】
1. 對每一筆 item，重新看原圖該列的手寫字符
2. 確認 name 是否為清單中的完整品名（截斷的字補全；陌生品名前綴加「[未匹配]」標記到 raw）
3. 確認 raw 是否正確（手寫原貌）
4. 確認 qty 是否與 raw 計算一致（加法、小數點)
5. 找出可能誤判的字（1/7、0/6、3/5/8、4/9）
6. 確認沒有漏列

【輸出格式】
嚴格 JSON，包含**所有**藥品（不只修正的，全部都要）。修正過的條目維持同樣 schema，但用更新後的 name、raw 與 qty：
{"date":"4/8","items":[{"name":"加味逍遙散","raw":"8+7","qty":15},...]}

不要加 markdown、不要加解釋。""")

                all_items = []  # 最終結果（含 raw）
                detected_dates = []
                total_passes = (1 + (1 if photo_double_check else 0)) * len(uploaded_files)
                pass_done = 0
                progress = st.progress(0, text="辨識中...")

                for i, f in enumerate(uploaded_files):
                    img_bytes = f.read()
                    mime = f.type or "image/jpeg"

                    # ── 第一遍 ──
                    pass_done += 1
                    progress.progress(
                        pass_done / total_passes,
                        text=f"第 {i+1}/{len(uploaded_files)} 張 — 第 1 次辨識中…",
                    )
                    response = client.models.generate_content(
                        model=model_id,
                        contents=[
                            genai.types.Part.from_bytes(data=img_bytes, mime_type=mime),
                            PROMPT_EXTRACT,
                        ],
                    )
                    text = response.text.strip()
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                    result = json_mod.loads(text)

                    # ── 第二遍（若啟用雙重檢視）──
                    if photo_double_check:
                        pass_done += 1
                        progress.progress(
                            pass_done / total_passes,
                            text=f"第 {i+1}/{len(uploaded_files)} 張 — 第 2 次核對中…",
                        )
                        try:
                            verify_resp = client.models.generate_content(
                                model=model_id,
                                contents=[
                                    genai.types.Part.from_bytes(data=img_bytes, mime_type=mime),
                                    PROMPT_VERIFY.replace("__VERIFY_RESULT__", json_mod.dumps(result, ensure_ascii=False)),
                                ],
                            )
                            v_text = verify_resp.text.strip()
                            if v_text.startswith("```"):
                                v_text = v_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                            verified = json_mod.loads(v_text)
                            # 用第二次結果覆蓋
                            result = verified
                        except Exception as ve:
                            st.warning(f"第 {i+1} 張第二次核對失敗，沿用第一次結果：{ve}")

                    if result.get("date"):
                        detected_dates.append(result["date"])
                    for item in result.get("items", []):
                        if item.get("name"):
                            all_items.append(item)

                progress.empty()

                # 日期解析
                unique_dates = list(set(d for d in detected_dates if d))
                parsed_ai_dates = set()
                for d in unique_dates:
                    try:
                        parts = d.replace(".", "/").replace("-", "/").split("/")
                        m, dy = int(parts[0]), int(parts[1])
                        y = date.today().year
                        parsed_ai_dates.add(date(y, m, dy).isoformat())
                    except Exception:
                        pass

                ai_date = list(parsed_ai_dates)[0] if len(parsed_ai_dates) == 1 else None

                # 品名匹配
                all_products_db = sb.table("products").select("id, name").execute().data
                name_to_pid = {}
                for p in all_products_db:
                    name_to_pid[p["name"]] = p["id"]
                    name_to_pid[p["name"].strip()] = p["id"]
                    name_to_pid[p["name"].replace(" ", "")] = p["id"]

                cs_photo = sb.table("clinic_stock").select(
                    "product_id, current_stock, is_active"
                ).eq("clinic_id", clinic_id).execute().data
                active_pids = {s["product_id"] for s in cs_photo if s.get("is_active", True)}
                stock_map_photo = {s["product_id"]: float(s["current_stock"]) for s in cs_photo}

                # active 品項的 name → pid 對照（給 fuzzy 前綴匹配用）
                active_name_pid = [(p["name"], p["id"]) for p in all_products_db
                                   if p["id"] in active_pids]

                def fuzzy_match_pid(query_name: str):
                    """fallback：去掉省略號/標記後，用前 5 字（或更短）做唯一前綴匹配"""
                    cleaned = query_name.replace("...", "").replace("…", "").strip()
                    for marker in ("[未匹配]", "[截斷不確定]"):
                        if cleaned.startswith(marker):
                            cleaned = cleaned[len(marker):].strip()
                    if not cleaned:
                        return None
                    prefix = cleaned[:5]
                    candidates = [pid for nm, pid in active_name_pid if nm.startswith(prefix)]
                    if len(candidates) == 1:
                        return candidates[0]
                    # 嘗試更短前綴：3 字（避免誤配）
                    if len(cleaned) >= 3:
                        prefix3 = cleaned[:3]
                        candidates3 = [pid for nm, pid in active_name_pid if nm.startswith(prefix3)]
                        if len(candidates3) == 1:
                            return candidates3[0]
                    return None

                # 去重（保留 raw 給人工核對）
                item_map = {}
                for item in all_items:
                    name = item["name"].strip()
                    item_map[name] = (item.get("qty"), item.get("raw", "") or "")

                matched, unmatched = [], []
                for name, (qty, raw) in item_map.items():
                    pid = name_to_pid.get(name) or name_to_pid.get(name.replace(" ", ""))
                    if not (pid and pid in active_pids):
                        # fallback：前綴 fuzzy
                        pid = fuzzy_match_pid(name)
                    if pid and pid in active_pids:
                        matched.append({
                            "product_id": int(pid),
                            "品項": next(p["name"] for p in all_products_db if p["id"] == pid),
                            "AI看到": str(raw),
                            "辨識數量": round(float(qty), 1) if qty is not None else None,
                            "帳面庫存": stock_map_photo.get(pid, 0),
                        })
                    else:
                        unmatched.append({
                            "品項(未匹配)": name,
                            "AI看到": str(raw),
                            "辨識數量": qty,
                        })

                # 寫入辨識報告（含失敗名單，供日後糾錯）
                report_id = None
                try:
                    report_resp = sb.table("photo_recognition_reports").insert({
                        "clinic_id": int(clinic_id),
                        "operator_id": int(user["id"]),
                        "session_date": ai_date,
                        "photo_count": len(uploaded_files),
                        "item_count": len(all_items),
                        "matched_count": len(matched),
                        "unmatched_count": len(unmatched),
                        "unmatched_items": unmatched,
                        "ai_model": model_id,
                        "double_check": bool(photo_double_check),
                        "saved": False,
                    }).execute()
                    report_id = report_resp.data[0]["id"] if report_resp.data else None
                except Exception as re:
                    st.warning(f"報告記錄失敗（不影響辨識結果）：{re}")

                # 存入 session_state
                st.session_state.photo_results = {
                    "matched": matched,
                    "unmatched": unmatched,
                    "ai_date": ai_date,
                    "parsed_ai_dates": sorted(parsed_ai_dates) if parsed_ai_dates else [],
                    "photo_count": len(uploaded_files),
                    "item_count": len(all_items),
                    "report_id": report_id,
                }
                st.rerun()

            except Exception as e:
                st.error(f"辨識失敗：{e}")
                import traceback
                st.code(traceback.format_exc())

        # 步驟 2：顯示辨識結果（從 session_state 讀取）
        results = st.session_state.get("photo_results")
        if results:
            matched = results["matched"]
            unmatched = results["unmatched"]
            ai_date = results["ai_date"]

            st.success(f"辨識完成！{results['photo_count']} 張照片，{results['item_count']} 筆資料")

            # 日期處理
            if ai_date and ai_date != str(photo_date):
                st.warning(f"照片日期 **{ai_date}** 與選擇日期 **{photo_date}** 不同")
                date_choice = st.radio("使用哪個日期？",
                    [f"照片日期 ({ai_date})", f"手選日期 ({photo_date})"],
                    key="photo_date_choice")
                final_date = ai_date if "照片" in date_choice else str(photo_date)
            elif ai_date:
                final_date = ai_date
                st.info(f"照片辨識日期：{ai_date}")
            elif results["parsed_ai_dates"] and len(results["parsed_ai_dates"]) > 1:
                st.warning(f"多張照片辨識到不同日期")
                final_date = st.selectbox("請選擇正確日期",
                    [str(photo_date)] + results["parsed_ai_dates"], key="photo_multi_date")
            else:
                final_date = str(photo_date)
                st.info(f"使用選擇日期：{photo_date}")

            st.markdown(f"**匹配成功 {len(matched)} 筆** / 未匹配 {len(unmatched)} 筆")

            if unmatched:
                with st.expander(f"⚠️ {len(unmatched)} 筆未匹配"):
                    st.dataframe(pd.DataFrame(unmatched), use_container_width=True, hide_index=True)

            if matched:
                match_df = pd.DataFrame(matched)

                st.subheader(f"📋 預覽辨識結果（日期：{final_date}）")
                st.caption("「AI 看到」是 AI 從照片讀到的原始字符，方便你比對手寫實際內容是否被正確解讀。")
                edited_match = st.data_editor(
                    match_df[["品項", "AI看到", "帳面庫存", "辨識數量"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "品項": st.column_config.TextColumn(disabled=True),
                        "AI看到": st.column_config.TextColumn("AI 看到", disabled=True,
                                                              help="AI 從照片讀到的原始手寫字符"),
                        "帳面庫存": st.column_config.NumberColumn(disabled=True, format="%.1f"),
                        "辨識數量": st.column_config.NumberColumn("盤點數量 ✏️", min_value=0, format="%.1f"),
                    },
                    height=min(len(match_df) * 35 + 38, 500),
                    key="photo_result_editor",
                )

                filled_count = edited_match["辨識數量"].notna().sum()
                st.caption(f"有效數量 {filled_count} / {len(edited_match)}")

                col_save, col_clear = st.columns([3, 1])
                with col_save:
                    # 預先檢查重複 session（同 clinic + 同 session_date 且有 logs）
                    existing_session_id = None
                    existing_pids = set()
                    dup_check = sb.table("inventory_sessions").select("id").eq(
                        "clinic_id", clinic_id).eq("session_date", final_date).execute().data
                    for ds in dup_check:
                        lc = sb.table("inventory_logs").select("id, product_id").eq(
                            "session_id", ds["id"]).execute().data
                        if lc:
                            existing_session_id = ds["id"]
                            existing_pids = {l["product_id"] for l in lc}
                            break

                    if existing_session_id:
                        st.warning(f"⚠️ {final_date} 已有盤點紀錄（session_id={existing_session_id}，已盤 {len(existing_pids)} 項）。"
                                   "可選擇「合併」追加新項目並更新已存在項目，或「取消」。")

                    save_label = ("✅ 合併存檔（追加 + 更新）"
                                  if existing_session_id
                                  else "✅ 確認存檔盤點")

                    if st.button(save_label, type="primary", key="photo_save",
                                 disabled=(filled_count == 0)):
                        entries = []
                        for idx, row in edited_match.iterrows():
                            if pd.notna(row["辨識數量"]):
                                entries.append((match_df.iloc[idx]["product_id"],
                                               round(float(row["辨識數量"]), 1)))

                        if not entries:
                            st.error("沒有有效的盤點數量")
                        else:
                            try:
                                # 取得 session_id（既有或新建）
                                if existing_session_id:
                                    session_id = existing_session_id
                                else:
                                    session_resp = sb.table("inventory_sessions").insert({
                                        "clinic_id": int(clinic_id),
                                        "session_date": final_date,
                                        "operator_id": int(user["id"]),
                                        "status": "已完成",
                                        "completed_at": datetime.now().isoformat(),
                                    }).execute()
                                    session_id = session_resp.data[0]["id"]

                                # 已存在項目的 log_id map
                                existing_log_map = {}
                                if existing_session_id:
                                    el = sb.table("inventory_logs").select(
                                        "id, product_id"
                                    ).eq("session_id", existing_session_id).execute().data
                                    existing_log_map = {l["product_id"]: l["id"] for l in el}

                                inserted, updated = 0, 0
                                affected_pids = set()
                                for product_id, count_qty in entries:
                                    affected_pids.add(int(product_id))
                                    log_id = existing_log_map.get(int(product_id))
                                    if log_id is not None:
                                        # update 既有
                                        sb.table("inventory_logs").update({
                                            "current_count_qty": count_qty,
                                            "log_date": final_date,
                                        }).eq("id", log_id).execute()
                                        updated += 1
                                    else:
                                        # 新增（last/restock/consumed 由 recalc 重算）
                                        # upsert：若該品項在此 session 其實已有 log（existing_log_map 漏抓/畫面快照過期），
                                        # 改為更新而非重複新增，從根本杜絕重複 log
                                        sb.table("inventory_logs").upsert({
                                            "session_id": int(session_id),
                                            "product_id": int(product_id),
                                            "clinic_id": int(clinic_id),
                                            "last_count_qty": 0,
                                            "restock_qty_since_last": 0,
                                            "current_count_qty": count_qty,
                                            "consumed_qty": 0,
                                            "log_date": final_date,
                                        }, on_conflict="session_id,product_id").execute()
                                        inserted += 1

                                # 重算所有受影響品項
                                for pid in affected_pids:
                                    recalc_consumed_for_product(pid, int(clinic_id))

                                # 更新辨識報告 saved 狀態
                                rid = results.get("report_id") if results else None
                                if rid:
                                    try:
                                        sb.table("photo_recognition_reports").update({
                                            "saved": True,
                                            "saved_at": datetime.now().isoformat(),
                                            "saved_inserted": int(inserted),
                                            "saved_updated": int(updated),
                                            "session_date": final_date,
                                        }).eq("id", rid).execute()
                                    except Exception:
                                        pass

                                st.session_state.photo_results = None
                                msg = []
                                if inserted > 0:
                                    msg.append(f"新增 {inserted} 筆")
                                if updated > 0:
                                    msg.append(f"更新 {updated} 筆")
                                st.success(f"照片盤點完成！" + "、".join(msg) + f"（日期：{final_date}）")
                                st.balloons()

                            except Exception as e:
                                st.error(f"存檔失敗：{e}")

                with col_clear:
                    if st.button("🗑️ 清除結果", key="photo_clear"):
                        st.session_state.photo_results = None
                        st.rerun()

        # ── 辨識報告歷史 ──
        st.divider()
        with st.expander("📊 辨識報告歷史（成功/失敗統計與失敗名單）", expanded=False):
            try:
                reports = sb.table("photo_recognition_reports").select(
                    "id, recognized_at, session_date, photo_count, item_count, "
                    "matched_count, unmatched_count, unmatched_items, ai_model, "
                    "double_check, saved, saved_at, saved_inserted, saved_updated"
                ).eq("clinic_id", clinic_id).order(
                    "recognized_at", desc=True
                ).limit(30).execute().data
            except Exception as e:
                reports = None
                st.info(f"尚未建立報告表，請先在 Supabase SQL Editor 執行 migration_v3_photo_reports.sql。錯誤：{e}")

            if reports is not None:
                if not reports:
                    st.caption("目前沒有辨識報告。執行一次拍照盤點後就會出現紀錄。")
                else:
                    for rpt in reports:
                        rec_at = rpt["recognized_at"]
                        try:
                            rec_dt = datetime.fromisoformat(rec_at.replace("Z", "+00:00"))
                            ts = rec_dt.strftime("%Y/%m/%d %H:%M")
                        except Exception:
                            ts = rec_at
                        saved_tag = "✅ 已存檔" if rpt.get("saved") else "⚠️ 未存檔"
                        sd = rpt.get("session_date") or "-"
                        title = (
                            f"{ts} ｜ {saved_tag} ｜ 盤點日 {sd} ｜ "
                            f"{rpt['photo_count']} 張 ｜ 成功 {rpt['matched_count']} / 失敗 {rpt['unmatched_count']}"
                        )
                        with st.container(border=True):
                            st.markdown(f"**{title}**")
                            meta = []
                            meta.append(f"模型：{rpt.get('ai_model') or '-'}")
                            meta.append("雙重檢視：開" if rpt.get("double_check") else "雙重檢視：關")
                            if rpt.get("saved"):
                                meta.append(f"新增 {rpt.get('saved_inserted') or 0} 筆")
                                meta.append(f"更新 {rpt.get('saved_updated') or 0} 筆")
                            st.caption(" ｜ ".join(meta))
                            unmatched_list = rpt.get("unmatched_items") or []
                            if unmatched_list:
                                df_un = pd.DataFrame(unmatched_list)
                                st.dataframe(df_un, use_container_width=True, hide_index=True)
                            else:
                                st.caption("無失敗項目")

    # ── 執行盤點 ──
    with tab_do:
        inv_date = st.date_input("盤點日期（可選過去日期回填）", value=date.today(), key="inv_date")

        # 檢查重複日期
        existing_sessions = sb.table("inventory_sessions").select("id").eq(
            "clinic_id", clinic_id).eq("session_date", str(inv_date)).execute().data
        # 只檢查有 logs 的 sessions
        has_logs = False
        for es in existing_sessions:
            log_check = sb.table("inventory_logs").select("id").eq("session_id", es["id"]).limit(1).execute().data
            if log_check:
                has_logs = True
                break

        if has_logs:
            st.warning(f"⚠️ {inv_date} 已有盤點紀錄，請至「盤點歷史」修改。")
        else:
            categories = load_categories()
            cat_filter = st.selectbox("分類（建議逐分類盤點）", ["全部"] + [c["name"] for c in categories], key="inv_cat")

            # per-clinic 啟用篩選（含 cabinet 用於排序）
            cs_data = sb.table("clinic_stock").select(
                "product_id, current_stock, is_active, cabinet"
            ).eq("clinic_id", clinic_id).execute().data
            cs_map = {s["product_id"]: s for s in cs_data}

            products = sb.table("products").select(
                "id, name, category_id, categories(name), units(name)"
            ).execute().data
            products = sort_products_by_bopomofo(products, cs_map=cs_map)
            products = [p for p in products if cs_map.get(p["id"], {}).get("is_active", True)]

            stock_map = {s["product_id"]: float(s["current_stock"]) for s in cs_data}

            filtered = products
            if cat_filter != "全部":
                filtered = [p for p in filtered if p["categories"]["name"] == cat_filter]

            if not filtered:
                st.info("沒有符合條件的品項")
            else:
                edit_data = [{
                    "product_id": p["id"], "品項名稱": p["name"], "分類": p["categories"]["name"],
                    "單位": p["units"]["name"], "帳面庫存": stock_map.get(p["id"], 0), "盤點數量": None,
                } for p in filtered]

                df = pd.DataFrame(edit_data)
                st.markdown(f"**{selected_clinic}** — 盤點日期：**{inv_date}** — 共 {len(df)} 個品項")

                edited_df = st.data_editor(
                    df[["品項名稱", "分類", "單位", "帳面庫存", "盤點數量"]],
                    use_container_width=True, hide_index=True, num_rows="fixed",
                    column_config={
                        "品項名稱": st.column_config.TextColumn(disabled=True),
                        "分類": st.column_config.TextColumn(disabled=True),
                        "單位": st.column_config.TextColumn(disabled=True),
                        "帳面庫存": st.column_config.NumberColumn(disabled=True, format="%.1f"),
                        "盤點數量": st.column_config.NumberColumn("盤點數量 ✏️", min_value=0, format="%.1f"),
                    },
                    height=min(len(df) * 35 + 38, 600),
                    key="inv_editor",
                )

                filled = edited_df["盤點數量"].notna().sum()
                st.caption(f"已填 {filled} / {len(edited_df)}")

                if st.button("✅ 確認盤點", type="primary", disabled=(filled == 0)):
                    # 先收集所有有填數量的項目，避免建立空批次
                    entries = []
                    for idx, row in edited_df.iterrows():
                        if pd.notna(row["盤點數量"]):
                            entries.append((idx, round(float(row["盤點數量"]), 1)))

                    if not entries:
                        st.error("沒有填入任何盤點數量，請重新填寫後再送出。")
                    else:
                        try:
                            # 取得每個品項的最新盤點
                            last_logs = fetch_all(sb.table("inventory_logs").select(
                                "product_id, current_count_qty, log_date"
                            ).eq("clinic_id", clinic_id).order("log_date", desc=True))

                            last_count_map, last_date_map = {}, {}
                            for log in last_logs:
                                pid = log["product_id"]
                                if pid not in last_count_map:
                                    last_count_map[pid] = float(log["current_count_qty"])
                                    last_date_map[pid] = log["log_date"]

                            all_tx = fetch_all(sb.table("transactions").select(
                                "product_id, change_qty, tx_date"
                            ).eq("clinic_id", clinic_id))

                            tx_by_pid = defaultdict(list)
                            for tx in all_tx:
                                tx_by_pid[tx["product_id"]].append(tx)

                            # 資料都準備好了，才建立 session
                            session_resp = sb.table("inventory_sessions").insert({
                                "clinic_id": clinic_id, "session_date": str(inv_date),
                                "operator_id": user["id"], "status": "已完成",
                                "completed_at": datetime.now().isoformat(),
                            }).execute()
                            session_id = session_resp.data[0]["id"]

                            logs_to_insert, results = [], []

                            for idx, count_qty in entries:
                                product_id = int(df.iloc[idx]["product_id"])
                                last_qty = round(float(last_count_map.get(product_id, stock_map.get(product_id, 0))), 1)
                                last_dt = last_date_map.get(product_id, "1900-01-01")

                                restock_sum = round(sum(
                                    float(t["change_qty"]) for t in tx_by_pid.get(product_id, [])
                                    if last_dt < t["tx_date"] <= str(inv_date)
                                ), 1)
                                consumed = round(last_qty + restock_sum - count_qty, 1)

                                logs_to_insert.append({
                                    "session_id": int(session_id), "product_id": product_id,
                                    "clinic_id": int(clinic_id),
                                    "last_count_qty": last_qty,
                                    "restock_qty_since_last": restock_sum,
                                    "current_count_qty": count_qty,
                                    "consumed_qty": consumed,
                                    "log_date": str(inv_date),
                                })

                                results.append({
                                    "品項": edited_df.iloc[idx]["品項名稱"],
                                    "上次盤點": last_qty,
                                    "期間進貨": restock_sum, "本次盤點": count_qty, "耗用量": consumed,
                                })

                            # 先寫入 logs，再重算每個品項的所有耗用（含 last/restock/consumed）
                            # upsert：同一 session 同品項只保留一筆（依 DB 唯一限制），避免重複送出造成重複 log
                            sb.table("inventory_logs").upsert(
                                logs_to_insert, on_conflict="session_id,product_id"
                            ).execute()

                            for entry in logs_to_insert:
                                recalc_consumed_for_product(int(entry["product_id"]), int(clinic_id))

                            st.success(f"盤點完成！共 {len(results)} 個品項（日期：{inv_date}）")
                            if results:
                                st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

                        except Exception as e:
                            st.error(f"盤點失敗：{e}")

    # ── 盤點歷史 ──
    with tab_history:
        sessions = sb.table("inventory_sessions").select(
            "id, session_date, status, users(display_name)"
        ).eq("clinic_id", clinic_id).order("session_date", desc=True).limit(20).execute().data

        if not sessions:
            st.info("尚無盤點紀錄")
        else:
            # per-clinic 啟用（含 cabinet 用於排序）
            cs_active = sb.table("clinic_stock").select(
                "product_id, current_stock, is_active, cabinet"
            ).eq("clinic_id", clinic_id).execute().data
            active_pids = {s["product_id"] for s in cs_active if s.get("is_active", True)}
            stock_map_hist = {s["product_id"]: float(s["current_stock"]) for s in cs_active}
            cs_active_map = {s["product_id"]: s for s in cs_active}

            # 載入全品項（用於顯示完整清單）
            all_products = sb.table("products").select(
                "id, name, category_id, units(name)"
            ).execute().data
            all_products = sort_products_by_bopomofo(all_products, cs_map=cs_active_map)
            all_products = [p for p in all_products if p["id"] in active_pids]

            for s in sessions:
                operator = s["users"]["display_name"] if s.get("users") else "-"
                with st.expander(f"📋 {s['session_date']}（{operator}）", expanded=False):
                    # 載入此 session 已有的 logs
                    logs = sb.table("inventory_logs").select(
                        "id, product_id, last_count_qty, restock_qty_since_last, "
                        "current_count_qty, consumed_qty"
                    ).eq("session_id", s["id"]).execute().data
                    log_by_pid = {l["product_id"]: l for l in logs}

                    counted = len(logs)
                    total = len(all_products)
                    st.caption(f"已盤 {counted} / {total} 個品項")

                    # 組裝全品項表格（已盤的顯示數量，未盤的留空）
                    log_rows = []
                    row_meta = []  # (product_id, log_id_or_None)
                    for p in all_products:
                        pid = p["id"]
                        log = log_by_pid.get(pid)
                        if log:
                            log_rows.append({
                                "品項": p["name"],
                                "分類": "",
                                "單位": p["units"]["name"],
                                "帳面庫存": stock_map_hist.get(pid, 0),
                                "盤點數量": float(log["current_count_qty"]) if log["current_count_qty"] is not None else None,
                            })
                            row_meta.append((pid, log["id"]))
                        else:
                            log_rows.append({
                                "品項": p["name"],
                                "分類": "",
                                "單位": p["units"]["name"],
                                "帳面庫存": stock_map_hist.get(pid, 0),
                                "盤點數量": None,
                            })
                            row_meta.append((pid, None))

                    log_df = pd.DataFrame(log_rows)

                    edited_log_df = st.data_editor(
                        log_df,
                        use_container_width=True, hide_index=True,
                        column_config={
                            "品項": st.column_config.TextColumn(disabled=True),
                            "分類": st.column_config.TextColumn(disabled=True),
                            "單位": st.column_config.TextColumn(disabled=True),
                            "帳面庫存": st.column_config.NumberColumn(disabled=True, format="%.1f"),
                            "盤點數量": st.column_config.NumberColumn("盤點數量 ✏️", min_value=0, format="%.1f"),
                        },
                        height=min(len(log_df) * 35 + 38, 600),
                        key=f"hist_editor_{s['id']}",
                    )

                    col_save, col_del = st.columns([3, 1])
                    with col_save:
                        if st.button("💾 儲存修改", key=f"hist_save_{s['id']}", type="primary"):
                            # 載入進貨資料
                            all_tx = fetch_all(sb.table("transactions").select(
                                "product_id, change_qty, tx_date"
                            ).eq("clinic_id", clinic_id))
                            tx_by_pid = defaultdict(list)
                            for tx in all_tx:
                                tx_by_pid[tx["product_id"]].append(tx)

                            # 載入每品項最新盤點（排除當前 session）
                            prev_logs = fetch_all(sb.table("inventory_logs").select(
                                "product_id, current_count_qty, log_date"
                            ).eq("clinic_id", clinic_id).neq(
                                "session_id", s["id"]
                            ).order("log_date", desc=True))
                            prev_map = {}
                            prev_date_map = {}
                            for pl in prev_logs:
                                pid = pl["product_id"]
                                if pid not in prev_map:
                                    prev_map[pid] = float(pl["current_count_qty"])
                                    prev_date_map[pid] = pl["log_date"]

                            updated, inserted = 0, 0
                            for idx in range(len(edited_log_df)):
                                new_val = edited_log_df.iloc[idx]["盤點數量"]
                                old_val = log_df.iloc[idx]["盤點數量"]
                                pid, log_id = row_meta[idx]

                                if pd.isna(new_val):
                                    continue

                                new_qty = round(float(new_val), 1)

                                # 計算耗用
                                last_qty = round(float(prev_map.get(pid, stock_map_hist.get(pid, 0))), 1)
                                last_dt = prev_date_map.get(pid, "1900-01-01")
                                restock_sum = round(sum(
                                    float(t["change_qty"]) for t in tx_by_pid.get(pid, [])
                                    if last_dt < t["tx_date"] <= s["session_date"]
                                ), 1)
                                consumed = round(last_qty + restock_sum - new_qty, 1)

                                if log_id is not None:
                                    # 已有 log — 更新
                                    if pd.notna(old_val) and round(float(old_val), 1) == new_qty:
                                        continue
                                    sb.table("inventory_logs").update({
                                        "current_count_qty": new_qty,
                                        "consumed_qty": consumed,
                                    }).eq("id", log_id).execute()
                                    updated += 1
                                else:
                                    # 新增 log
                                    # upsert：若畫面快照過期、該品項其實已有 log，改為更新而非重複新增
                                    sb.table("inventory_logs").upsert({
                                        "session_id": int(s["id"]),
                                        "product_id": int(pid),
                                        "clinic_id": int(clinic_id),
                                        "last_count_qty": last_qty,
                                        "restock_qty_since_last": restock_sum,
                                        "current_count_qty": new_qty,
                                        "consumed_qty": consumed,
                                        "log_date": s["session_date"],
                                    }, on_conflict="session_id,product_id").execute()
                                    inserted += 1

                                # 重算所有耗用量（含 last/restock/consumed）
                                recalc_consumed_for_product(int(pid), int(clinic_id))

                            msg = []
                            if updated > 0:
                                msg.append(f"修改 {updated} 筆")
                            if inserted > 0:
                                msg.append(f"新增 {inserted} 筆")
                            if msg:
                                st.success("已" + "、".join(msg))
                                st.rerun()
                            else:
                                st.info("沒有變更")

                    with col_del:
                        if user["role"] == "admin":
                            if st.button("🗑️ 刪除整筆", key=f"hist_del_{s['id']}", type="secondary"):
                                try:
                                    # 收集受影響的品項
                                    affected_pids = set(l["product_id"] for l in logs)

                                    # 先刪除 logs 和 session
                                    sb.table("inventory_logs").delete().eq("session_id", s["id"]).execute()
                                    sb.table("inventory_sessions").delete().eq("id", s["id"]).execute()

                                    # 再重算每個受影響品項的所有耗用量
                                    for pid in affected_pids:
                                        recalc_consumed_for_product(int(pid), int(clinic_id))

                                    st.success("已刪除整筆盤點")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"刪除失敗：{e}")


# ══════════════════════════════════════════════
#  4. 品項管理（per-clinic 廠牌/啟用）
# ══════════════════════════════════════════════

def page_items():
    st.header("📋 品項管理")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")

    if selected_clinic == "合併檢視":
        st.info("請先選擇單一診所來管理品項。")
        return

    clinic_id = get_clinic_id(selected_clinic)
    if not clinic_id:
        st.error("找不到該診所")
        return

    brands = load_brands()
    categories = load_categories()
    units = load_units()

    brand_map = {b["id"]: b["name"] for b in brands}
    unit_map = {u["id"]: u["name"] for u in units}

    sb = get_supabase_client()

    # 載入 per-clinic 資料
    cs_data = sb.table("clinic_stock").select(
        "product_id, brand1_id, brand2_id, is_active, cabinet"
    ).eq("clinic_id", clinic_id).execute().data
    cs_map = {s["product_id"]: s for s in cs_data}

    # 依（分類 → 首字注音 → 櫃位 → 全字注音）排序
    products = sort_products_by_bopomofo(load_products(), cs_map=cs_map)

    tab_list, tab_add = st.tabs(["📊 品項清單", "➕ 新增品項"])

    with tab_list:
        st.caption(f"📍 顯示 **{selected_clinic}** 的廠牌與啟用狀態")

        col1, col2, col3 = st.columns([2, 2, 3])
        with col1:
            cat_filter = st.selectbox("分類", ["全部"] + [c["name"] for c in categories], key="item_cat")
        with col2:
            active_filter = st.selectbox("狀態", ["啟用中", "已停用", "全部"], key="item_active")
        with col3:
            search = st.text_input("搜尋", placeholder="中文 或 注音首碼", key="item_search")

        all_names = tuple(p["name"] for p in products)
        key_index = build_bopomofo_index(all_names)

        rows, product_ids = [], []
        cat_names = [c["name"] for c in categories]
        unit_names = [u["name"] for u in units]
        brand_names = ["-"] + [b["name"] for b in brands]

        for p in products:
            cs = cs_map.get(p["id"], {})
            is_active = cs.get("is_active", True)

            if cat_filter != "全部" and p["categories"]["name"] != cat_filter:
                continue
            if active_filter == "啟用中" and not is_active:
                continue
            if active_filter == "已停用" and is_active:
                continue
            if search and not match_search(p["name"], key_index.get(p["name"], ("", "")), search):
                continue

            rows.append({
                "品項名稱": p["name"], "分類": p["categories"]["name"], "單位": p["units"]["name"],
                "櫃位": cs.get("cabinet") or "",
                "第一廠牌": brand_map.get(cs.get("brand1_id"), "-"),
                "第二廠牌": brand_map.get(cs.get("brand2_id"), "-"),
                "規格": p["spec_note"] or "-", "啟用": is_active,
            })
            product_ids.append(p["id"])

        if rows:
            df = pd.DataFrame(rows)
            can_edit = st.session_state.user["role"] in ("admin", "manager")

            if can_edit:
                edited_df = st.data_editor(
                    df, use_container_width=True, hide_index=True,
                    height=min(len(df) * 35 + 38, 600),
                    column_config={
                        "品項名稱": st.column_config.TextColumn(),
                        "分類": st.column_config.SelectboxColumn(options=cat_names),
                        "單位": st.column_config.SelectboxColumn(options=unit_names),
                        "櫃位": st.column_config.TextColumn(),
                        "第一廠牌": st.column_config.SelectboxColumn(options=brand_names),
                        "第二廠牌": st.column_config.SelectboxColumn(options=brand_names),
                        "規格": st.column_config.TextColumn(),
                        "啟用": st.column_config.CheckboxColumn(),
                    },
                    key="items_editor",
                )

                if st.button("💾 儲存修改", type="primary"):
                    updated = 0
                    for idx in range(len(edited_df)):
                        old, new = df.iloc[idx], edited_df.iloc[idx]
                        pid = product_ids[idx]

                        # 共用欄位 → 更新 products 表
                        product_changes = {}
                        if old["品項名稱"] != new["品項名稱"]:
                            product_changes["name"] = new["品項名稱"]
                        if old["分類"] != new["分類"]:
                            product_changes["category_id"] = next(c["id"] for c in categories if c["name"] == new["分類"])
                        if old["單位"] != new["單位"]:
                            product_changes["unit_id"] = next(u["id"] for u in units if u["name"] == new["單位"])
                        if old["規格"] != new["規格"]:
                            product_changes["spec_note"] = new["規格"] if new["規格"] != "-" else None

                        if product_changes:
                            try:
                                sb.table("products").update(product_changes).eq("id", pid).execute()
                                updated += 1
                            except Exception as e:
                                err_msg = str(e)
                                if "duplicate" in err_msg.lower() or "unique" in err_msg.lower():
                                    st.error(f"品項「{new['品項名稱']}」名稱重複，無法更新")
                                else:
                                    st.error(f"更新「{old['品項名稱']}」失敗：{e}")
                                continue

                        # per-clinic 欄位 → 更新 clinic_stock 表
                        cs_changes = {}
                        if old.get("櫃位", "") != new.get("櫃位", ""):
                            cs_changes["cabinet"] = new["櫃位"] if new["櫃位"] else None
                        if old["第一廠牌"] != new["第一廠牌"]:
                            cs_changes["brand1_id"] = next((b["id"] for b in brands if b["name"] == new["第一廠牌"]), None)
                        if old["第二廠牌"] != new["第二廠牌"]:
                            cs_changes["brand2_id"] = next((b["id"] for b in brands if b["name"] == new["第二廠牌"]), None)
                        if old["啟用"] != new["啟用"]:
                            cs_changes["is_active"] = bool(new["啟用"])

                        if cs_changes:
                            invalidate_stock_cache()
                            sb.table("clinic_stock").update(cs_changes).eq(
                                "product_id", pid).eq("clinic_id", clinic_id).execute()
                            updated += 1

                    if updated > 0:
                        st.success(f"已更新 {updated} 個品項")
                        load_products.clear()
                    else:
                        st.info("沒有變更")

                st.caption(f"共 {len(df)} 個品項（停用品項=軟刪除，取消勾選「啟用」即可）— {selected_clinic}")
            else:
                st.dataframe(style_banded(df), use_container_width=True, hide_index=True, height=min(len(df) * 35 + 38, 600))
                st.caption(f"共 {len(df)} 個品項 — {selected_clinic}")

    with tab_add:
        if st.session_state.user["role"] not in ("admin", "manager"):
            st.warning("僅管理者可新增品項")
            return

        with st.form("add_product_form"):
            st.subheader("新增品項")
            col1, col2 = st.columns(2)
            with col1:
                new_name = st.text_input("品項名稱 *")
                new_cat = st.selectbox("分類 *", [c["name"] for c in categories])
                selected_cat = next((c for c in categories if c["name"] == new_cat), None)
            with col2:
                unit_options = [u["name"] for u in units]
                default_idx = 0
                if selected_cat and selected_cat["default_unit_id"]:
                    dn = unit_map.get(selected_cat["default_unit_id"], "罐")
                    if dn in unit_options:
                        default_idx = unit_options.index(dn)
                new_unit = st.selectbox("單位", unit_options, index=default_idx)
                new_spec = st.text_input("規格", value=selected_cat["default_spec_note"] or "" if selected_cat else "")

            col3, col4 = st.columns(2)
            with col3:
                bo = ["-"] + [b["name"] for b in brands]
                new_b1 = st.selectbox("第一廠牌（叫貨首選）", bo)
            with col4:
                new_b2 = st.selectbox("第二廠牌（缺藥備選，可留空）", bo, key="add_b2")

            if st.form_submit_button("新增品項", type="primary", use_container_width=True):
                if not new_name.strip():
                    st.error("請輸入品項名稱")
                else:
                    try:
                        b1_id = next((b["id"] for b in brands if b["name"] == new_b1), None)
                        b2_id = next((b["id"] for b in brands if b["name"] == new_b2), None)

                        resp = sb.table("products").insert({
                            "name": new_name.strip(),
                            "category_id": next(c["id"] for c in categories if c["name"] == new_cat),
                            "unit_id": next(u["id"] for u in units if u["name"] == new_unit),
                            "brand1_id": b1_id, "brand2_id": b2_id,
                            "spec_note": new_spec or None, "is_active": True,
                        }).execute()
                        pid = resp.data[0]["id"]

                        # 為兩間診所建立 clinic_stock（含廠牌）
                        for c in sb.table("clinics").select("id").execute().data:
                            invalidate_stock_cache()
                            sb.table("clinic_stock").insert({
                                "product_id": pid, "clinic_id": c["id"],
                                "current_stock": 0,
                                "brand1_id": b1_id, "brand2_id": b2_id,
                                "is_active": True,
                            }).execute()

                        st.success(f"已新增：{new_name}")
                        load_products.clear()
                    except Exception as e:
                        if "duplicate" in str(e).lower():
                            st.error(f"品項「{new_name}」已存在")
                        else:
                            st.error(f"新增失敗：{e}")


# ══════════════════════════════════════════════
#  5. 數據分析
# ══════════════════════════════════════════════

def page_analytics():
    st.header("📊 數據分析")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")
    sb = get_supabase_client()

    if selected_clinic == "合併檢視":
        clinic_ids = [1, 2]
    else:
        cid = get_clinic_id(selected_clinic)
        clinic_ids = [cid] if cid else []

    # 每診所獨立統計（避免兩診所混算 avg；首盤 placeholder 由 build_recent_consumed_map 自動排除）
    consumed_per_clinic = {}
    product_info = {}
    has_any = False
    gp = load_global_params()
    cat_params = build_category_params(load_categories())
    k_dates, n_max = avg_window_dates(gp["avg_n"]), gp["avg_n"]
    for cid in clinic_ids:
        clinic_logs, win_start = load_recent_logs(
            int(cid), k_dates,
            "id, product_id, consumed_qty, log_date, products(name, category_id, categories(name), units(name))",
        )
        if clinic_logs:
            has_any = True
        consumed_per_clinic[cid] = build_recent_consumed_map(clinic_logs, n=n_max, window_start=win_start)
        for log in clinic_logs:
            pid = log["product_id"]
            if pid not in product_info:
                product_info[pid] = {
                    "name": log["products"]["name"],
                    "category": log["products"]["categories"]["name"],
                    "unit": log["products"]["units"]["name"],
                }

    if not has_any:
        st.info("尚無盤點資料。")
        return

    # 排名/常用統計用：合併兩診所
    product_consumed_all = defaultdict(list)
    for cid in clinic_ids:
        for pid, vals in consumed_per_clinic[cid].items():
            product_consumed_all[pid].extend(vals[:n_max])

    # ── 診斷區（admin 限定，用於排查耗用/建議叫貨計算問題）──
    user_role = st.session_state.user.get("role")
    if user_role == "admin":
        with st.expander("🔧 維護工具：一鍵重算 inventory_logs"):
            st.caption("用途：清理因 transactions 修改/刪除或改錯存檔造成的髒資料。"
                       "會重算每個品項所有 inventory_logs 的 last/restock/consumed，第一次盤點 consumed 設為 0。")
            recalc_scope = st.radio(
                "範圍",
                ["僅當前診所", "全部診所（澤豐+澤沛）"],
                key="recalc_scope",
                horizontal=True,
            )
            if st.button("🔄 開始重算", type="primary", key="recalc_btn"):
                with st.spinner("重算中⋯"):
                    if recalc_scope == "僅當前診所":
                        if selected_clinic == "合併檢視":
                            st.warning("請先切換到單一診所")
                        else:
                            cid = get_clinic_id(selected_clinic)
                            n = recalc_all_consumed_in_clinic(cid)
                            st.success(f"完成：{selected_clinic} 已重算 {n} 個品項")
                    else:
                        total = 0
                        for cn in ("澤豐", "澤沛"):
                            cid = get_clinic_id(cn)
                            if cid:
                                n = recalc_all_consumed_in_clinic(cid)
                                total += n
                                st.write(f"・{cn}：{n} 個品項")
                        st.success(f"完成：共重算 {total} 個品項")

        with st.expander("🔍 診斷：列出指定品項的所有盤點紀錄"):
            st.caption(f"目前診所：{selected_clinic}（clinic_ids={clinic_ids}）")
            diag_products = sb.table("products").select("id, name").execute().data
            diag_products = sorted(diag_products, key=lambda p: get_bopomofo_sort_key(p["name"]))
            diag_options = {p["name"]: p["id"] for p in diag_products}
            diag_sel = st.selectbox("品項", list(diag_options.keys()), key="diag_pid_select")
            diag_pid = diag_options[diag_sel]

            for cid in clinic_ids:
                clinic_label = "澤豐" if cid == 1 else "澤沛" if cid == 2 else f"id={cid}"
                st.markdown(f"**【{clinic_label}】 inventory_logs**")
                d_logs = sb.table("inventory_logs").select(
                    "id, session_id, log_date, last_count_qty, "
                    "restock_qty_since_last, current_count_qty, consumed_qty"
                ).eq("product_id", diag_pid).eq("clinic_id", cid).order(
                    "log_date", desc=False
                ).execute().data
                if d_logs:
                    st.dataframe(pd.DataFrame(d_logs), use_container_width=True, hide_index=True)
                    st.caption(f"共 {len(d_logs)} 筆")
                else:
                    st.info("無紀錄")

                st.markdown(f"**【{clinic_label}】 transactions**")
                d_tx = sb.table("transactions").select(
                    "id, tx_date, tx_type, change_qty, note"
                ).eq("product_id", diag_pid).eq("clinic_id", cid).order(
                    "tx_date", desc=False
                ).execute().data
                if d_tx:
                    st.dataframe(pd.DataFrame(d_tx), use_container_width=True, hide_index=True)
                    st.caption(f"共 {len(d_tx)} 筆")
                else:
                    st.info("無紀錄")

    tab_reorder, tab_ranking, tab_cabinet = st.tabs(["📦 建議叫貨", "🏆 常用排名", "🗄️ 櫃位分類"])

    with tab_reorder:
        stock_data = []
        for cid in clinic_ids:
            resp = sb.table("clinic_stock").select(
                "product_id, current_stock, clinic_id, cabinet"
            ).eq("clinic_id", cid).execute()
            stock_data.extend(resp.data)

        stock_map = {(s["product_id"], s["clinic_id"]): s for s in stock_data}

        reorder_rows = []
        for cid in clinic_ids:
            for pid, consumed_list in consumed_per_clinic[cid].items():
                info = product_info.get(pid)
                if not info:
                    continue
                avg = calc_avg_consumption(consumed_list, max_n=n_max)
                if avg <= 0:
                    continue
                cs = stock_map.get((pid, cid)) or {}
                current = float(cs.get("current_stock") or 0)
                multiplier, safety = effective_params(cat_params, info["category"], cs.get("cabinet"))
                suggested = suggest_order_qty(avg, current, multiplier, safety)
                if suggested > 0:
                    reorder_rows.append({
                        "診所": "澤豐" if cid == 1 else "澤沛",
                        "品項": info["name"], "分類": info["category"],
                        "目前庫存": current, "平均耗用": round(avg, 1),
                        "建議叫貨": suggested,
                    })

        if reorder_rows:
            reorder_df = pd.DataFrame(reorder_rows).sort_values(["診所", "分類"]).reset_index(drop=True)
            st.dataframe(
                style_banded(reorder_df,
                             bold_cols=["平均耗用", "建議叫貨"],
                             big_bold_col="目前庫存"),
                use_container_width=True, hide_index=True,
            )
        else:
            st.success("所有品項庫存充足")

    with tab_ranking:
        categories = load_categories()
        rank_cat = st.selectbox("分類", ["全部"] + [c["name"] for c in categories], key="rank_cat")

        ranking = []
        for pid, consumed_list in product_consumed_all.items():
            info = product_info.get(pid)
            if not info:
                continue
            if rank_cat != "全部" and info["category"] != rank_cat:
                continue
            # 排名只計正值
            positive = [v for v in consumed_list if v > 0]
            total = sum(positive)
            if total > 0:
                ranking.append({"品項": info["name"], "分類": info["category"],
                                "總耗用量": round(total, 1),
                                "平均耗用": round(calc_avg_consumption(consumed_list, max_n=n_max), 1)})

        if ranking:
            rank_df = pd.DataFrame(ranking).sort_values("總耗用量", ascending=False).reset_index(drop=True)
            st.bar_chart(rank_df.head(15).set_index("品項")[["總耗用量"]], color="#6A5ACD")
            st.dataframe(
                style_banded(rank_df, bold_cols=["總耗用量", "平均耗用"]),
                use_container_width=True, hide_index=True,
            )

    with tab_cabinet:
        if selected_clinic == "合併檢視":
            st.info("請先選擇單一診所。")
        else:
            cab_clinic_id = get_clinic_id(selected_clinic)
            cab_sb = get_supabase_client()
            cab_products = cab_sb.table("products").select(
                "id, name, category_id, categories(name), units(name)"
            ).execute().data

            cab_cs = cab_sb.table("clinic_stock").select(
                "product_id, current_stock, cabinet, is_active, brand1_id, brand2_id"
            ).eq("clinic_id", cab_clinic_id).execute().data
            cab_cs_map = {s["product_id"]: s for s in cab_cs}

            cab_brands = load_brands()
            cab_brand_map = {b["id"]: b["name"] for b in cab_brands}

            cab_rows = []
            for p in cab_products:
                cs = cab_cs_map.get(p["id"])
                if not cs or not cs.get("is_active", True):
                    continue
                cabinet_val = cs.get("cabinet") or ""
                cab_rows.append({
                    "櫃位": cabinet_val,
                    "品項": p["name"],
                    "分類": p["categories"]["name"],
                    "單位": p["units"]["name"],
                    "廠1": abbr_brand(cab_brand_map.get(cs.get("brand1_id"), "-")),
                    "廠2": abbr_brand(cab_brand_map.get(cs.get("brand2_id"), "-")),
                    "庫存": float(cs["current_stock"]),
                })

            if cab_rows:
                cab_df = pd.DataFrame(cab_rows)

                # 選分類篩選
                cab_cats = sorted(cab_df["分類"].unique())
                cab_cat_filter = st.selectbox("分類", ["全部"] + list(cab_cats), key="cab_cat")
                if cab_cat_filter != "全部":
                    cab_df = cab_df[cab_df["分類"] == cab_cat_filter]

                # 依櫃位排序
                cab_df = cab_df.sort_values(["櫃位", "品項"]).reset_index(drop=True)
                st.dataframe(
                    style_banded(cab_df, big_bold_col="庫存"),
                    use_container_width=True, hide_index=True,
                )

                # 匯出
                if st.button("📥 匯出櫃位分類表", type="primary"):
                    from openpyxl.styles import Font, Border, Side, Alignment
                    from openpyxl import Workbook
                    from openpyxl.utils import get_column_letter

                    thin_s = Side(style="thin")
                    double_s = Side(style="double")
                    border_t = Border(left=thin_s, right=thin_s, top=thin_s, bottom=thin_s)

                    wb = Workbook()
                    wb.remove(wb.active)

                    # 每分類一個 sheet，依櫃位排序
                    full_df = pd.DataFrame(cab_rows)
                    for cat_name, cat_group in full_df.groupby("分類", sort=True):
                        ws = wb.create_sheet(title=str(cat_name)[:31])
                        ws.page_setup.orientation = "portrait"
                        ws.page_setup.paperSize = ws.PAPERSIZE_A4
                        ws.page_setup.fitToWidth = 1
                        ws.page_setup.fitToHeight = 0
                        ws.sheet_properties.pageSetUpPr.fitToPage = True

                        headers = ["櫃位", "品項", "廠1", "廠2", "庫存"]
                        widths = [6, 22, 5, 5, 7]
                        for ci, (h, w) in enumerate(zip(headers, widths), 1):
                            cell = ws.cell(row=1, column=ci, value=h)
                            cell.font = Font(bold=True, size=11)
                            cell.border = border_t
                            cell.alignment = Alignment(horizontal="center")
                            ws.column_dimensions[get_column_letter(ci)].width = w

                        sorted_group = cat_group.sort_values(["櫃位", "品項"]).reset_index(drop=True)
                        prev_cab = None
                        row_num = 2
                        for _, r in sorted_group.iterrows():
                            cur_cab = r["櫃位"]
                            # 櫃位切換 → 上一行加雙線
                            if prev_cab is not None and cur_cab != prev_cab and row_num > 2:
                                for ci in range(1, len(headers) + 1):
                                    cell = ws.cell(row=row_num - 1, column=ci)
                                    cell.border = Border(left=thin_s, right=thin_s, top=thin_s, bottom=double_s)

                            show_cab = cur_cab if cur_cab != prev_cab else ""
                            prev_cab = cur_cab

                            vals = [show_cab, r["品項"], r["廠1"], r["廠2"], r["庫存"]]
                            for ci, v in enumerate(vals, 1):
                                cell = ws.cell(row=row_num, column=ci, value=v)
                                cell.font = Font(size=11)
                                cell.border = border_t
                                if ci != 2:
                                    cell.alignment = Alignment(horizontal="center")
                            row_num += 1

                    buf = io.BytesIO()
                    wb.save(buf)
                    buf.seek(0)
                    st.download_button("📥 下載", data=buf.getvalue(),
                        file_name=f"櫃位分類_{selected_clinic}_{date.today()}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True)
            else:
                st.info("尚無品項資料")


# ══════════════════════════════════════════════
#  6. 叫貨出表（per-clinic 廠牌）
# ══════════════════════════════════════════════

ORDER_EDIT_COLS = ["勾選", "品項", "平均耗用", "最近耗用", "目前庫存", "櫃位", "建議數量", "數量",
                   "廠牌1", "✓1", "廠牌2", "✓2", "其他廠牌"]
ORDER_BRAND_COLS = ("✓1", "✓2", "其他廠牌")


def _apply_order_edits(ed_key: str, index_labels: list) -> None:
    """步驟一 data_editor 的 on_change：把編輯寫回 session_state.order_df，並處理廠牌三選一。
    勾 ✓1 → 取消 ✓2、清空其他廠牌；勾 ✓2 → 取消 ✓1、清空其他廠牌；選其他廠牌 → 取消 ✓1、✓2。
    廠牌欄有變動時提高 order_ver，讓表格以更新後的資料重建（畫面才看得到自動取消）。"""
    state = st.session_state.get(ed_key) or {}
    edited_rows = state.get("edited_rows") or {}
    if not edited_rows:
        return
    df = st.session_state.order_df
    brand_touched = False
    for pos, changes in edited_rows.items():
        try:
            label = index_labels[int(pos)]
        except (IndexError, ValueError, TypeError):
            continue
        for col, val in changes.items():
            if col not in df.columns:
                continue
            if col == "其他廠牌" and (val is None or not str(val).strip()):
                val = ORDER_NO_OTHER
            if col == "✓1" and val:
                df.loc[label, "✓2"] = False
                df.loc[label, "其他廠牌"] = ORDER_NO_OTHER
            elif col == "✓2" and val:
                df.loc[label, "✓1"] = False
                df.loc[label, "其他廠牌"] = ORDER_NO_OTHER
            elif col == "其他廠牌" and val != ORDER_NO_OTHER:
                df.loc[label, "✓1"] = False
                df.loc[label, "✓2"] = False
            if col in ORDER_BRAND_COLS:
                brand_touched = True
            if col == "數量" and val is None:
                val = 0.0
            df.loc[label, col] = val
    st.session_state.order_df = df
    if brand_touched:
        st.session_state.order_ver = st.session_state.get("order_ver", 0) + 1


def page_order():
    st.header("🛒 叫貨出表")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")
    if selected_clinic == "合併檢視":
        st.info("請先選擇單一診所。")
        return

    clinic_id = get_clinic_id(selected_clinic)
    if not clinic_id:
        return

    gp = load_global_params()
    cat_params = build_category_params(load_categories())
    brands = load_brands()
    brand_map = {b["id"]: b["name"] for b in brands}
    brand_keys = brand_order_key_map(brands)
    brand_options = sorted(brand_map.values(), key=lambda n: brand_keys.get(n, (1, 10 ** 9)))

    try:
        cs_data, products = load_stock_snapshot(int(clinic_id))
        logs, win_start = load_recent_logs(int(clinic_id), avg_window_dates(gp["avg_n"]),
                                           "id, product_id, consumed_qty, log_date")
    except NETWORK_ERRORS:
        st.error("連線資料庫逾時（已自動重試 3 次）。請稍候按下方「重新讀取資料」，或檢查網路。")
        if st.button("🔄 重新讀取資料", key="order_retry"):
            invalidate_stock_cache()
            st.rerun()
        return
    cs_map = {s["product_id"]: s for s in cs_data}
    products = sort_products_by_bopomofo(products, cs_map=cs_map)
    consumed_data = build_recent_consumed_map(logs, n=gp["avg_n"], window_start=win_start)

    # ── 基礎清單（依 分類 → 注音/櫃位 排序）──
    base_rows = []
    for p in products:
        cs = cs_map.get(p["id"])
        if not cs or not cs.get("is_active", True):
            continue
        current = float(cs["current_stock"])
        cat_name = p["categories"]["name"]
        recent = consumed_data.get(p["id"], [])
        avg = calc_avg_consumption(recent, max_n=gp["avg_n"])
        multiplier, safety = effective_params(cat_params, cat_name, cs.get("cabinet"))
        suggested = suggest_order_qty(avg, current, multiplier, safety)
        b1 = brand_map.get(cs.get("brand1_id")) or ""
        b2 = brand_map.get(cs.get("brand2_id")) or ""
        base_rows.append({
            "_pid": p["id"], "_cat_id": p.get("category_id"), "_unit": (p.get("units") or {}).get("name", ""),
            "勾選": bool(suggested > 0 and b1 != BRAND_NO_REORDER),
            "品項": p["name"],
            "平均耗用": round(avg, 1),
            "最近耗用": round(float(recent[0]), 1) if recent else 0.0,
            "目前庫存": current,
            "櫃位": cs.get("cabinet") or "",
            "建議數量": int(suggested),
            "數量": float(suggested),
            "廠牌1": b1, "✓1": bool(b1), "廠牌2": b2, "✓2": False, "其他廠牌": ORDER_NO_OTHER,
        })
    if not base_rows:
        st.info("此診所沒有啟用中的品項。")
        return
    base_df = pd.DataFrame(base_rows)
    base_df["其他廠牌"] = base_df["其他廠牌"].astype(object)

    # 編輯內容保存在 session_state；換診所或資料更新（指紋改變）時重建並清掉編輯
    fp = hashlib.md5(repr([(r["_pid"], r["建議數量"], r["目前庫存"], r["廠牌1"], r["廠牌2"])
                           for r in base_rows]).encode("utf-8")).hexdigest()
    sig = (int(clinic_id), fp)
    if st.session_state.get("order_sig") != sig:
        st.session_state.order_sig = sig
        st.session_state.order_df = base_df
        st.session_state.order_ver = 0
    full = st.session_state.order_df

    st.subheader("步驟一：編輯叫貨清單")
    show_all = st.checkbox("顯示所有品項", value=False, key="order_show_all",
                           help="預設只列出有建議叫貨數量的品項（廠牌1 為「用完不補」者除外）")
    if show_all:
        view = full
    else:
        view = full[(full["建議數量"] > 0) & (full["廠牌1"] != BRAND_NO_REORDER)]

    if view.empty:
        st.success("所有品項庫存充足！")
        return

    st.caption("廠牌三選一：勾 ✓1 / ✓2，或在「其他廠牌」下拉選擇，其餘會自動取消；三個都沒選的品項以廠牌1 出單。"
               "建議數量 = 四捨五入(平均耗用 × 備貨倍數 − 目前庫存)。")
    view_df = view[ORDER_EDIT_COLS].copy()  # 數值 0 照常顯示（data_editor 會把缺值顯示成 "None"，不隱藏）
    ed_key = f"order_editor_{st.session_state.order_ver}"
    st.data_editor(
        view_df,
        key=ed_key, hide_index=True, use_container_width=True,
        height=min(len(view) * 35 + 38, 600),
        on_change=_apply_order_edits, args=(ed_key, list(view.index)),
        column_config={
            "勾選": st.column_config.CheckboxColumn("✅"),
            "品項": st.column_config.TextColumn(disabled=True),
            "平均耗用": st.column_config.NumberColumn(disabled=True, format="%.1f"),
            "最近耗用": st.column_config.NumberColumn(disabled=True, format="%.1f", help="最近一次盤點的耗用量"),
            "目前庫存": st.column_config.NumberColumn(disabled=True, format="%.1f"),
            "櫃位": st.column_config.TextColumn(disabled=True),
            "建議數量": st.column_config.NumberColumn(disabled=True, format="%d"),
            "數量": st.column_config.NumberColumn("數量 ✏️", min_value=0, step=1, format="%g"),
            "廠牌1": st.column_config.TextColumn(disabled=True),
            "✓1": st.column_config.CheckboxColumn("✓1", help="以廠牌1 出單"),
            "廠牌2": st.column_config.TextColumn(disabled=True),
            "✓2": st.column_config.CheckboxColumn("✓2", help="以廠牌2 出單"),
            "其他廠牌": st.column_config.SelectboxColumn(options=[ORDER_NO_OTHER] + brand_options,
                                                        help="以其他廠牌出單；選「（無）」取消"),
        },
    )
    full = st.session_state.order_df  # callback 已寫回

    st.divider()
    st.subheader("步驟二：依廠牌分組")

    sel = full[(full["勾選"]) & (full["數量"].fillna(0) > 0)].copy()
    if sel.empty:
        st.info("請勾選要叫貨的品項")
        return
    sel["廠牌"] = sel.apply(resolve_order_brand, axis=1)
    n_skip = int((sel["廠牌"] == BRAND_NO_REORDER).sum())
    sel = sel[sel["廠牌"] != BRAND_NO_REORDER]
    if n_skip:
        st.caption(f"{n_skip} 個品項的廠牌為「{BRAND_NO_REORDER}」，不列入叫貨單。")

    grouped = sorted(sel.groupby("廠牌", sort=False), key=lambda kv: brand_keys.get(kv[0], (1, 10 ** 9)))
    flat_items = []
    for brand_name, group in grouped:
        st.markdown(f"### 【{brand_name}】")
        bdf = (group[["品項", "數量", "_unit"]]
               .rename(columns={"數量": "叫貨數量", "_unit": "單位"})
               .reset_index(drop=True))
        gfp = hashlib.md5(repr(list(zip(group["_pid"], group["數量"]))).encode("utf-8")).hexdigest()[:8]
        edited_brand = st.data_editor(
            bdf, use_container_width=True, hide_index=True,
            column_config={"品項": st.column_config.TextColumn(disabled=True),
                           "單位": st.column_config.TextColumn(disabled=True),
                           "叫貨數量": st.column_config.NumberColumn(min_value=0, format="%g")},
            key=f"brand_{brand_name}_{gfp}",
        )
        st.caption(f"{len(edited_brand)} 品項")
        for name, qty, cat_id in zip(edited_brand["品項"], edited_brand["叫貨數量"], group["_cat_id"]):
            flat_items.append({"品項": name, "數量": qty, "category_id": cat_id, "廠牌": brand_name})

    st.divider()
    st.subheader("步驟三：匯出")

    order_groups = group_order_items(flat_items, brand_keys)
    n_items = sum(len(items) for _, items in order_groups)
    st.download_button(
        f"📥 下載叫貨單 (.xlsx)　{len(order_groups)} 個廠牌 / {n_items} 項",
        data=_build_order_excel(selected_clinic, order_groups),
        file_name=f"叫貨單_{selected_clinic}_{date.today()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True, type="primary", disabled=n_items == 0,
    )


# ══════════════════════════════════════════════
#  7. 系統設定（含分類/廠牌/單位的編輯刪除）
# ══════════════════════════════════════════════

def page_settings():
    role = st.session_state.user["role"]
    if role not in ("admin", "manager"):
        st.warning("僅管理者可使用此功能")
        return

    st.header("⚙️ 系統設定")
    sb = get_supabase_client()

    if role == "admin":
        tab_params, tab_cats, tab_brands, tab_units, tab_users = st.tabs([
            "📊 參數", "📂 分類", "🏭 廠牌", "📏 單位", "👤 帳號"
        ])
    else:
        tab_params, tab_cats, tab_brands, tab_units = st.tabs([
            "📊 參數", "📂 分類", "🏭 廠牌", "📏 單位"
        ])
        tab_users = None

    # ── 參數：全域（平均耗用計算次數 / 異常閾值）＋ 各分類（備貨倍數 / 安全係數，可依櫃位 A–D）──
    with tab_params:
        gp = load_global_params()
        st.subheader("全域參數")
        st.caption("所有分類共用。")
        g1, g2 = st.columns(2)
        with g1:
            n_avg = st.number_input("平均耗用計算次數（近 N 次）", value=int(gp["avg_n"]),
                                    min_value=1, max_value=24, step=1, key="gp_avg_n",
                                    help="平均耗用 = 該品項最近 N 次盤點的耗用平均（首次盤點不計、負數當 0）")
        with g2:
            thr = st.number_input("異常耗用警示閾值（倍數）", value=float(gp["threshold"]),
                                  min_value=0.0, step=0.1, format="%.1f", key="gp_threshold",
                                  help="單次耗用超過 平均耗用 × 此倍數（且 > 1）或為負數時，庫存頁以紅色粗體標示")
        if st.button("儲存全域參數", type="primary", key="gp_save"):
            save_global_params(int(n_avg), float(thr))
            invalidate_stock_cache()
            st.success("已更新全域參數")

        st.divider()
        st.subheader("各分類參數設定")
        st.caption("目標庫存備貨倍數與建議叫貨安全係數可依櫃位 A/B/C/D 分別設定："
                   "櫃位取第一個字母（A、A2、A70 都算 A），無櫃位或非 A–D 開頭比照 B。"
                   "建議叫貨 = 四捨五入(平均耗用 × 備貨倍數 − 目前庫存)，庫存 < 平均耗用 × 安全係數 才啟動，算出 ≤ 0 不建議。")
        param_cats = load_categories()
        v8_missing = categories_v8_missing(param_cats)
        if v8_missing:
            st.warning("尚未執行 migration_v8_cabinet_params.sql：目前只能設定不分櫃位的數值。"
                       "請在 Supabase SQL Editor 執行該腳本後，再依櫃位分別設定。")

        def _num(v, d):
            return float(v) if v not in (None, "") else float(d)

        new_params = {}
        for c in param_cats:
            cid = c["id"]
            st.markdown(f"**{c['name']}**")
            m_def = _num(c.get("stock_target_multiplier"), DEFAULT_CAT_PARAMS["multiplier"])
            s_def = _num(c.get("safety_factor"), DEFAULT_CAT_PARAMS["safety"])
            vals = {}
            by_cab = False
            if not v8_missing:
                by_cab = st.checkbox("依櫃位 A/B/C/D 分別設定", value=bool(c.get("by_cabinet")),
                                     key=f"cp_bycab_{cid}")
                vals["by_cabinet"] = bool(by_cab)
            if by_cab:
                mcols = st.columns(4)
                for g, col in zip(CABINET_GROUPS, mcols):
                    with col:
                        vals[f"multiplier_{g.lower()}"] = st.number_input(
                            f"目標庫存備貨倍數({g})", value=_num(c.get(f"multiplier_{g.lower()}"), m_def),
                            min_value=0.0, step=0.1, format="%.1f", key=f"cp_m{g}_{cid}")
                scols = st.columns(4)
                for g, col in zip(CABINET_GROUPS, scols):
                    with col:
                        vals[f"safety_{g.lower()}"] = st.number_input(
                            f"建議叫貨安全係數({g})", value=_num(c.get(f"safety_{g.lower()}"), s_def),
                            min_value=0.0, step=0.1, format="%.1f", key=f"cp_s{g}_{cid}")
            else:
                pc1, pc2 = st.columns(2)
                with pc1:
                    vals["stock_target_multiplier"] = st.number_input(
                        "目標庫存備貨倍數", value=m_def, min_value=0.0, step=0.1, format="%.1f", key=f"cp_m_{cid}")
                with pc2:
                    vals["safety_factor"] = st.number_input(
                        "建議叫貨安全係數", value=s_def, min_value=0.0, step=0.1, format="%.1f", key=f"cp_s_{cid}")
            new_params[cid] = vals

        if st.button("儲存全部分類", type="primary", key="cp_save"):
            for cid, vals in new_params.items():
                sb.table("categories").update(vals).eq("id", cid).execute()
            load_categories.clear()
            invalidate_stock_cache()
            st.success("已更新所有分類參數")

    # ── 分類 ──
    with tab_cats:
        cats = load_categories()
        units_list = load_units()

        st.subheader("現有分類")
        for c in cats:
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                new_name = st.text_input("名稱", value=c["name"], key=f"cat_name_{c['id']}", label_visibility="collapsed")
            with col2:
                if st.button("💾", key=f"cat_save_{c['id']}"):
                    sb.table("categories").update({"name": new_name}).eq("id", c["id"]).execute()
                    st.success("已更新")
                    load_categories.clear()
            with col3:
                if st.button("🗑️", key=f"cat_del_{c['id']}"):
                    try:
                        sb.table("categories").delete().eq("id", c["id"]).execute()
                        st.success("已刪除")
                        load_categories.clear()
                    except Exception:
                        st.error("無法刪除（有品項使用中）")

        with st.form("add_cat"):
            st.subheader("新增分類")
            nc = st.text_input("名稱", key="new_cat_name")
            unit_names = [u["name"] for u in units_list]
            nu = st.selectbox("預設單位", unit_names, key="new_cat_unit")
            ns = st.text_input("預設規格", key="new_cat_spec")
            if st.form_submit_button("新增"):
                if nc:
                    uid = next(u["id"] for u in units_list if u["name"] == nu)
                    sb.table("categories").insert({"name": nc, "default_unit_id": uid, "default_spec_note": ns or None}).execute()
                    st.success(f"已新增：{nc}")
                    load_categories.clear()

    # ── 廠牌 ──
    with tab_brands:
        blist = load_brands()
        st.subheader("現有廠牌")
        for b in blist:
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                bn = st.text_input("名稱", value=b["name"], key=f"br_{b['id']}", label_visibility="collapsed")
            with col2:
                if st.button("💾", key=f"br_save_{b['id']}"):
                    sb.table("brands").update({"name": bn}).eq("id", b["id"]).execute()
                    st.success("已更新")
                    load_brands.clear()
            with col3:
                if st.button("🗑️", key=f"br_del_{b['id']}"):
                    try:
                        sb.table("brands").delete().eq("id", b["id"]).execute()
                        st.success("已刪除")
                        load_brands.clear()
                    except Exception:
                        st.error("無法刪除（有品項使用中）")

        with st.form("add_brand"):
            nb = st.text_input("新廠牌名稱")
            if st.form_submit_button("新增"):
                if nb:
                    sb.table("brands").insert({"name": nb}).execute()
                    st.success(f"已新增：{nb}")
                    load_brands.clear()

    # ── 單位 ──
    with tab_units:
        ulist = load_units()
        st.subheader("現有單位")
        for u in ulist:
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                un = st.text_input("名稱", value=u["name"], key=f"un_{u['id']}", label_visibility="collapsed")
            with col2:
                if st.button("💾", key=f"un_save_{u['id']}"):
                    sb.table("units").update({"name": un}).eq("id", u["id"]).execute()
                    st.success("已更新")
                    load_units.clear()
            with col3:
                if st.button("🗑️", key=f"un_del_{u['id']}"):
                    try:
                        sb.table("units").delete().eq("id", u["id"]).execute()
                        st.success("已刪除")
                        load_units.clear()
                    except Exception:
                        st.error("無法刪除（有品項使用中）")

        with st.form("add_unit"):
            nu = st.text_input("新單位名稱")
            if st.form_submit_button("新增"):
                if nu:
                    sb.table("units").insert({"name": nu}).execute()
                    st.success(f"已新增：{nu}")
                    load_units.clear()

    # ── 帳號（僅 admin）──
    if tab_users is None:
        return
    with tab_users:
        users = sb.table("users").select("id, username, role, display_name, clinic_id, clinics(name)").execute().data
        clinics_data = sb.table("clinics").select("id, name").execute().data
        clinic_options = {"不限": None}
        for c in clinics_data:
            clinic_options[c["name"]] = c["id"]
        clinic_id_to_name = {c["id"]: c["name"] for c in clinics_data}
        role_options = ["staff", "manager", "admin"]

        st.subheader("現有帳號")
        for u in users:
            display_label = u['display_name'] or u['username']
            with st.expander(f"👤 {display_label} ｜ 帳號：{u['username']} ｜ {u['role']}"):
                st.text_input(
                    "帳號（不可修改）", value=u["username"],
                    key=f"uname_{u['id']}", disabled=True,
                )
                col1, col2 = st.columns(2)
                with col1:
                    new_display = st.text_input("顯示名稱", value=u["display_name"] or "", key=f"ud_{u['id']}")
                    new_role = st.selectbox("角色", role_options,
                                           index=role_options.index(u["role"]), key=f"ur_{u['id']}")
                with col2:
                    current_clinic = clinic_id_to_name.get(u["clinic_id"], "不限")
                    new_clinic = st.selectbox("所屬診所", list(clinic_options.keys()),
                                             index=list(clinic_options.keys()).index(current_clinic)
                                             if current_clinic in clinic_options else 0,
                                             key=f"uc_{u['id']}")
                    new_pw = st.text_input("重設密碼（留空不改）", type="password", key=f"up_{u['id']}")

                col_save, col_del = st.columns([3, 1])
                with col_save:
                    if st.button("💾 儲存", key=f"us_{u['id']}"):
                        updates = {
                            "display_name": new_display or u["username"],
                            "role": new_role,
                            "clinic_id": clinic_options[new_clinic],
                        }
                        if new_pw:
                            from auth import hash_password
                            updates["password_hash"] = hash_password(new_pw)
                        sb.table("users").update(updates).eq("id", u["id"]).execute()
                        st.success("已更新")
                with col_del:
                    if u["username"] != "admin":
                        if st.button("🗑️ 刪除", key=f"udel_{u['id']}"):
                            sb.table("users").delete().eq("id", u["id"]).execute()
                            st.success("已刪除")
                            st.rerun()
                    else:
                        st.caption("(不可刪除)")

        st.divider()
        with st.form("add_user"):
            st.subheader("新增帳號")
            col1, col2 = st.columns(2)
            with col1:
                nu = st.text_input("帳號 *")
                np = st.text_input("密碼 *", type="password")
            with col2:
                nd = st.text_input("顯示名稱")
                nr = st.selectbox("角色", role_options)
            nc = st.selectbox("所屬診所", list(clinic_options.keys()))

            if st.form_submit_button("新增帳號", type="primary"):
                if not nu or not np:
                    st.error("帳號和密碼為必填")
                else:
                    from auth import hash_password
                    try:
                        sb.table("users").insert({
                            "username": nu, "password_hash": hash_password(np),
                            "display_name": nd or nu, "role": nr, "clinic_id": clinic_options[nc],
                        }).execute()
                        st.success(f"已新增：{nu}")
                    except Exception as e:
                        st.error(f"失敗：{e}")
