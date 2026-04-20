"""
頁面模組 — 庫存、進退貨登錄、盤點、品項管理、數據分析、叫貨出表、系統設定
v2: per-clinic 廠牌/啟用、整數數量、庫存日期欄、盤點歷史可編輯、進退貨可修改刪除
"""

import io
import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from collections import defaultdict
from pypinyin import pinyin, Style
from database import get_supabase_client


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


def sort_products_by_bopomofo(products: list) -> list:
    return sorted(products, key=lambda p: (
        p.get("category_id", 0),
        get_bopomofo_sort_key(p["name"])
    ))


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


def short_date(d: str) -> str:
    """'2026-03-27' -> '03/27'"""
    return d[5:7] + "/" + d[8:10] if d and len(d) >= 10 else d or "-"


# ══════════════════════════════════════════════
#  共用資料載入函式
# ══════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_products():
    sb = get_supabase_client()
    data = sb.table("products").select(
        "id, name, category_id, unit_id, spec_note, "
        "categories(name), units(name)"
    ).execute().data
    return sort_products_by_bopomofo(data)

@st.cache_data(ttl=30)
def load_brands():
    sb = get_supabase_client()
    return sb.table("brands").select("id, name").order("id").execute().data

@st.cache_data(ttl=30)
def load_categories():
    sb = get_supabase_client()
    return sb.table("categories").select("id, name, default_unit_id, default_spec_note").order("id").execute().data

@st.cache_data(ttl=30)
def load_units():
    sb = get_supabase_client()
    return sb.table("units").select("id, name").order("id").execute().data

