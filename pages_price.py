"""
💰 進價 / 成本表 — 品項各廠牌進價（全院共用）、成本價（進價 × 廠牌倍數，僅院長）、廠1/2/3 編輯、Excel 匯出、參數設定

表格三區（由上而下）：健保藥 → 特級用藥（高貴藥材；與健保不重複，進價手填）→ 特.用藥（自費科中；健保品項的映射，
只是選用不同廠牌，進價自動複製同名健保品項、不可手填，廠牌可編輯）。
預設只列啟用中的品項，但「特.」品項的健保來源即使停用也列出（天麻B ← 特.天麻），否則無處填價；可切換顯示全部。
進價填入區自動歸位（plan_price_moves）：同一藥的 健保／特.／特級 版本，健保或特. 仍啟用 → 進價歸健保區（特級版停用則其進價搬到健保）；
健保與特. 全停用、只剩特級版 → 進價歸特級區（健保進價自動搬過去）。
🔍 搜尋品項（進價表／成本表共用、兩個 tab 同時過濾；成本表可搜廠牌／櫃位）只過濾畫面、不影響 Excel 匯出；
列底色分組（每 5 列／逐列／無，key price_band）＋ 注音/品項 欄凍結，方便左右對照。編輯模式下 Streamlit 只對不可編輯欄套用底色。

資料表（migration_v10_prices.sql）：
  product_prices       product_id × brand_id → price（數字）或 note（文字，如「時價」「無」）
  price_table_brands   表格要顯示的廠牌欄位：kind(單方/複方)、brand_id、multiplier(成本倍數)、sort_order
  clinic_stock.brand3_id  第三廠牌（叫貨順序表「廠3」）
  system_settings      high_price_single / high_price_compound：成本 ≥ 界線 → 橘色粗體
"""

import io
import math
import re
import unicodedata
from datetime import date, datetime
from fractions import Fraction

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Font, Border, Side

from database import get_supabase_client, fetch_all, with_retry
from pages_app import (
    load_products, load_brands, load_categories, load_clinics, get_clinic_id,
    load_global_params, invalidate_stock_cache, get_bopomofo_initial, get_bopomofo_sort_key,
    BRAND_ABBR,
)

KINDS = ("複方", "單方")
SECTIONS = ("健保", "高貴", "自費")
SECTION_LABEL = {"健保": "健保藥", "高貴": "特級用藥（高貴藥）", "自費": "特.用藥（自費藥）"}
SECTION_NOTE = {
    "健保": "科學中藥複方／單方（含水藥材、非健保藥材）；進價在此填寫。同一藥只要健保或特. 版仍啟用，進價就填在這裡。",
    "高貴": "高貴藥材；品項與健保藥不重複（如 特級阿膠），進價在此填寫。健保與特. 版都停用、只剩特級版時，原健保進價會自動搬到這裡。",
    "自費": "自費科中「特.」品項＝健保藥的映射，只是選用不同廠牌；進價自動複製同名健保品項、不可手填，廠1/2/3 在成本表編輯。",
}
EXCEL_MARKER = {"高貴": "特級用藥(高貴藥)", "自費": "特.用藥(自費藥)"}   # 匯出時各區前的標記列（原表只有「自費藥」）
INACTIVE_CSS = "color:#9E9E9E;font-style:italic"
INACTIVE_RGB = "9E9E9E"
HIGH_PRICE_KEYS = {"單方": "high_price_single", "複方": "high_price_compound"}
DEFAULT_HIGH_PRICE = {"單方": 280.0, "複方": 450.0}
ORANGE = "FFC000"
ORANGE_CSS = "color:#E08E00;font-weight:700"
# 成本表品名顏色（沿用原 Excel 慣例：A 櫃紅字、C 櫃藍字）
NAME_COLOR_BY_CABINET = {"A": "C00000", "C": "00B0F0"}
BRAND_COLS = (("廠1", "brand1_id"), ("廠2", "brand2_id"), ("廠3", "brand3_id"))
# 列底色分組（方便左右對照）：選項 → 每幾列換一次底色；0 = 不分組。半透明色在深／淺色主題都看得到
BAND_OPTIONS = {"每 5 列分組": 5, "逐列交錯": 1, "無": 0}
BAND_CSS = "background-color:rgba(90,120,170,0.14)"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ══════════════════════════════════════════════
#  基本工具
# ══════════════════════════════════════════════

def product_kind(cat_name) -> str:
    """分類名稱含「複方」→ 複方；其餘（單方、水藥材、非健保藥材…）→ 單方。"""
    return "複方" if "複方" in (cat_name or "") else "單方"


def product_section(cat_name) -> str:
    """分類名稱含「高貴」→ 高貴（特級用藥）；含「自費」→ 自費（特. 用藥，最下方）；其餘 → 健保。"""
    name = cat_name or ""
    if "高貴" in name:
        return "高貴"
    return "自費" if "自費" in name else "健保"


def is_auto_price_category(cat_name) -> bool:
    """自費科中（「特.」品項）：進價自動帶入同名健保品項，不可手填；高貴藥材則可手填。"""
    return product_section(cat_name) == "自費"


_VARIANTS = str.maketrans({"蔘": "參", "术": "朮", "姜": "薑", "芪": "耆", "荆": "荊"})


