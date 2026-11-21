"""
💰 進價 / 成本表 — 品項各廠牌進價（全院共用）、成本價（進價 × 廠牌倍數，僅院長）、廠1/2/3 編輯、Excel 匯出、參數設定

資料表（migration_v10_prices.sql）：
  product_prices       product_id × brand_id → price（數字）或 note（文字，如「時價」「無」）
  price_table_brands   表格要顯示的廠牌欄位：kind(單方/複方)、brand_id、multiplier(成本倍數)、sort_order
  clinic_stock.brand3_id  第三廠牌（叫貨順序表「廠3」）
  system_settings      high_price_single / high_price_compound：成本 ≥ 界線 → 橘色粗體
"""

import io
import math
import re
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
SECTIONS = ("健保", "自費")
SECTION_LABEL = {"健保": "健保科中", "自費": "自費藥"}
HIGH_PRICE_KEYS = {"單方": "high_price_single", "複方": "high_price_compound"}
DEFAULT_HIGH_PRICE = {"單方": 280.0, "複方": 450.0}
ORANGE = "FFC000"
ORANGE_CSS = "color:#E08E00;font-weight:700"
# 成本表品名顏色（沿用原 Excel 慣例：A 櫃紅字、C 櫃藍字）
NAME_COLOR_BY_CABINET = {"A": "C00000", "C": "00B0F0"}
BRAND_COLS = (("廠1", "brand1_id"), ("廠2", "brand2_id"), ("廠3", "brand3_id"))
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ══════════════════════════════════════════════
#  基本工具
# ══════════════════════════════════════════════

def product_kind(cat_name) -> str:
    """分類名稱含「複方」→ 複方；其餘（單方、水藥材、非健保藥材…）→ 單方。"""
    return "複方" if "複方" in (cat_name or "") else "單方"


def product_section(cat_name) -> str:
    """分類名稱含「自費」或「高貴」→ 自費藥區（表格最下方）；其餘 → 健保區。"""
    name = cat_name or ""
    return "自費" if ("自費" in name or "高貴" in name) else "健保"


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


def parse_price_cell(text):
    """表格字串 → (price, note)。數字 → 進價；空白 → 都 None（＝刪除）；其他文字（時價/無…）→ note。"""
    s = "" if text is None else str(text).strip()
    if s in ("", "nan", "None", "-"):
        return None, None
    try:
        return float(s.replace(",", "")), None
    except ValueError:
        return None, s[:50]


def fmt_price(price, note=None) -> str:
    if price is not None:
        p = float(price)
        return str(int(p)) if p.is_integer() else f"{p:g}"
    return note or ""