def get_clinic_id(clinic_name: str) -> int | None:
    sb = get_supabase_client()
    resp = sb.table("clinics").select("id").eq("name", clinic_name).execute()
    return resp.data[0]["id"] if resp.data else None


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

    col1, col2 = st.columns([2, 3])
    with col1:
        cat_filter = st.selectbox("分類", ["全部"] + [c["name"] for c in categories], key="stock_cat")
    with col2:
        search = st.text_input("搜尋", placeholder="中文 或 注音首碼（ee=葛根, 2e=當歸）", key="stock_search")

    # 載入品項
    products = sb.table("products").select(
        "id, name, category_id, categories(name), units(name)"
    ).execute().data
    products = sort_products_by_bopomofo(products)

    # 載入 per-clinic 資料
    cs_data = sb.table("clinic_stock").select(
        "product_id, current_stock, brand1_id, brand2_id, is_active"
    ).eq("clinic_id", clinic_id).execute().data
    cs_map = {s["product_id"]: s for s in cs_data}

    # 搜尋索引
    all_names = tuple(p["name"] for p in products)
    key_index = build_bopomofo_index(all_names)

    # 載入系統設定
    settings = sb.table("system_settings").select("*").execute().data
    safety_factor = float(next(s["value"] for s in settings if s["key"] == "safety_factor"))
    stock_multiplier = float(next(s["value"] for s in settings if s["key"] == "stock_target_multiplier"))

    # 載入盤點紀錄
    all_logs = sb.table("inventory_logs").select(
        "product_id, current_count_qty, consumed_qty, log_date, session_id"
    ).eq("clinic_id", clinic_id).order("log_date", desc=True).execute().data

    # 計算每品項平均耗用
    consumed_by_product = defaultdict(list)
    for log in all_logs:
        if log["consumed_qty"] is not None and log["consumed_qty"] > 0:
            consumed_by_product[log["product_id"]].append(int(log["consumed_qty"]))

    # 取得最近 3 個不重複的盤點日期
    seen_dates = []
    for log in all_logs:
        d = log["log_date"]
        if d not in seen_dates:
            seen_dates.append(d)
        if len(seen_dates) >= 3:
            break
    # seen_dates: [最新, ..., 最舊]; display: 最舊→最新
    display_dates = list(reversed(seen_dates))

    # 填滿 3 個位置: d3(最舊), d2, d1(最新)
    d3 = display_dates[0] if len(display_dates) >= 3 else None
    d2 = display_dates[1] if len(display_dates) >= 3 else (display_dates[0] if len(display_dates) >= 2 else None)
    d1 = display_dates[-1] if display_dates else None

    sd3 = short_date(d3) if d3 else "-"
    sd2 = short_date(d2) if d2 else "-"
    sd1 = short_date(d1) if d1 else "-"

    # 欄位標題
    h3 = f"盤({sd3})" if d3 else "盤(3)"
    h2 = f"盤({sd2})" if d2 else "盤(2)"
    h1 = f"盤({sd1})" if d1 else "盤(1)"
    hr32 = f"進({sd3}~{sd2})" if d3 and d2 else "進(3~2)"
    hc32 = f"耗({sd3}~{sd2})" if d3 and d2 else "耗(3~2)"
    hr21 = f"進({sd2}~{sd1})" if d2 and d1 and d2 != d1 else "進(2~1)"
    hc21 = f"耗({sd2}~{sd1})" if d2 and d1 and d2 != d1 else "耗(2~1)"

    # 每品項盤點紀錄
    product_log_map = defaultdict(dict)
    for log in all_logs:
        pid = log["product_id"]
        d = log["log_date"]
        if d not in product_log_map[pid]:
            product_log_map[pid][d] = log

    # 進退貨
    all_tx = sb.table("transactions").select(
        "product_id, change_qty, tx_date"
    ).eq("clinic_id", clinic_id).order("tx_date").execute().data
    tx_by_product = defaultdict(list)
    for tx in all_tx:
        tx_by_product[tx["product_id"]].append(tx)

    # 組裝表格（固定 15 欄）
    rows = []
    product_id_list = []

    for p in products:
        pid = p["id"]
        cs = cs_map.get(pid)
        if not cs or not cs.get("is_active", True):
            continue
        if cat_filter != "全部" and p["categories"]["name"] != cat_filter:
            continue
        if search and not match_search(p["name"], key_index.get(p["name"], ("", "")), search):
            continue

        p_logs = product_log_map.get(pid, {})
        log3 = p_logs.get(d3) if d3 else None
        log2 = p_logs.get(d2) if d2 else None
        log1 = p_logs.get(d1) if d1 else None

        v3 = int(log3["current_count_qty"]) if log3 else None
        v2 = int(log2["current_count_qty"]) if log2 else None
        v1 = int(log1["current_count_qty"]) if log1 else None

        # 進貨 3→2
        if d3 and d2 and d3 != d2:
            r32 = sum(int(t["change_qty"]) for t in tx_by_product.get(pid, []) if d3 < t["tx_date"] <= d2)
        else:
            r32 = None
        c32 = (v3 + r32 - v2) if (v3 is not None and v2 is not None and r32 is not None) else None

        # 進貨 2→1
        if d2 and d1 and d2 != d1:
            r21 = sum(int(t["change_qty"]) for t in tx_by_product.get(pid, []) if d2 < t["tx_date"] <= d1)
        else:
            r21 = None
        c21 = (v2 + r21 - v1) if (v2 is not None and v1 is not None and r21 is not None) else None

        # 進貨迄今
        r_now = sum(int(t["change_qty"]) for t in tx_by_product.get(pid, []) if t["tx_date"] > d1) if d1 else 0

        current_stock = int(cs["current_stock"]) if cs else 0

        # 建議叫貨
        avg_vals = consumed_by_product.get(pid, [])
        avg_c = sum(avg_vals) / len(avg_vals) if avg_vals else 0
        if avg_c > 0 and current_stock <= avg_c * safety_factor:
            suggested = max(0, round(avg_c * stock_multiplier - current_stock))
        else:
            suggested = 0

        row = {
            "品項": p["name"],
            "分類": p["categories"]["name"],
            "廠牌1": brand_map.get(cs.get("brand1_id"), "-"),
            "廠牌2": brand_map.get(cs.get("brand2_id"), "-"),
            h3: v3, hr32: r32, hc32: c32,
            h2: v2, hr21: r21, hc21: c21,
            h1: v1,
            "進(迄今)": r_now,
            "即時庫存": current_stock,
            "建議叫貨": suggested,
            "叫貨": suggested,
        }
        rows.append(row)
        product_id_list.append(pid)

    if not rows:
        st.info("沒有符合條件的品項")
        return

    df = pd.DataFrame(rows)

    # 欄位設定：只有「叫貨」可編輯
    col_config = {
        "品項": st.column_config.TextColumn(disabled=True),
        "分類": st.column_config.TextColumn(disabled=True),
        "廠牌1": st.column_config.TextColumn(disabled=True),
        "廠牌2": st.column_config.TextColumn(disabled=True),
        h3: st.column_config.NumberColumn(disabled=True, format="%d"),
        hr32: st.column_config.NumberColumn(disabled=True, format="%d"),
        hc32: st.column_config.NumberColumn(disabled=True, format="%d"),
        h2: st.column_config.NumberColumn(disabled=True, format="%d"),
        hr21: st.column_config.NumberColumn(disabled=True, format="%d"),
        hc21: st.column_config.NumberColumn(disabled=True, format="%d"),
        h1: st.column_config.NumberColumn(disabled=True, format="%d"),
        "進(迄今)": st.column_config.NumberColumn(disabled=True, format="%d"),
        "即時庫存": st.column_config.NumberColumn(disabled=True, format="%d"),
        "建議叫貨": st.column_config.NumberColumn(disabled=True, format="%d"),
        "叫貨": st.column_config.NumberColumn("叫貨 ✏️", min_value=0, format="%d"),
    }

    edited_stock_df = st.data_editor(
        df, use_container_width=True, hide_index=True,
        height=min(len(df) * 35 + 38, 700),
        column_config=col_config, key="stock_editor",
    )

    # 送出叫貨
    order_items = edited_stock_df[edited_stock_df["叫貨"] > 0]
    if not order_items.empty:
        st.markdown(f"**共 {len(order_items)} 項需要叫貨**")
        if st.button("🛒 送出叫貨清單", type="primary"):
            order_data = order_items[["品項", "分類", "廠牌1", "即時庫存", "叫貨"]].copy()
            order_data.columns = ["品項", "分類", "廠牌", "目前庫存", "叫貨數量"]

            st.subheader("叫貨清單")
            for bname, group in order_data.groupby("廠牌"):
                if not bname or bname == "-":
                    bname = "未指定"
                st.markdown(f"### 【{bname}】")
                st.dataframe(group[["品項", "叫貨數量"]], use_container_width=True, hide_index=True)

            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                for bname, group in order_data.groupby("廠牌"):
                    if not bname or bname == "-":
                        bname = "未指定"
                    group[["品項", "叫貨數量"]].to_excel(writer, sheet_name=str(bname)[:31], index=False)
            st.download_button("📥 下載叫貨單 (.xlsx)", data=buf.getvalue(),
                file_name=f"叫貨單_{selected_clinic}_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True)

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
            fix_options = {f"{l['log_date']} — 數量: {int(l['current_count_qty'])}": l for l in fix_logs}
            selected_fix = st.selectbox("選擇要修正的盤點紀錄", list(fix_options.keys()), key="fix_log")
            fix_log = fix_options[selected_fix]

            new_qty = st.number_input("修正後數量", value=int(fix_log["current_count_qty"]), step=1, key="fix_qty")

            if st.button("改錯存檔", type="primary"):
                try:
                    sb.table("inventory_logs").update(
                        {"current_count_qty": new_qty}
                    ).eq("id", fix_log["id"]).execute()

                    if fix_log == fix_logs[0]:
                        restock_after = sum(
                            int(t["change_qty"]) for t in tx_by_product.get(fix_pid, [])
                            if t["tx_date"] > fix_log["log_date"]
                        )
                        new_stock = new_qty + restock_after
                        sb.table("clinic_stock").update(
                            {"current_stock": new_stock}
                        ).eq("product_id", fix_pid).eq("clinic_id", clinic_id).execute()

                    st.success(f"已修正：{selected_name} {fix_log['log_date']} → {new_qty}")
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
        products = sb.table("products").select(
            "id, name, category_id, categories(name), units(name)"
        ).execute().data
        products = sort_products_by_bopomofo(products)

        # per-clinic 啟用篩選
        cs_data = sb.table("clinic_stock").select(
            "product_id, current_stock, is_active"
        ).eq("clinic_id", clinic_id).execute().data
        cs_map = {s["product_id"]: s for s in cs_data}
        products = [p for p in products if cs_map.get(p["id"], {}).get("is_active", True)]

        stock_map = {s["product_id"]: int(s["current_stock"]) for s in cs_data}

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

        with st.form("tx_form"):
            col1, col2, col3 = st.columns(3)
            with col1:
                tx_type = st.selectbox("異動類型", ["進貨", "調撥", "廢棄"])
            with col2:
                qty = st.number_input("數量（負數=調出/廢棄）", value=0, step=1)
            with col3:
                tx_date = st.date_input("日期", value=date.today())

            col4, col5 = st.columns(2)
            with col4:
                tx_brands = load_brands()
                tx_brand_options = ["-"] + [b["name"] for b in tx_brands]
                tx_brand = st.selectbox("廠牌", tx_brand_options, key="tx_brand")
            with col5:
                note = st.text_input("備註（選填）")

            if st.form_submit_button("確認登錄", type="primary", use_container_width=True):
                if qty == 0:
                    st.error("數量不可為 0")
                else:
                    try:
                        full_note = f"[{tx_brand}] {note}".strip() if tx_brand != "-" else (note or None)
                        sb.table("transactions").insert({
                            "product_id": product["id"], "clinic_id": clinic_id,
                            "change_qty": qty, "tx_date": str(tx_date),
                            "tx_type": tx_type, "note": full_note, "created_by": user["id"],
                        }).execute()

                        new_stock = current_stock + qty
                        sb.table("clinic_stock").update(
                            {"current_stock": new_stock}
                        ).eq("product_id", product["id"]).eq("clinic_id", clinic_id).execute()

                        st.success(f"已登錄：{product['name']} {qty:+d}（庫存 {current_stock} → {new_stock}）")
                    except Exception as e:
                        st.error(f"登錄失敗：{e}")

    with tab_history:
        sb = get_supabase_client()
        col1, col2 = st.columns(2)
        with col1:
            days_filter = st.selectbox("時間範圍", ["最近 30 天", "最近 7 天", "最近 90 天", "全部"], key="tx_days")
        with col2:
            type_filter = st.selectbox("類型", ["全部", "進貨", "調撥", "廢棄"], key="tx_type_filter")

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

        resp = query.limit(200).execute()

        if not resp.data:
            st.info("沒有異動紀錄")
        else:
            hist_rows = [{
                "日期": t["tx_date"], "品項": t["products"]["name"], "類型": t["tx_type"],
                "數量": int(t["change_qty"]), "單位": t["products"]["units"]["name"],
                "備註": t["note"] or "", "操作人": t["users"]["display_name"] if t.get("users") else "-",
            } for t in resp.data]

            hist_df = pd.DataFrame(hist_rows)
            styled = hist_df.style.map(
                lambda v: "color:#DC3545;font-weight:bold" if isinstance(v, (int, float)) and v < 0 else (
                    "color:#28A745" if isinstance(v, (int, float)) and v > 0 else ""),
                subset=["數量"],
            )
            st.dataframe(styled, use_container_width=True, hide_index=True, height=400)

            # 修改/刪除
            st.divider()
            st.subheader("修改或刪除紀錄")

            tx_options = {
                f"#{t['id']} | {t['tx_date']} | {t['products']['name']} | {int(t['change_qty']):+d}": t
                for t in resp.data
            }
            selected_tx_key = st.selectbox("選擇紀錄", list(tx_options.keys()), key="tx_hist_select")
            tx_item = tx_options[selected_tx_key]

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                edit_qty = st.number_input("數量", value=int(tx_item["change_qty"]), step=1, key="tx_edit_qty")
            with col_b:
                type_list = ["進貨", "調撥", "廢棄"]
                edit_type = st.selectbox("類型", type_list,
                                         index=type_list.index(tx_item["tx_type"]), key="tx_edit_type")
            with col_c:
                edit_note = st.text_input("備註", value=tx_item["note"] or "", key="tx_edit_note")

            col_save, col_del = st.columns(2)
            with col_save:
                if st.button("💾 修改此筆", type="primary", key="tx_save_btn"):
                    try:
                        diff = edit_qty - int(tx_item["change_qty"])
                        sb.table("transactions").update({
                            "change_qty": edit_qty, "tx_type": edit_type,
                            "note": edit_note or None,
                        }).eq("id", tx_item["id"]).execute()
                        if diff != 0:
                            fresh = sb.table("clinic_stock").select("current_stock").eq(
                                "product_id", tx_item["product_id"]).eq("clinic_id", clinic_id).execute().data
                            old_stock = int(fresh[0]["current_stock"]) if fresh else 0
                            sb.table("clinic_stock").update(
                                {"current_stock": old_stock + diff}
                            ).eq("product_id", tx_item["product_id"]).eq("clinic_id", clinic_id).execute()
                        st.success("已修改")
                        st.rerun()
                    except Exception as e:
                        st.error(f"修改失敗：{e}")

            with col_del:
                if st.button("🗑️ 刪除此筆", type="secondary", key="tx_del_btn"):
                    try:
                        old_qty = int(tx_item["change_qty"])
                        fresh = sb.table("clinic_stock").select("current_stock").eq(
                            "product_id", tx_item["product_id"]).eq("clinic_id", clinic_id).execute().data
                        old_stock = int(fresh[0]["current_stock"]) if fresh else 0
                        sb.table("clinic_stock").update(
                            {"current_stock": old_stock - old_qty}
                        ).eq("product_id", tx_item["product_id"]).eq("clinic_id", clinic_id).execute()
                        sb.table("transactions").delete().eq("id", tx_item["id"]).execute()
                        st.success("已刪除")
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

    tab_do, tab_print, tab_history = st.tabs(["📝 執行盤點", "🖨️ 列印盤點表", "📜 盤點歷史"])

    # ── 列印盤點表 ──
    with tab_print:
        st.subheader("🖨️ 列印空白盤點表")
        st.caption("下載 Excel，列印後拿紙本到藥櫃盤點，再回來建檔")

        print_cat = st.selectbox("分類", ["全部"] + [c["name"] for c in load_categories()], key="print_cat")

        products = sb.table("products").select(
            "name, category_id, categories(name), units(name)"
        ).execute().data
        products = sort_products_by_bopomofo(products)

        # per-clinic 啟用
        cs_active = sb.table("clinic_stock").select("product_id, is_active").eq("clinic_id", clinic_id).execute().data
        active_set = {s["product_id"] for s in cs_active if s.get("is_active", True)}
        # Note: print products don't have id, need to filter differently
        # Actually we need id for filtering, let me reload with id
        products_with_id = sb.table("products").select(
            "id, name, category_id, categories(name), units(name)"
        ).execute().data
        products_with_id = sort_products_by_bopomofo(products_with_id)
        products_with_id = [p for p in products_with_id if p["id"] in active_set]

        if print_cat != "全部":
            products_with_id = [p for p in products_with_id if p["categories"]["name"] == print_cat]

        print_rows = [{"品項名稱": p["name"], "分類": p["categories"]["name"],
                       "單位": p["units"]["name"], "盤點數量": ""} for p in products_with_id]

        if print_rows:
            print_df = pd.DataFrame(print_rows)

            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                print_df.to_excel(writer, index=False, sheet_name="盤點表")
                ws = writer.sheets["盤點表"]
                ws.column_dimensions["A"].width = 30
                ws.column_dimensions["B"].width = 15
                ws.column_dimensions["C"].width = 8
                ws.column_dimensions["D"].width = 12

            st.download_button(
                f"📥 下載盤點表（{len(print_df)} 品項）",
                data=buf.getvalue(),
                file_name=f"盤點表_{selected_clinic}_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, type="primary",
            )
            st.dataframe(print_df, use_container_width=True, hide_index=True, height=300)

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

            products = sb.table("products").select(
                "id, name, category_id, categories(name), units(name)"
            ).execute().data
            products = sort_products_by_bopomofo(products)

            # per-clinic 啟用篩選
            cs_data = sb.table("clinic_stock").select(
                "product_id, current_stock, is_active"
            ).eq("clinic_id", clinic_id).execute().data
            cs_map = {s["product_id"]: s for s in cs_data}
            products = [p for p in products if cs_map.get(p["id"], {}).get("is_active", True)]

            stock_map = {s["product_id"]: int(s["current_stock"]) for s in cs_data}

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
                        "帳面庫存": st.column_config.NumberColumn(disabled=True, format="%d"),
                        "盤點數量": st.column_config.NumberColumn("盤點數量 ✏️", min_value=0, format="%d"),
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
                            entries.append((idx, int(row["盤點數量"])))

                    if not entries:
                        st.error("沒有填入任何盤點數量，請重新填寫後再送出。")
                    else:
                        try:
                            # 取得每個品項的最新盤點
                            last_logs = sb.table("inventory_logs").select(
                                "product_id, current_count_qty, log_date"
                            ).eq("clinic_id", clinic_id).order("log_date", desc=True).execute().data

                            last_count_map, last_date_map = {}, {}
                            for log in last_logs:
                                pid = log["product_id"]
                                if pid not in last_count_map:
                                    last_count_map[pid] = int(log["current_count_qty"])
                                    last_date_map[pid] = log["log_date"]

                            all_tx = sb.table("transactions").select(
                                "product_id, change_qty, tx_date"
                            ).eq("clinic_id", clinic_id).execute().data

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
                                product_id = df.iloc[idx]["product_id"]
                                last_qty = last_count_map.get(product_id, stock_map.get(product_id, 0))
                                last_dt = last_date_map.get(product_id, "1900-01-01")

                                restock_sum = sum(
                                    int(t["change_qty"]) for t in tx_by_pid.get(product_id, [])
                                    if last_dt < t["tx_date"] <= str(inv_date)
                                )
                                consumed = last_qty + restock_sum - count_qty

                                logs_to_insert.append({
                                    "session_id": session_id, "product_id": product_id,
                                    "clinic_id": clinic_id,
                                    "last_count_qty": last_qty,
                                    "restock_qty_since_last": restock_sum,
                                    "current_count_qty": count_qty,
                                    "consumed_qty": consumed,
                                    "log_date": str(inv_date),
                                })

                                # 更新即時庫存（加上盤點日之後的進貨）
                                restock_after = sum(
                                    int(t["change_qty"]) for t in tx_by_pid.get(product_id, [])
                                    if t["tx_date"] > str(inv_date)
                                )
                                new_stock = count_qty + restock_after
                                sb.table("clinic_stock").update(
                                    {"current_stock": new_stock}
                                ).eq("product_id", product_id).eq("clinic_id", clinic_id).execute()

                                results.append({
                                    "品項": edited_df.iloc[idx]["品項名稱"],
                                    "上次盤點": last_qty,
                                    "期間進貨": restock_sum, "本次盤點": count_qty, "耗用量": consumed,
                                })

                            sb.table("inventory_logs").insert(logs_to_insert).execute()

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
            for s in sessions:
                operator = s["users"]["display_name"] if s.get("users") else "-"
                with st.expander(f"📋 {s['session_date']}（{operator}）", expanded=False):
                    logs = sb.table("inventory_logs").select(
                        "id, product_id, products(name, category_id, units(name)), "
                        "last_count_qty, restock_qty_since_last, current_count_qty, consumed_qty"
                    ).eq("session_id", s["id"]).execute().data
                    # 依分類+注音排序
                    logs = sorted(logs, key=lambda l: (
                        l["products"].get("category_id", 0),
                        get_bopomofo_sort_key(l["products"]["name"])
                    ))

                    if not logs:
                        st.info("此盤點批次沒有明細紀錄（可能是空批次）")

                        # admin 可刪除空批次
                        if user["role"] == "admin":
                            if st.button(f"🗑️ 刪除此空批次", key=f"del_empty_{s['id']}"):
                                sb.table("inventory_sessions").delete().eq("id", s["id"]).execute()
                                st.success("已刪除")
                                st.rerun()
                    else:
                        # 顯示可編輯的表格
                        log_rows = []
                        log_ids = []
                        log_pids = []
                        for l in logs:
                            log_rows.append({
                                "品項": l["products"]["name"],
                                "單位": l["products"]["units"]["name"],
                                "上次盤點": int(l["last_count_qty"]) if l["last_count_qty"] is not None else 0,
                                "期間進貨": int(l["restock_qty_since_last"]) if l["restock_qty_since_last"] is not None else 0,
                                "盤點數量": int(l["current_count_qty"]) if l["current_count_qty"] is not None else 0,
                                "耗用量": int(l["consumed_qty"]) if l["consumed_qty"] is not None else 0,
                            })
                            log_ids.append(l["id"])
                            log_pids.append(l["product_id"])

                        log_df = pd.DataFrame(log_rows)

                        edited_log_df = st.data_editor(
                            log_df,
                            use_container_width=True, hide_index=True,
                            column_config={
                                "品項": st.column_config.TextColumn(disabled=True),
                                "單位": st.column_config.TextColumn(disabled=True),
                                "上次盤點": st.column_config.NumberColumn(disabled=True, format="%d"),
                                "期間進貨": st.column_config.NumberColumn(disabled=True, format="%d"),
                                "盤點數量": st.column_config.NumberColumn("盤點數量 ✏️", min_value=0, format="%d"),
                                "耗用量": st.column_config.NumberColumn(disabled=True, format="%d"),
                            },
                            key=f"hist_editor_{s['id']}",
                        )

                        col_save, col_del = st.columns([3, 1])
                        with col_save:
                            if st.button("💾 儲存修改", key=f"hist_save_{s['id']}", type="primary"):
                                updated = 0
                                for idx in range(len(edited_log_df)):
                                    old_qty = log_df.iloc[idx]["盤點數量"]
                                    new_qty = int(edited_log_df.iloc[idx]["盤點數量"])
                                    if old_qty != new_qty:
                                        last_qty = log_df.iloc[idx]["上次盤點"]
                                        restock = log_df.iloc[idx]["期間進貨"]
                                        new_consumed = last_qty + restock - new_qty

                                        sb.table("inventory_logs").update({
                                            "current_count_qty": new_qty,
                                            "consumed_qty": new_consumed,
                                        }).eq("id", log_ids[idx]).execute()

                                        # 如果是最新一次盤點，更新庫存
                                        latest_log = sb.table("inventory_logs").select(
                                            "id, log_date"
                                        ).eq("product_id", log_pids[idx]).eq(
                                            "clinic_id", clinic_id
                                        ).order("log_date", desc=True).limit(1).execute().data

                                        if latest_log and latest_log[0]["id"] == log_ids[idx]:
                                            all_tx = sb.table("transactions").select(
                                                "change_qty, tx_date"
                                            ).eq("product_id", log_pids[idx]).eq(
                                                "clinic_id", clinic_id
                                            ).gt("tx_date", s["session_date"]).execute().data
                                            restock_after = sum(int(t["change_qty"]) for t in all_tx)
                                            sb.table("clinic_stock").update(
                                                {"current_stock": new_qty + restock_after}
                                            ).eq("product_id", log_pids[idx]).eq("clinic_id", clinic_id).execute()

                                        updated += 1

                                if updated > 0:
                                    st.success(f"已更新 {updated} 筆")
                                    st.rerun()
                                else:
                                    st.info("沒有變更")

                        with col_del:
                            if user["role"] == "admin":
                                if st.button("🗑️ 刪除整筆", key=f"hist_del_{s['id']}", type="secondary"):
                                    try:
                                        # 刪除前，還原庫存
                                        for idx, l in enumerate(logs):
                                            pid = l["product_id"]
                                            # 檢查這筆是否為該品項最新的 log
                                            latest = sb.table("inventory_logs").select(
                                                "id, log_date"
                                            ).eq("product_id", pid).eq(
                                                "clinic_id", clinic_id
                                            ).order("log_date", desc=True).limit(1).execute().data

                                            if latest and latest[0]["id"] == l["id"]:
                                                # 是最新的 log — 刪除後需要退回到上一次盤點
                                                prev = sb.table("inventory_logs").select(
                                                    "current_count_qty, log_date"
                                                ).eq("product_id", pid).eq(
                                                    "clinic_id", clinic_id
                                                ).order("log_date", desc=True).limit(2).execute().data

                                                if len(prev) >= 2:
                                                    prev_qty = int(prev[1]["current_count_qty"])
                                                    prev_date = prev[1]["log_date"]
                                                else:
                                                    prev_qty = 0
                                                    prev_date = "1900-01-01"

                                                # 加上 prev_date 之後所有進貨
                                                tx_after = sb.table("transactions").select(
                                                    "change_qty"
                                                ).eq("product_id", pid).eq(
                                                    "clinic_id", clinic_id
                                                ).gt("tx_date", prev_date).execute().data
                                                restock_total = sum(int(t["change_qty"]) for t in tx_after)

                                                sb.table("clinic_stock").update(
                                                    {"current_stock": prev_qty + restock_total}
                                                ).eq("product_id", pid).eq("clinic_id", clinic_id).execute()

                                        sb.table("inventory_logs").delete().eq("session_id", s["id"]).execute()
                                        sb.table("inventory_sessions").delete().eq("id", s["id"]).execute()
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

    products = load_products()
    brands = load_brands()
    categories = load_categories()
    units = load_units()

    brand_map = {b["id"]: b["name"] for b in brands}
    unit_map = {u["id"]: u["name"] for u in units}

    sb = get_supabase_client()

    # 載入 per-clinic 資料
    cs_data = sb.table("clinic_stock").select(
        "product_id, brand1_id, brand2_id, is_active"
    ).eq("clinic_id", clinic_id).execute().data
    cs_map = {s["product_id"]: s for s in cs_data}

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
                            sb.table("products").update(product_changes).eq("id", pid).execute()
                            updated += 1

                        # per-clinic 欄位 → 更新 clinic_stock 表
                        cs_changes = {}
                        if old["第一廠牌"] != new["第一廠牌"]:
                            cs_changes["brand1_id"] = next((b["id"] for b in brands if b["name"] == new["第一廠牌"]), None)
                        if old["第二廠牌"] != new["第二廠牌"]:
                            cs_changes["brand2_id"] = next((b["id"] for b in brands if b["name"] == new["第二廠牌"]), None)
                        if old["啟用"] != new["啟用"]:
                            cs_changes["is_active"] = bool(new["啟用"])

                        if cs_changes:
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
                st.dataframe(df, use_container_width=True, hide_index=True, height=min(len(df) * 35 + 38, 600))
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

    all_logs = []
    for cid in clinic_ids:
        resp = sb.table("inventory_logs").select(
            "product_id, consumed_qty, products(name, category_id, categories(name), units(name))"
        ).eq("clinic_id", cid).order("log_date", desc=True).execute()
        all_logs.extend(resp.data)

    if not all_logs:
        st.info("尚無盤點資料。")
        return

    product_consumed = defaultdict(list)
    product_info = {}
    for log in all_logs:
        pid = log["product_id"]
        if log["consumed_qty"] is not None and log["consumed_qty"] > 0:
            product_consumed[pid].append(int(log["consumed_qty"]))
        product_info[pid] = {
            "name": log["products"]["name"],
            "category": log["products"]["categories"]["name"],
            "unit": log["products"]["units"]["name"],
        }

    tab_reorder, tab_ranking = st.tabs(["📦 建議叫貨", "🏆 常用排名"])

    with tab_reorder:
        settings = sb.table("system_settings").select("*").execute().data
        safety = float(next(s["value"] for s in settings if s["key"] == "safety_factor"))
        multiplier = float(next(s["value"] for s in settings if s["key"] == "stock_target_multiplier"))

        stock_data = []
        for cid in clinic_ids:
            resp = sb.table("clinic_stock").select("product_id, current_stock, clinic_id").eq("clinic_id", cid).execute()
            stock_data.extend(resp.data)

        stock_map = {(s["product_id"], s["clinic_id"]): int(s["current_stock"]) for s in stock_data}

        reorder_rows = []
        for pid, consumed_list in product_consumed.items():
            avg = sum(consumed_list) / len(consumed_list)
            if avg <= 0:
                continue
            info = product_info[pid]
            for cid in clinic_ids:
                current = stock_map.get((pid, cid), 0)
                if current < avg * safety:
                    reorder_rows.append({
                        "診所": "澤豐" if cid == 1 else "澤沛",
                        "品項": info["name"], "分類": info["category"],
                        "目前庫存": current, "平均耗用": round(avg),
                        "建議叫貨": max(0, round(avg * multiplier - current)),
                    })

        if reorder_rows:
            st.dataframe(pd.DataFrame(reorder_rows).sort_values(["診所", "分類"]),
                         use_container_width=True, hide_index=True)
        else:
            st.success("所有品項庫存充足")

    with tab_ranking:
        categories = load_categories()
        rank_cat = st.selectbox("分類", ["全部"] + [c["name"] for c in categories], key="rank_cat")

        ranking = []
        for pid, consumed_list in product_consumed.items():
            info = product_info[pid]
            if rank_cat != "全部" and info["category"] != rank_cat:
                continue
            total = sum(consumed_list)
            if total > 0:
                ranking.append({"品項": info["name"], "分類": info["category"],
                                "總耗用量": total, "平均耗用": round(total / len(consumed_list))})

        if ranking:
            rank_df = pd.DataFrame(ranking).sort_values("總耗用量", ascending=False)
            st.bar_chart(rank_df.head(15).set_index("品項")[["總耗用量"]], color="#6A5ACD")
            st.dataframe(rank_df, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════
#  6. 叫貨出表（per-clinic 廠牌）
# ══════════════════════════════════════════════

def page_order():
    st.header("🛒 叫貨出表")

    selected_clinic = st.session_state.get("selected_clinic", "澤豐")
    if selected_clinic == "合併檢視":
        st.info("請先選擇單一診所。")
        return

    clinic_id = get_clinic_id(selected_clinic)
    if not clinic_id:
        return

    sb = get_supabase_client()

    settings = sb.table("system_settings").select("*").execute().data
    safety = float(next(s["value"] for s in settings if s["key"] == "safety_factor"))
    multiplier = float(next(s["value"] for s in settings if s["key"] == "stock_target_multiplier"))

    products = sb.table("products").select(
        "id, name, category_id, categories(name), units(name)"
    ).execute().data
    products = sort_products_by_bopomofo(products)

    brands = load_brands()
    brand_map = {b["id"]: b["name"] for b in brands}
    brand_names = [b["name"] for b in brands]

    # per-clinic 資料
    cs_data = sb.table("clinic_stock").select(
        "product_id, current_stock, brand1_id, is_active"
    ).eq("clinic_id", clinic_id).execute().data
    cs_map = {s["product_id"]: s for s in cs_data}

    logs = sb.table("inventory_logs").select("product_id, consumed_qty").eq("clinic_id", clinic_id).execute().data
    consumed_data = defaultdict(list)
    for l in logs:
        if l["consumed_qty"] and l["consumed_qty"] > 0:
            consumed_data[l["product_id"]].append(int(l["consumed_qty"]))

    st.subheader("步驟一：編輯叫貨清單")
    show_all = st.checkbox("顯示所有品項", value=False)

    order_rows = []
    for p in products:
        cs = cs_map.get(p["id"])
        if not cs or not cs.get("is_active", True):
            continue

        current = int(cs["current_stock"])
        vals = consumed_data.get(p["id"], [])
        avg = sum(vals) / len(vals) if vals else 0
        auto = current < avg * safety and avg > 0
        suggested = max(0, round(avg * multiplier - current)) if auto else 0

        order_rows.append({
            "勾選": auto, "品項": p["name"], "分類": p["categories"]["name"],
            "單位": p["units"]["name"], "目前庫存": current,
            "建議數量": suggested, "叫貨數量": suggested,
            "廠牌": brand_map.get(cs.get("brand1_id"), ""),
        })

    order_df = pd.DataFrame(order_rows)
    display = order_df if show_all else order_df[order_df["勾選"]]

    if display.empty:
        st.success("所有品項庫存充足！")
        return

    edited = st.data_editor(
        display[["勾選", "品項", "分類", "單位", "目前庫存", "建議數量", "叫貨數量", "廠牌"]],
        use_container_width=True, hide_index=True,
        column_config={
            "勾選": st.column_config.CheckboxColumn("✅"),
            "品項": st.column_config.TextColumn(disabled=True),
            "分類": st.column_config.TextColumn(disabled=True),
            "單位": st.column_config.TextColumn(disabled=True),
            "目前庫存": st.column_config.NumberColumn(disabled=True, format="%d"),
            "建議數量": st.column_config.NumberColumn(disabled=True, format="%d"),
            "叫貨數量": st.column_config.NumberColumn("數量 ✏️", min_value=0, format="%d"),
            "廠牌": st.column_config.SelectboxColumn(options=brand_names),
        },
        height=min(len(display) * 35 + 38, 500), key="order_editor",
    )

    st.divider()
    st.subheader("步驟二：依廠牌分組")

    selected = edited[(edited["勾選"]) & (edited["叫貨數量"] > 0)]
    if selected.empty:
        st.info("請勾選要叫貨的品項")
        return

    brand_dfs = {}
    for brand_name, group in selected.groupby("廠牌"):
        if not brand_name:
            brand_name = "未指定"
        st.markdown(f"### 【{brand_name}】")
        bdf = group[["品項", "叫貨數量", "單位"]].copy()
        edited_brand = st.data_editor(bdf, use_container_width=True, hide_index=True,
                                       column_config={"品項": st.column_config.TextColumn(disabled=True),
                                                      "單位": st.column_config.TextColumn(disabled=True),
                                                      "叫貨數量": st.column_config.NumberColumn(format="%d")},
                                       key=f"brand_{brand_name}")
        brand_dfs[brand_name] = edited_brand
        st.caption(f"{len(edited_brand)} 品項")

    st.divider()
    st.subheader("步驟三：匯出")

    col1, col2 = st.columns(2)
    with col1:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for name, bdf in brand_dfs.items():
                bdf.to_excel(writer, sheet_name=name[:31], index=False)
        st.download_button("📥 合併版 .xlsx", data=buf.getvalue(),
                           file_name=f"叫貨單_{selected_clinic}_{date.today()}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, type="primary")
    with col2:
        all_items = pd.concat([bdf.assign(廠牌=n) for n, bdf in brand_dfs.items()], ignore_index=True)
        st.download_button("📥 CSV", data=all_items.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"叫貨單_{selected_clinic}_{date.today()}.csv",
                           mime="text/csv", use_container_width=True)


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

    # ── 參數 ──
    with tab_params:
        settings = sb.table("system_settings").select("*").execute().data
        with st.form("settings_form"):
            new_values = {}
            for s in settings:
                new_values[s["key"]] = st.number_input(s["description"], value=float(s["value"]),
                                                        step=0.1, format="%.1f", key=f"s_{s['key']}")
            if st.form_submit_button("儲存", type="primary"):
                for k, v in new_values.items():
                    sb.table("system_settings").update({"value": str(v)}).eq("key", k).execute()
                st.success("已更新")

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
            with st.expander(f"👤 {u['display_name'] or u['username']}（{u['role']}）"):
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