def _norm_name(name, strip_b: bool = True) -> str:
    """品名正規化（找同名健保品項用）：去括號註記、去 特./特級、去 -規格 後綴、異體字統一。
    strip_b=True 時另去掉健保「B」版本尾碼（天麻B → 天麻），讓 特.天麻 能對應 天麻B。"""
    s = str(name).strip().replace("（", "(").replace("）", ")")
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[-－—]+.*$", "", s)
    s = re.sub(r"^特[.．級]", "", s)
    for a, b in (("瓜蔞", "栝樓"), ("栝婁", "栝樓"), ("栝簍", "栝樓"), ("栝蔞", "栝樓"), ("龜粄", "龜板")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", "", s.translate(_VARIANTS))
    return re.sub(r"(?<=[一-鿿])[BＢ]$", "", s) if strip_b else s


def _cat_of(p, cat_names: dict) -> str:
    return cat_names.get(p["category_id"]) or (p.get("categories") or {}).get("name", "")


def build_linked_sources(products: list, cat_names: dict) -> dict:
    """{特.品項 id: 同名健保品項 id}（同單複方、正規化品名相同；多個候選時取去掉「特.」後完全同名者，否則取最短名）。
    沒有同名健保品項時退而對應同名「高貴藥材」品項（如 特.吳茱萸湯 → 特級吳茱萸湯）。"""
    by_key, by_key_precious = {}, {}
    for p in products:
        cname = _cat_of(p, cat_names)
        key = (product_kind(cname), _norm_name(p["name"]))
        sec = product_section(cname)
        if sec == "健保":
            by_key.setdefault(key, []).append(p)
        elif sec == "高貴":
            by_key_precious.setdefault(key, []).append(p)
    out = {}
    for p in products:
        cname = _cat_of(p, cat_names)
        if not is_auto_price_category(cname):
            continue
        key = (product_kind(cname), _norm_name(p["name"]))
        cands = by_key.get(key) or by_key_precious.get(key) or []
        if not cands:
            continue
        if len(cands) > 1:
            # 多候選：先排除「B」版本（延胡索 vs 延胡索B 取延胡索），再取去掉「特.」後完全同名者，否則取最短名
            plain = _norm_name(p["name"], strip_b=False)
            non_b = [c for c in cands if _norm_name(c["name"], strip_b=False) == plain]
            bare = re.sub(r"^特[.．]", "", p["name"]).strip()
            exact = [c for c in (non_b or cands) if c["name"].strip() == bare]
            cands = exact or sorted(non_b or cands, key=lambda c: len(c["name"]))
        out[p["id"]] = cands[0]["id"]
    return out


def plan_price_moves(products: list, cat_names: dict, cs_any: dict, prices: dict) -> list:
    """進價填入區自動歸位。同名（正規化、同單複方）的 健保／特.／高貴 品項視為同一藥：
       - 健保或任一特. 仍啟用 → 進價歸健保；高貴版若已全部停用，其進價搬到健保品項
       - 健保與特. 全停用、高貴版啟用 → 進價歸高貴；健保品項的進價搬到高貴品項
    只處理「恰有一個健保、一個高貴」的組合，避免誤判。cs_any=load_clinic_stock_brands(None)（任一診所啟用即 is_active）。
    回傳 [{"from", "to", "cells":[price rows 要補到 to 的], "drop":[from 的所有 (pid, brand_id)]}]，不寫 DB。"""
    fam = {}
    for p in products:
        cname = _cat_of(p, cat_names)
        fam.setdefault((product_kind(cname), _norm_name(p["name"])), {"健保": [], "高貴": [], "自費": []})[product_section(cname)].append(p)

    def active(p):
        return bool((cs_any.get(p["id"]) or {}).get("is_active"))

    plan = []
    for members in fam.values():
        if len(members["健保"]) != 1 or len(members["高貴"]) != 1:
            continue
        h, g = members["健保"][0], members["高貴"][0]
        if active(h) or any(active(t) for t in members["自費"]):
            frm, to = (g, h) if not active(g) else (None, None)
        elif active(g):
            frm, to = h, g
        else:
            frm, to = None, None
        if frm is None:
            continue
        own = {(pid, bid): v for (pid, bid), v in prices.items() if pid == frm["id"]}
        if not own:
            continue
        cells = [{"product_id": to["id"], "brand_id": bid, "price": v["price"], "note": v["note"]}
                 for (pid, bid), v in own.items() if (to["id"], bid) not in prices]
        plan.append({"from": frm, "to": to, "cells": cells, "drop": list(own)})
    return plan


def apply_price_moves(plan: list) -> None:
    sb = get_supabase_client()
    uid = (st.session_state.get("user") or {}).get("id")
    now = datetime.now().isoformat(timespec="seconds")
    for m in plan:
        if m["cells"]:
            sb.table("product_prices").upsert([{**c, "updated_at": now, "updated_by": uid} for c in m["cells"]],
                                              on_conflict="product_id,brand_id").execute()
        sb.table("product_prices").delete().eq("product_id", m["from"]["id"]).execute()
    if plan:
        load_product_prices.clear()


def _price_of(prices: dict, row: dict, brand_id: int):
    """該列某廠牌的進價記錄；特. 品項改讀同名健保品項（row["src"]）。"""
    return prices.get((row.get("src") or row["pid"], brand_id))


def calc_cost(price, multiplier):
    """成本 = 進價 × 倍數，無條件進位取整（與原 Excel ROUNDUP 一致）。
    multiplier 建議傳 Fraction（由算式精確算出），避免 5/6*0.65 這類循環小數因浮點誤差多進 1。"""
    if price is None or multiplier is None:
        return None
    m = multiplier if isinstance(multiplier, Fraction) else Fraction(str(multiplier))
    return math.ceil(Fraction(str(price)) * m)


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def eval_formula(expr) -> Fraction:
    """倍數算式 → 精確分數。只接受數字與 + - * / ( )，例如 5/6*0.65、0.7/5。"""
    s = str(expr).strip().replace("×", "*").replace("÷", "/").replace("＊", "*").replace("／", "/")
    if not s or not re.fullmatch(r"[0-9.\s+\-*/()]+", s):
        raise ValueError(f"算式「{expr}」只能含數字與 + - * / ( )")
    code = _NUM_RE.sub(lambda m: f"Fraction('{m.group()}')", s)
    val = eval(code, {"__builtins__": {}}, {"Fraction": Fraction})  # noqa: S307 — 已以正規表示式限制為純算術
    if not isinstance(val, Fraction) or not (0 < val < 10):
        raise ValueError(f"算式「{expr}」算出 {float(val):g}，不合理")
    return val


def fmt_multiplier(v) -> str:
    return f"{float(v):.6f}".rstrip("0").rstrip(".")


_PRICE_NOTE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[（(]\s*([^()（）]+?)\s*[)）]$")


def parse_price_cell(text):
    """表格字串 → (price, note)。數字 → 進價；「240(無)」→ 進價 240 + 備註「無」；
    空白 → 都 None（＝刪除）；其他文字（時價…）→ 只有 note。"""
    s = "" if text is None else str(text).strip()
    if s in ("", "nan", "None", "-"):
        return None, None
    m = _PRICE_NOTE_RE.match(s)
    if m:
        return float(m.group(1)), m.group(2)[:50]
    try:
        return float(s.replace(",", "")), None
    except ValueError:
        return None, s[:50]


def _fmt_num(p) -> str:
    p = float(p)
    return str(int(p)) if p.is_integer() else f"{p:g}"


def fmt_price(price, note=None) -> str:
    if price is not None:
        return f"{_fmt_num(price)}({note})" if note else _fmt_num(price)
    return note or ""


def _row_remarks(row, brands_k, prices) -> str:
    """同時有進價與備註的格（如 莊松榮 240 + 無）→ 匯出時集中在「備註」欄：莊松榮:無"""
    out = []
    for b in brands_k:
        pr = _price_of(prices, row, b["brand_id"])
        if pr and pr["price"] is not None and pr.get("note"):
            out.append(f"{b['name']}:{pr['note']}")
    return "、".join(out)


def _sort_name(name: str) -> str:
    for prefix in ("特.", "特級"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _display_name(r: dict) -> str:
    """停用品項在畫面上加註（停用）；匯出 Excel 不加字、改灰字。"""
    return r["name"] if r.get("active", True) else f"{r['name']}（停用）"


def _cabinet_letter(cabinet) -> str:
    s = (cabinet or "").strip().upper()
    return s[0] if s else ""


def _abbr(brand_name) -> str:
    if not brand_name or brand_name == "-":
        return ""
    return BRAND_ABBR.get(brand_name, brand_name)


# ══════════════════════════════════════════════
#  資料讀寫
# ══════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_price_table_brands():
    """[{id, kind, brand_id, multiplier, sort_order, name}]（依 sort_order）。資料表不存在（未跑 migration_v10）→ None。"""
    try:
        rows = with_retry(lambda: get_supabase_client().table("price_table_brands")
                          .select("id, kind, brand_id, multiplier, formula, sort_order, brands(name)")
                          .order("sort_order").order("id").execute().data)
    except Exception:
        return None
    out = []
    for r in rows:
        formula = (r.get("formula") or "").strip()
        try:
            mult = eval_formula(formula) if formula else Fraction(str(r["multiplier"]))
        except Exception:
            mult = Fraction(str(r["multiplier"] if r["multiplier"] is not None else "0.7"))
        out.append({
            "id": r["id"], "kind": r["kind"], "brand_id": r["brand_id"],
            "multiplier": mult, "formula": formula or fmt_multiplier(mult),
            "sort_order": r["sort_order"] or 0,
            "name": (r.get("brands") or {}).get("name") or f"#{r['brand_id']}",
        })
    return out


@st.cache_data(ttl=30)
def load_product_prices() -> dict:
    """{(product_id, brand_id): {"price": float|None, "note": str|None}}"""
    rows = fetch_all(get_supabase_client().table("product_prices")
                     .select("product_id, brand_id, price, note").order("id"))
    return {(r["product_id"], r["brand_id"]): {
        "price": float(r["price"]) if r["price"] is not None else None, "note": r["note"]} for r in rows}


@st.cache_data(ttl=30)
def load_high_price() -> dict:
    """{"單方": 230.0, "複方": 399.0}（system_settings；缺值套預設）"""
    rows = with_retry(lambda: get_supabase_client().table("system_settings").select("key, value")
                      .in_("key", list(HIGH_PRICE_KEYS.values())).execute().data)
    kv = {r["key"]: r["value"] for r in rows}
    out = {}
    for kind, key in HIGH_PRICE_KEYS.items():
        try:
            out[kind] = float(kv[key])
        except (KeyError, TypeError, ValueError):
            out[kind] = DEFAULT_HIGH_PRICE[kind]
    return out


def save_high_price(single: float, compound: float) -> None:
    get_supabase_client().table("system_settings").upsert([
        {"key": "high_price_single", "value": str(float(single)), "description": "單方高價標示界線（成本 ≥ 此值橘色粗體）"},
        {"key": "high_price_compound", "value": str(float(compound)), "description": "複方高價標示界線（成本 ≥ 此值橘色粗體）"},
    ]).execute()
    load_high_price.clear()


def save_price_table_brands(rows: list) -> None:
    """rows=[{kind, brand_id, multiplier, sort_order}]：不在清單內的列刪除，其餘 upsert。"""
    sb = get_supabase_client()
    existing = sb.table("price_table_brands").select("id, kind, brand_id").execute().data
    keep = {(r["kind"], r["brand_id"]) for r in rows}
    for e in existing:
        if (e["kind"], e["brand_id"]) not in keep:
            sb.table("price_table_brands").delete().eq("id", e["id"]).execute()
    if rows:
        sb.table("price_table_brands").upsert(rows, on_conflict="kind,brand_id").execute()
    load_price_table_brands.clear()


@st.cache_data(ttl=30)
def load_clinic_stock_brands(clinic_id):
    """{product_id: {brand1_id, brand2_id, brand3_id, cabinet, is_active}}。
    clinic_id=None → 兩診所合併（任一診所啟用即算啟用，廠牌/櫃位取先讀到的）。brand3_id 欄位不存在時補 None。"""
    sb = get_supabase_client()

    def _q(cols):
        q = sb.table("clinic_stock").select(cols)
        if clinic_id is not None:
            q = q.eq("clinic_id", clinic_id)
        return fetch_all(q.order("id"))

    try:
        rows = _q("product_id, brand1_id, brand2_id, brand3_id, is_active, cabinet")
    except Exception:
        rows = _q("product_id, brand1_id, brand2_id, is_active, cabinet")
        for r in rows:
            r["brand3_id"] = None
    out = {}
    for r in rows:
        cur = out.get(r["product_id"])
        if cur is None:
            out[r["product_id"]] = {k: r.get(k) for k in ("brand1_id", "brand2_id", "brand3_id", "cabinet", "is_active")}
        else:
            cur["is_active"] = bool(cur["is_active"]) or bool(r["is_active"])
    return out


def invalidate_price_cache():
    for fn in (load_product_prices, load_price_table_brands, load_high_price, load_clinic_stock_brands):
        fn.clear()


# ══════════════════════════════════════════════
#  表格資料
# ══════════════════════════════════════════════

def build_rows(kind: str, cs_map: dict, products: list, cat_names: dict, linked: dict | None = None,
               include_inactive: bool = False) -> list:
    """kind（單方/複方）的品項列，依 區段(健保→高貴→自費) → 首字注音 → 全字注音 排序。
    預設只列診所啟用中的品項；但已列出的「特.」品項其健保來源即使停用也一併列出（特.天麻 → 天麻B），
    否則來源無處填價。include_inactive=True 則全部列出。列的 active=False 表示停用（畫面灰字）。
    linked：build_linked_sources() 結果；特. 品項列帶 auto=True、src=來源品項 id（可能 None＝找不到）、src_section=來源區段。"""
    linked = linked or {}
    sec_by_pid = {p["id"]: product_section(_cat_of(p, cat_names)) for p in products}
    cands = []
    for p in products:
        cname = _cat_of(p, cat_names)
        if product_kind(cname) != kind:
            continue
        cs = cs_map.get(p["id"]) or {}
        cands.append((p, cname, cs, bool(cs.get("is_active")) if cs else False))
    keep = {p["id"] for p, _, _, active in cands if active or include_inactive}
    for p, cname, _, _ in cands:
        if p["id"] in keep and is_auto_price_category(cname) and linked.get(p["id"]):
            keep.add(linked[p["id"]])
    rows = []
    for p, cname, cs, active in cands:
        if p["id"] not in keep:
            continue
        rows.append({
            "pid": p["id"], "name": p["name"], "cat": cname, "section": product_section(cname),
            "initial": get_bopomofo_initial(p["name"]), "sort": get_bopomofo_sort_key(_sort_name(p["name"])),
            "cabinet": cs.get("cabinet") or "", "active": active,
            "b1": cs.get("brand1_id"), "b2": cs.get("brand2_id"), "b3": cs.get("brand3_id"),
            "auto": is_auto_price_category(cname), "src": linked.get(p["id"]),
            "src_section": sec_by_pid.get(linked.get(p["id"])),
        })
    rows.sort(key=lambda r: (SECTIONS.index(r["section"]), r["initial"], r["sort"]))
    return rows


def _initial_labels(sec_rows: list) -> list:
    """同首字注音只在第一列顯示（與原 Excel 相同）。"""
    labels, prev = [], None
    for r in sec_rows:
        labels.append(r["initial"] if r["initial"] != prev else "")
        prev = r["initial"]
    return labels


def _price_frame(sec_rows, brands_k, prices, high):
    cols = ["注音", "品項"] + [b["name"] for b in brands_k]
    data, css = [], []
    for r, lab in zip(sec_rows, _initial_labels(sec_rows)):
        rec = {"注音": lab, "品項": _display_name(r)}
        crec = {"注音": "", "品項": "" if r.get("active", True) else INACTIVE_CSS}
        for b in brands_k:
            pr = _price_of(prices, r, b["brand_id"])
            rec[b["name"]] = fmt_price(pr["price"], pr["note"]) if pr else ""
            cost = calc_cost(pr["price"], b["multiplier"]) if pr else None
            crec[b["name"]] = ORANGE_CSS if (cost is not None and cost >= high) else ""
        data.append(rec)
        css.append(crec)
    return pd.DataFrame(data, columns=cols), pd.DataFrame(css, columns=cols)


def _cost_frame(sec_rows, brands_k, prices, high, brand_map):
    cols = ["注音", "品項"] + [b["name"] for b in brands_k] + ["廠1", "廠2", "廠3", "櫃"]
    data, css = [], []
    for r, lab in zip(sec_rows, _initial_labels(sec_rows)):
        rec, crec = {"注音": lab, "品項": _display_name(r)}, {c: "" for c in cols}
        g = _cabinet_letter(r["cabinet"])
        if not r.get("active", True):
            crec["品項"] = INACTIVE_CSS
        elif g in NAME_COLOR_BY_CABINET:
            crec["品項"] = f"color:#{NAME_COLOR_BY_CABINET[g]}"
        for b in brands_k:
            pr = _price_of(prices, r, b["brand_id"])
            cost = calc_cost(pr["price"], b["multiplier"]) if pr else None
            if cost is not None:
                rec[b["name"]] = str(cost)
                if cost >= high:
                    crec[b["name"]] = ORANGE_CSS
            else:
                rec[b["name"]] = (pr or {}).get("note") or ""
        for col, key in zip(("廠1", "廠2", "廠3"), ("b1", "b2", "b3")):
            rec[col] = brand_map.get(r[key], "-") if r[key] else "-"
        rec["櫃"] = r["cabinet"]
        data.append(rec)
        css.append(crec)
    return pd.DataFrame(data, columns=cols), pd.DataFrame(css, columns=cols)


def _table_height(n: int) -> int:
    return int(min(n * 35 + 38, 900))


# ── 搜尋 / 可讀性 ──

def search_terms(q) -> list:
    """搜尋字串 → 關鍵字清單（空格分隔、全半形統一、不分大小寫）。"""
    return [unicodedata.normalize("NFKC", t).lower() for t in str(q or "").split() if t.strip()]


def filter_rows(rows: list, terms: list, brand_map: dict | None = None) -> list:
    """搜尋過濾：比對 品名（原名、正規化名、含「（停用）」的顯示名）、注音首字、櫃位；給 brand_map 時再比對 廠1/2/3 廠牌名。
    多個關鍵字需全部符合（AND）。回傳順序不變（三區位置保留）。"""
    if not terms:
        return rows
    out = []
    for r in rows:
        parts = [r["name"], _norm_name(r["name"]), _display_name(r), r.get("initial") or "", r.get("cabinet") or ""]
        if brand_map:
            parts += [brand_map.get(r.get(k)) or "" for k in ("b1", "b2", "b3") if r.get(k)]
        hay = unicodedata.normalize("NFKC", " ".join(parts)).lower()
        if all(t in hay for t in terms):
            out.append(r)
    return out


def _band_rows(n: int, size: int) -> list:
    """第 i 列的底色 css：每 size 列一組、組與組交錯上色；size=0 → 全空。"""
    return [BAND_CSS if size and (i // size) % 2 else "" for i in range(n)]


def _styled(df: pd.DataFrame, css: pd.DataFrame, band: int):
    """Styler：原有格式（橘字／灰字／櫃位色）疊上列底色分組。st.data_editor 只會對不可編輯欄套用。"""
    full = css.copy()
    for i, bg in enumerate(_band_rows(len(df), band)):
        if bg:
            full.iloc[i] = [";".join(x for x in (bg, c) if x) for c in css.iloc[i]]
    return df.style.apply(lambda _: full, axis=None)


def _view_cfg() -> dict:
    """檢視模式欄位設定：注音窄欄、注音＋品項 凍結在左側（橫向捲動時仍看得到品名）。"""
    return {"注音": st.column_config.TextColumn(width="small", pinned=True),
            "品項": st.column_config.TextColumn(pinned=True)}


# ══════════════════════════════════════════════
#  儲存
# ══════════════════════════════════════════════

def save_price_edits(sec_rows, brands_k, old_df, new_df, prices) -> tuple:
    """比對編輯前後字串，upsert 有變更的格；清空的格刪除。回傳 (upsert 數, 刪除數)。"""
    sb = get_supabase_client()
    uid = (st.session_state.get("user") or {}).get("id")
    now = datetime.now().isoformat(timespec="seconds")
    ups, dels = [], []
    for i, r in enumerate(sec_rows):
        for b in brands_k:
            o = "" if pd.isna(old_df.iloc[i][b["name"]]) else str(old_df.iloc[i][b["name"]]).strip()
            n = "" if pd.isna(new_df.iloc[i][b["name"]]) else str(new_df.iloc[i][b["name"]]).strip()
            if o == n:
                continue
            price, note = parse_price_cell(n)
            if price is None and note is None:
                if (r["pid"], b["brand_id"]) in prices:
                    dels.append((r["pid"], b["brand_id"]))
            else:
                ups.append({"product_id": r["pid"], "brand_id": b["brand_id"], "price": price, "note": note,
                            "updated_at": now, "updated_by": uid})
    if ups:
        sb.table("product_prices").upsert(ups, on_conflict="product_id,brand_id").execute()
    for pid, bid in dels:
        sb.table("product_prices").delete().eq("product_id", pid).eq("brand_id", bid).execute()
    if ups or dels:
        load_product_prices.clear()
    return len(ups), len(dels)


def save_brand_edits(sec_rows, old_df, new_df, brands, clinic_id) -> tuple:
    """廠1/廠2/廠3 變更 → clinic_stock（本診所；同步開啟時一併寫入其他診所）。回傳 (更新品項數, 是否同步)。"""
    sb = get_supabase_client()
    sync = bool(load_global_params()["sync_brands"])
    name2id = {b["name"]: b["id"] for b in brands}
    n = 0
    for i, r in enumerate(sec_rows):
        ch = {}
        for col, key in BRAND_COLS:
            o, nw = old_df.iloc[i][col], new_df.iloc[i][col]
            if (o or "-") != (nw or "-"):
                ch[key] = name2id.get(nw) if nw and nw != "-" else None
        if ch:
            sb.table("clinic_stock").update(ch).eq("product_id", r["pid"]).eq("clinic_id", clinic_id).execute()
            if sync:
                sb.table("clinic_stock").update(ch).eq("product_id", r["pid"]).neq("clinic_id", clinic_id).execute()
            n += 1
    if n:
        invalidate_stock_cache()
        load_clinic_stock_brands.clear()
    return n, sync


# ══════════════════════════════════════════════
#  Excel 匯出（格式沿用 @科中進貨價目表 / @科中進貨成本價目&叫貨順序表）
# ══════════════════════════════════════════════

_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_FONT = Font(name="新細明體", size=11)
_FONT_ORANGE = Font(name="新細明體", size=11, bold=True, color=ORANGE)


def _font_color(rgb: str) -> Font:
    return Font(name="新細明體", size=11, color=rgb)


def _setup_sheet(ws, kind: str, widths: dict):
    ws.page_setup.orientation = "portrait"
    ws.page_setup.paperSize = 9  # A4
    ws.page_setup.scale = 95
    ws.print_title_rows = "1:3"
    ws.page_margins.left = ws.page_margins.right = 0.75
    ws.page_margins.top = ws.page_margins.bottom = 1.0
    ws.cell(2, 2, kind).font = _FONT
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _write_cell(ws, r, c, value=None, font=_FONT):
    cell = ws.cell(r, c, value)
    cell.font = font
    cell.border = _BORDER
    return cell


def _iter_sections(rows):
    """yield (section, [rows])，依 SECTIONS 順序；匯出時高貴/自費區前空一列再放 EXCEL_MARKER 標記列。"""
    for section in SECTIONS:
        sec = [x for x in rows if x["section"] == section]
        if sec:
            yield section, sec


def build_price_workbook(tables: dict, prices: dict, high: dict, as_of: date) -> bytes:
    """tables={kind: (rows, brands_k)}；兩個工作表（科學中藥-複方 / 科學中藥-單方），右上角日期，橘色粗體＝成本 ≥ 界線。"""
    wb = Workbook()
    wb.remove(wb.active)
    for kind in KINDS:
        rows, brands_k = tables[kind]
        ws = wb.create_sheet(f"科學中藥-{kind}")
        ncol = 2 + len(brands_k)
        has_remark = any(_row_remarks(x, brands_k, prices) for x in rows)
        widths = {"A": 4.5, "B": 16 if kind == "複方" else 11.4,
                  **{ws.cell(3, 3 + i).column_letter: 9 for i in range(len(brands_k))}}
        if has_remark:
            widths[ws.cell(3, ncol + 1).column_letter] = 12
        _setup_sheet(ws, kind, widths)
        d = ws.cell(1, ncol, as_of)
        d.number_format = "yyyy/mm"
        d.font = _FONT
        _write_cell(ws, 3, 1)
        _write_cell(ws, 3, 2, "品項")
        for i, b in enumerate(brands_k):
            _write_cell(ws, 3, 3 + i, b["name"])
        if has_remark:
            _write_cell(ws, 3, ncol + 1, "備註")
        r = 4
        for section, sec in _iter_sections(rows):
            if section in EXCEL_MARKER:
                r += 1
                _write_cell(ws, r, 1, EXCEL_MARKER[section])
                r += 1
            for x, lab in zip(sec, _initial_labels(sec)):
                _write_cell(ws, r, 1, lab or None)
                _write_cell(ws, r, 2, x["name"], font=_FONT if x.get("active", True) else _font_color(INACTIVE_RGB))
                for i, b in enumerate(brands_k):
                    pr = _price_of(prices, x, b["brand_id"])
                    cell = _write_cell(ws, r, 3 + i)
                    if not pr:
                        continue
                    if pr["price"] is not None:
                        p = pr["price"]
                        cell.value = int(p) if float(p).is_integer() else p
                        cost = calc_cost(p, b["multiplier"])
                        if cost is not None and cost >= high[kind]:
                            cell.font = _FONT_ORANGE
                    elif pr["note"]:
                        cell.value = pr["note"]
                if has_remark:
                    _write_cell(ws, r, ncol + 1, _row_remarks(x, brands_k, prices) or None)
                r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_cost_workbook(tables: dict, prices: dict, high: dict, brand_map: dict, clinic_name: str) -> bytes:
    """成本價目＆叫貨順序表：成本（進價×倍數）、廠1/廠2/廠3（縮寫）、櫃；品名 A 櫃紅字、C 櫃藍字；橘色粗體＝成本 ≥ 界線。"""
    wb = Workbook()
    wb.remove(wb.active)
    for kind in KINDS:
        rows, brands_k = tables[kind]
        ws = wb.create_sheet(f"科學中藥-{kind}")
        nb = len(brands_k)
        extra = ("廠1", "廠2", "廠3", "櫃")
        if any(_row_remarks(x, brands_k, prices) for x in rows):
            extra = extra + ("備註",)
        widths = {"A": 3.8, "B": 17.4 if kind == "複方" else 12.8}
        for i in range(nb):
            widths[ws.cell(3, 3 + i).column_letter] = 7
        for j, h in enumerate(extra):
            widths[ws.cell(3, 3 + nb + j).column_letter] = 12 if h == "備註" else 5.5
        _setup_sheet(ws, kind, widths)
        ws.cell(1, 2, f"{clinic_name}  {date.today():%Y/%m/%d}").font = _FONT
        _write_cell(ws, 3, 1)
        _write_cell(ws, 3, 2, "品項")
        for i, b in enumerate(brands_k):
            _write_cell(ws, 3, 3 + i, b["name"])
        for j, h in enumerate(extra):
            _write_cell(ws, 3, 3 + nb + j, h)
        r = 4
        for section, sec in _iter_sections(rows):
            if section in EXCEL_MARKER:
                r += 1
                _write_cell(ws, r, 1, EXCEL_MARKER[section])
                r += 1
            for x, lab in zip(sec, _initial_labels(sec)):
                _write_cell(ws, r, 1, lab or None)
                g = _cabinet_letter(x["cabinet"])
                if not x.get("active", True):
                    name_font = _font_color(INACTIVE_RGB)
                else:
                    name_font = _font_color(NAME_COLOR_BY_CABINET[g]) if g in NAME_COLOR_BY_CABINET else _FONT
                _write_cell(ws, r, 2, x["name"], font=name_font)
                for i, b in enumerate(brands_k):
                    pr = _price_of(prices, x, b["brand_id"])
                    cell = _write_cell(ws, r, 3 + i)
                    if not pr:
                        continue
                    cost = calc_cost(pr["price"], b["multiplier"])
                    if cost is not None:
                        cell.value = cost
                        if cost >= high[kind]:
                            cell.font = _FONT_ORANGE
                    elif pr["note"]:
                        cell.value = pr["note"]
                for j, key in enumerate(("b1", "b2", "b3")):
                    _write_cell(ws, r, 3 + nb + j, _abbr(brand_map.get(x[key])) or None)
                _write_cell(ws, r, 3 + nb + 3, x["cabinet"] or None)
                if len(extra) == 5:
                    _write_cell(ws, r, 3 + nb + 4, _row_remarks(x, brands_k, prices) or None)
                r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════
#  頁面
# ══════════════════════════════════════════════

def _render_price_table(kind, rows, brands_k, prices, high, band=0):
    edit = st.toggle("✏️ 編輯進價", key=f"price_edit_{kind}")
    st.caption(f"成本 = 進價 × 廠牌倍數；成本 ≥ {high:g} 的進價以橘色粗體標示。灰字＝停用品項。"
               + ("直接在表格填寫後按「儲存進價」；清空＝刪除，可填「時價」等文字，或「240(無)」＝進價 240 加備註。" if edit
                  else "開啟「編輯進價」即可填寫。"))
    for section, sec in _iter_sections(rows):
        st.markdown(f"**{SECTION_LABEL[section]}**（{len(sec)} 項）")
        st.caption(SECTION_NOTE[section])
        if not edit:
            df, css = _price_frame(sec, brands_k, prices, high)
            st.dataframe(_styled(df, css, band), hide_index=True, use_container_width=True,
                         height=_table_height(len(df)), column_config=_view_cfg())
            continue
        manual = [r for r in sec if not r["auto"]]
        auto = [r for r in sec if r["auto"]]
        if manual:
            df, css = _price_frame(manual, brands_k, prices, high)
            cfg = {"注音": st.column_config.TextColumn(disabled=True, width="small", pinned=True),
                   "品項": st.column_config.TextColumn(disabled=True, pinned=True)}
            for b in brands_k:
                cfg[b["name"]] = st.column_config.TextColumn(width="small")
            ed = st.data_editor(_styled(df, css, band), hide_index=True, use_container_width=True,
                                height=_table_height(len(df)), column_config=cfg, key=f"price_ed_{kind}_{section}")
            if st.button("💾 儲存進價", type="primary", key=f"price_save_{kind}_{section}"):
                n_up, n_del = save_price_edits(manual, brands_k, df, ed, prices)
                if n_up or n_del:
                    st.success(f"已儲存：更新 {n_up} 格、清除 {n_del} 格")
                    st.rerun()
                else:
                    st.info("沒有變更")
        if auto:
            missing = [r["name"] for r in auto if not r.get("src")]
            odd = [r["name"] for r in auto if r.get("src") and r.get("src_section") != "健保"]
            st.caption(f"🔒 以下 {len(auto)} 個「特.」品項進價自動帶入同名健保品項，不可手填"
                       + (f"；找不到同名健保品項：{'、'.join(missing)}" if missing else "")
                       + (f"；系統無同名健保品項、暫以同名高貴藥材帶價（請確認分類）：{'、'.join(odd)}" if odd else "") + "。")
            adf, acss = _price_frame(auto, brands_k, prices, high)
            st.dataframe(_styled(adf, acss, band), hide_index=True, use_container_width=True,
                         height=_table_height(len(adf)), column_config=_view_cfg())


def _render_cost_table(kind, rows, brands_k, prices, high, brand_map, brands, clinic_id, band=0):
    edit = st.toggle("✏️ 編輯廠牌（廠1 / 廠2 / 廠3）", key=f"cost_edit_{kind}")
    sync = bool(load_global_params()["sync_brands"])
    st.caption(f"成本 = 進價 × 廠牌倍數（無條件進位取整）；≥ {high:g} 橘色粗體。品名紅字＝A 櫃、藍字＝C 櫃、灰字＝停用。"
               f"三區（含「特.」品項）的廠1/2/3 都可在此編輯，存檔後即同步到品項管理、庫存與叫貨頁"
               f"{'，並同步到其他診所' if sync else ''}；櫃位請到品項管理修改。")
    options = ["-"] + [b["name"] for b in brands]
    for section, sec in _iter_sections(rows):
        st.markdown(f"**{SECTION_LABEL[section]}**（{len(sec)} 項）")
        st.caption(SECTION_NOTE[section])
        df, css = _cost_frame(sec, brands_k, prices, high, brand_map)
        if edit:
            cfg = {c: st.column_config.TextColumn(disabled=True, width="small") for c in df.columns}
            cfg["注音"] = st.column_config.TextColumn(disabled=True, width="small", pinned=True)
            cfg["品項"] = st.column_config.TextColumn(disabled=True, pinned=True)
            for col, _ in BRAND_COLS:
                cfg[col] = st.column_config.SelectboxColumn(options=options, width="small")
            ed = st.data_editor(_styled(df, css, band), hide_index=True, use_container_width=True,
                                height=_table_height(len(df)), column_config=cfg, key=f"cost_ed_{kind}_{section}")
            if st.button("💾 儲存廠牌", type="primary", key=f"cost_save_{kind}_{section}"):
                try:
                    n, synced = save_brand_edits(sec, df, ed, brands, clinic_id)
                except Exception as e:
                    st.error(f"儲存失敗：{e}（若訊息含 brand3_id，請先執行 migration_v10_prices.sql）")
                    return
                if n:
                    st.success(f"已更新 {n} 個品項的廠牌" + ("（已同步其他診所）" if synced else ""))
                    st.rerun()
                else:
                    st.info("沒有變更")
        else:
            st.dataframe(_styled(df, css, band), hide_index=True, use_container_width=True,
                         height=_table_height(len(df)), column_config=_view_cfg())


def page_price():
    st.header("💰 進價 / 成本表")
    role = st.session_state.user["role"]

    ptb = load_price_table_brands()
    if ptb is None:
        st.warning("尚未執行 migration_v10_prices.sql（進價資料表與參數尚未建立）。"
                   "請在 Supabase SQL Editor 執行該腳本後重新整理此頁。")
        return
    if not ptb:
        st.info("尚未設定表格廠牌，請至 系統設定 → 💰 進價表參數 新增廠牌欄位與成本倍數。")
        return

    high = load_high_price()
    clinics = load_clinics()
    selected_clinic = st.session_state.get("selected_clinic", "澤豐")

    modes = ["📋 進價表"] + (["💰 成本表（院長）"] if role == "admin" else [])
    c1, c2 = st.columns([3, 2])
    with c1:
        mode = st.radio("表格", modes, horizontal=True, label_visibility="collapsed", key="price_mode") \
            if len(modes) > 1 else modes[0]
    is_cost = mode.startswith("💰")

    # 進價全院共用；成本表的廠1/2/3、櫃位是各診所獨立 → 合併檢視時要先選診所
    clinic_name = selected_clinic
    if is_cost and selected_clinic == "合併檢視":
        with c2:
            clinic_name = st.selectbox("廠牌 / 櫃位顯示哪間診所", [c["name"] for c in clinics], key="price_cost_clinic")
    clinic_id = None if clinic_name == "合併檢視" else get_clinic_id(clinic_name)

    cs_map = load_clinic_stock_brands(clinic_id)
    products = load_products()
    cat_names = {c["id"]: c["name"] for c in load_categories()}
    prices = load_product_prices()
    brands = load_brands()
    brand_map = {b["id"]: b["name"] for b in brands}
    linked = build_linked_sources(products, cat_names)
    moves = plan_price_moves(products, cat_names, load_clinic_stock_brands(None), prices)
    if moves:
        try:
            apply_price_moves(moves)
            prices = load_product_prices()
            st.info("進價已依啟用狀態自動歸位：" + "；".join(
                f"{m['from']['name']} → {m['to']['name']}（{len(m['cells'])} 格）" for m in moves))
        except Exception as e:
            st.warning(f"進價自動歸位失敗：{e}")
    with c1:
        show_inactive = st.toggle("顯示停用品項", key="price_show_inactive",
                                  help="預設只列啟用中的品項；「特.」品項的健保來源（如 天麻B）即使停用也會列出以便填價。"
                                       "開啟後列出全部品項（灰字＝停用），匯出的 Excel 也跟著變。")
    tables = {k: (build_rows(k, cs_map, products, cat_names, linked, include_inactive=show_inactive),
                  [b for b in ptb if b["kind"] == k]) for k in KINDS}

    with c2:
        if is_cost:
            st.download_button("📥 匯出成本價目＆叫貨順序表 (Excel)",
                               build_cost_workbook(tables, prices, high, brand_map, clinic_name),
                               file_name=f"科中進貨成本價目&叫貨順序表_{clinic_name}_{date.today():%Y%m%d}.xlsx",
                               mime=XLSX_MIME, use_container_width=True)
        else:
            st.download_button("📥 匯出進價表 (Excel，列印用)",
                               build_price_workbook(tables, prices, high, date.today()),
                               file_name=f"科中進貨價目表_{date.today():%Y%m}.xlsx",
                               mime=XLSX_MIME, use_container_width=True)
    scope = "全部品項（灰字＝停用）" if show_inactive else (
        ("兩診所任一啟用" if clinic_id is None else f"**{clinic_name}** 啟用中") + " 的品項，加上「特.」品項的健保來源（停用者灰字）")
    if is_cost:
        st.caption(f"📍 廠牌 / 櫃位：**{clinic_name}**；進價與成本為全院共用。顯示 {scope}。")
    else:
        st.caption(f"📍 進價為全院共用（澤豐、澤沛同一份）。顯示 {scope}。")

    s1, s2 = st.columns([3, 2])
    with s1:
        query = st.text_input("🔍 搜尋品項", key="price_search",
                              placeholder="品名關鍵字，可多個（空格分隔，如「半夏 天麻」）" + ("；也可搜廠牌、櫃位" if is_cost else ""),
                              help="同時過濾 複方／單方 兩個 tab，三區位置不變；只影響畫面，Excel 匯出仍是完整表格。"
                                   "改搜尋字之前請先按儲存，未存的編輯會被清掉。")
    with s2:
        band_label = st.selectbox("列底色分組（方便左右對照）", list(BAND_OPTIONS), key="price_band",
                                  help="每 5 列（或每列）交錯上底色，看橫列更不易串行。編輯模式下 Streamlit 只會替不可編輯的欄位上色。")
    band = BAND_OPTIONS.get(band_label, 5)
    terms = search_terms(query)
    shown = {k: filter_rows(tables[k][0], terms, brand_map if is_cost else None) for k in KINDS}
    if terms:
        st.caption("🔍 符合「" + " ".join(terms) + "」：" + "、".join(f"{k} {len(shown[k])} 項" for k in KINDS)
                   + "。清空搜尋框即恢復完整表格。")

    tabs = st.tabs(list(KINDS))
    for kind, tab in zip(KINDS, tabs):
        with tab:
            rows, brands_k = shown[kind], tables[kind][1]
            if not brands_k:
                st.info(f"{kind} 尚未設定廠牌欄位，請至 系統設定 → 💰 進價表參數 新增。")
                continue
            if not rows:
                st.info(f"{kind} 沒有符合「{' '.join(terms)}」的品項。" if terms else "沒有啟用中的品項。")
                continue
            if is_cost:
                _render_cost_table(kind, rows, brands_k, prices, high[kind], brand_map, brands, clinic_id, band)
            else:
                _render_price_table(kind, rows, brands_k, prices, high[kind], band)


# ══════════════════════════════════════════════
#  系統設定 → 💰 進價表參數
# ══════════════════════════════════════════════

def render_price_params_tab():
    st.subheader("💰 進價 / 成本表參數")
    ptb = load_price_table_brands()
    if ptb is None:
        st.warning("尚未執行 migration_v10_prices.sql，請先在 Supabase SQL Editor 執行後再設定。")
        return

    high = load_high_price()
    st.markdown("**高價標示界線**（成本 ≥ 界線 → 進價表與成本表皆以橘色粗體標示）")
    h1, h2 = st.columns(2)
    with h1:
        hs = st.number_input("單方高價界線", value=float(high["單方"]), min_value=0.0, step=10.0, format="%.0f", key="hp_single")
    with h2:
        hc = st.number_input("複方高價界線", value=float(high["複方"]), min_value=0.0, step=10.0, format="%.0f", key="hp_compound")
    if st.button("儲存高價界線", key="hp_save"):
        save_high_price(hs, hc)
        st.success("已更新高價界線")

    st.divider()
    st.markdown("**表格廠牌欄位與成本倍數**")
    st.caption("每一列＝表格的一個廠牌欄位：選單方/複方、廠牌（從現有廠牌挑選）、成本倍數算式、顯示順序。"
               "算式可直接填分數運算，例如 `5/6*0.65`、`10/11*0.7`、`0.65/5`（K 裝 5 入換算每罐），成本＝進價×算出的倍數，無條件進位取整。"
               "要把其他廠牌加進表格就新增一列；刪除列即從表格移除（已填的進價仍保留在資料庫）。")
    brands = load_brands()
    bnames = [b["name"] for b in brands]
    name2id = {b["name"]: b["id"] for b in brands}
    cols = ["單複方", "廠牌", "成本倍數算式", "倍數值", "順序"]
    df = pd.DataFrame([{"單複方": r["kind"], "廠牌": r["name"], "成本倍數算式": r["formula"],
                        "倍數值": fmt_multiplier(r["multiplier"]), "順序": r["sort_order"]}
                       for r in ptb], columns=cols)
    for c in ("單複方", "廠牌", "成本倍數算式", "倍數值"):
        df[c] = df[c].astype(object)
    ed = st.data_editor(
        df, num_rows="dynamic", hide_index=True, use_container_width=True, key="ptb_editor",
        column_config={
            "單複方": st.column_config.SelectboxColumn(options=list(KINDS), required=True),
            "廠牌": st.column_config.SelectboxColumn(options=bnames, required=True),
            "成本倍數算式": st.column_config.TextColumn(required=True, help="例：5/6*0.65、0.7、0.65/5"),
            "倍數值": st.column_config.TextColumn(disabled=True, help="由算式算出，存檔後更新"),
            "順序": st.column_config.NumberColumn(min_value=0, step=1),
        })
    if st.button("儲存廠牌與倍數", type="primary", key="ptb_save"):
        rows, seen, errs = [], set(), []
        for _, r in ed.iterrows():
            kind, bname = r["單複方"], r["廠牌"]
            if pd.isna(kind) or pd.isna(bname) or bname not in name2id:
                continue
            key = (kind, name2id[bname])
            if key in seen:
                errs.append(f"{kind}「{bname}」重複")
                continue
            seen.add(key)
            formula = r["成本倍數算式"]
            formula = "0.7" if (formula is None or pd.isna(formula) or not str(formula).strip()) else str(formula).strip()
            try:
                mult = eval_formula(formula)
            except Exception as e:
                errs.append(f"{kind}「{bname}」{e}")
                continue
            order = r["順序"]
            rows.append({"kind": kind, "brand_id": name2id[bname], "multiplier": round(float(mult), 8),
                         "formula": formula[:50], "sort_order": int(order) if not pd.isna(order) else 0})
        if errs:
            st.error("；".join(errs))
        else:
            save_price_table_brands(rows)
            st.success(f"已儲存 {len(rows)} 個廠牌欄位")
            st.rerun()