def _sort_name(name: str) -> str:
    for prefix in ("特.", "特級"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


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

def build_rows(kind: str, cs_map: dict, products: list, cat_names: dict) -> list:
    """kind（單方/複方）的啟用品項列，依 區段(健保→自費) → 首字注音 → 全字注音 排序。"""
    rows = []
    for p in products:
        cname = cat_names.get(p["category_id"]) or (p.get("categories") or {}).get("name", "")
        if product_kind(cname) != kind:
            continue
        cs = cs_map.get(p["id"])
        if cs is None or not cs.get("is_active", True):
            continue
        rows.append({
            "pid": p["id"], "name": p["name"], "cat": cname, "section": product_section(cname),
            "initial": get_bopomofo_initial(p["name"]), "sort": get_bopomofo_sort_key(_sort_name(p["name"])),
            "cabinet": cs.get("cabinet") or "",
            "b1": cs.get("brand1_id"), "b2": cs.get("brand2_id"), "b3": cs.get("brand3_id"),
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
        rec, crec = {"注音": lab, "品項": r["name"]}, {"注音": "", "品項": ""}
        for b in brands_k:
            pr = prices.get((r["pid"], b["brand_id"]))
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
        rec, crec = {"注音": lab, "品項": r["name"]}, {c: "" for c in cols}
        g = _cabinet_letter(r["cabinet"])
        if g in NAME_COLOR_BY_CABINET:
            crec["品項"] = f"color:#{NAME_COLOR_BY_CABINET[g]}"
        for b in brands_k:
            pr = prices.get((r["pid"], b["brand_id"]))
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
    """yield (section, [rows])；自費區前空一列再放「自費藥」標記列。"""
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
        _setup_sheet(ws, kind, {"A": 4.5, "B": 16 if kind == "複方" else 11.4,
                                **{ws.cell(3, 3 + i).column_letter: 9 for i in range(len(brands_k))}})
        d = ws.cell(1, ncol, as_of)
        d.number_format = "yyyy/mm"
        d.font = _FONT
        _write_cell(ws, 3, 1)
        _write_cell(ws, 3, 2, "品項")
        for i, b in enumerate(brands_k):
            _write_cell(ws, 3, 3 + i, b["name"])
        r = 4
        for section, sec in _iter_sections(rows):
            if section == "自費":
                r += 1
                _write_cell(ws, r, 1, "自費藥")
                r += 1
            for x, lab in zip(sec, _initial_labels(sec)):
                _write_cell(ws, r, 1, lab or None)
                _write_cell(ws, r, 2, x["name"])
                for i, b in enumerate(brands_k):
                    pr = prices.get((x["pid"], b["brand_id"]))
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
        widths = {"A": 3.8, "B": 17.4 if kind == "複方" else 12.8}
        for i in range(nb):
            widths[ws.cell(3, 3 + i).column_letter] = 7
        for j in range(len(extra)):
            widths[ws.cell(3, 3 + nb + j).column_letter] = 5.5
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
            if section == "自費":
                r += 1
                _write_cell(ws, r, 1, "自費藥")
                r += 1
            for x, lab in zip(sec, _initial_labels(sec)):
                _write_cell(ws, r, 1, lab or None)
                g = _cabinet_letter(x["cabinet"])
                name_font = _font_color(NAME_COLOR_BY_CABINET[g]) if g in NAME_COLOR_BY_CABINET else _FONT
                _write_cell(ws, r, 2, x["name"], font=name_font)
                for i, b in enumerate(brands_k):
                    pr = prices.get((x["pid"], b["brand_id"]))
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
                r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════
#  頁面
# ══════════════════════════════════════════════

def _render_price_table(kind, rows, brands_k, prices, high):
    edit = st.toggle("✏️ 編輯進價", key=f"price_edit_{kind}")
    st.caption(f"成本 = 進價 × 廠牌倍數；成本 ≥ {high:g} 的進價以橘色粗體標示。"
               + ("直接在表格填寫後按「儲存進價」；清空＝刪除，可填「時價」等文字。" if edit
                  else "開啟「編輯進價」即可填寫。"))
    for section, sec in _iter_sections(rows):
        st.markdown(f"**{SECTION_LABEL[section]}**（{len(sec)} 項）")
        df, css = _price_frame(sec, brands_k, prices, high)
        if edit:
            cfg = {"注音": st.column_config.TextColumn(disabled=True, width="small"),
                   "品項": st.column_config.TextColumn(disabled=True)}
            for b in brands_k:
                cfg[b["name"]] = st.column_config.TextColumn(width="small")
            ed = st.data_editor(df, hide_index=True, use_container_width=True, height=_table_height(len(df)),
                                column_config=cfg, key=f"price_ed_{kind}_{section}")
            if st.button("💾 儲存進價", type="primary", key=f"price_save_{kind}_{section}"):
                n_up, n_del = save_price_edits(sec, brands_k, df, ed, prices)
                if n_up or n_del:
                    st.success(f"已儲存：更新 {n_up} 格、清除 {n_del} 格")
                    st.rerun()
                else:
                    st.info("沒有變更")
        else:
            st.dataframe(df.style.apply(lambda _: css, axis=None), hide_index=True,
                         use_container_width=True, height=_table_height(len(df)))


def _render_cost_table(kind, rows, brands_k, prices, high, brand_map, brands, clinic_id):
    edit = st.toggle("✏️ 編輯廠牌（廠1 / 廠2 / 廠3）", key=f"cost_edit_{kind}")
    sync = bool(load_global_params()["sync_brands"])
    st.caption(f"成本 = 進價 × 廠牌倍數（無條件進位取整）；≥ {high:g} 橘色粗體。品名紅字＝A 櫃、藍字＝C 櫃。"
               f"廠牌存檔後即同步到品項管理與庫存頁{'，並同步到其他診所' if sync else ''}；櫃位請到品項管理修改。")
    options = ["-"] + [b["name"] for b in brands]
    for section, sec in _iter_sections(rows):
        st.markdown(f"**{SECTION_LABEL[section]}**（{len(sec)} 項）")
        df, css = _cost_frame(sec, brands_k, prices, high, brand_map)
        if edit:
            cfg = {c: st.column_config.TextColumn(disabled=True, width="small") for c in df.columns}
            cfg["品項"] = st.column_config.TextColumn(disabled=True)
            for col, _ in BRAND_COLS:
                cfg[col] = st.column_config.SelectboxColumn(options=options, width="small")
            ed = st.data_editor(df, hide_index=True, use_container_width=True, height=_table_height(len(df)),
                                column_config=cfg, key=f"cost_ed_{kind}_{section}")
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
            st.dataframe(df.style.apply(lambda _: css, axis=None), hide_index=True,
                         use_container_width=True, height=_table_height(len(df)))


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
    tables = {k: (build_rows(k, cs_map, products, cat_names), [b for b in ptb if b["kind"] == k]) for k in KINDS}

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
    if is_cost:
        st.caption(f"📍 廠牌 / 櫃位：**{clinic_name}**；進價與成本為全院共用。")
    else:
        st.caption("📍 進價為全院共用（澤豐、澤沛同一份）。顯示 "
                   + ("兩診所任一啟用" if clinic_id is None else f"**{clinic_name}** 啟用中") + " 的品項。")

    tabs = st.tabs(list(KINDS))
    for kind, tab in zip(KINDS, tabs):
        with tab:
            rows, brands_k = tables[kind]
            if not brands_k:
                st.info(f"{kind} 尚未設定廠牌欄位，請至 系統設定 → 💰 進價表參數 新增。")
                continue
            if not rows:
                st.info("沒有啟用中的品項。")
                continue
            if is_cost:
                _render_cost_table(kind, rows, brands_k, prices, high[kind], brand_map, brands, clinic_id)
            else:
                _render_price_table(kind, rows, brands_k, prices, high[kind])


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
