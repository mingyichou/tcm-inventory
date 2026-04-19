"""
澤豐中醫聯盟 — 科學中藥盤點管理系統
主程式入口
"""

import streamlit as st

st.set_page_config(
    page_title="澤豐中醫聯盟 盤點系統",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 全域 CSS
st.markdown("""
<style>
    .stDataFrame tbody tr:nth-child(10n) {
        border-bottom: 3px solid #6A5ACD !important;
    }
    .sidebar-title {
        font-size: 1.3rem;
        font-weight: 700;
        color: #6A5ACD;
        margin-bottom: 0.5rem;
    }
</style>
""", unsafe_allow_html=True)

from auth import check_login, show_login_page
from pages_app import (
    page_stock_overview, page_transactions, page_inventory,
    page_items, page_analytics, page_order, page_settings,
)


def main():
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.user = None

    if not st.session_state.logged_in:
        show_login_page()
        return

    user = st.session_state.user
    role = user["role"]

    with st.sidebar:
        st.markdown('<p class="sidebar-title">💊 澤豐中醫聯盟</p>', unsafe_allow_html=True)
        st.caption(f"👤 {user['display_name']}（{role}）")

        if role in ("admin", "manager"):
            clinic_options = ["澤豐", "澤沛", "合併檢視"]
            selected_clinic = st.selectbox("診所", clinic_options, key="clinic_select")
        else:
            selected_clinic = user["clinic_name"]
            st.info(f"📍 {selected_clinic}")

        st.session_state.selected_clinic = selected_clinic
        st.divider()

        # 選單順序：庫存 → 進退貨登錄 → 執行盤點 → 品項管理 → 數據分析 → 叫貨出表 → 系統設定 → 登出
        menu_items = [
            "📦 庫存",
            "📥 進退貨登錄",
            "📝 執行盤點",
            "📋 品項管理",
            "📊 數據分析",
            "🛒 叫貨出表",
        ]

        if role == "admin":
            menu_items.append("⚙️ 系統設定")

        menu_items.append("🚪 登出")

        choice = st.radio("功能", menu_items, label_visibility="collapsed")

        if choice == "🚪 登出":
            st.session_state.logged_in = False
            st.session_state.user = None
            st.rerun()

    # 頁面路由
    if choice == "📦 庫存":
        page_stock_overview()
    elif choice == "📥 進退貨登錄":
        page_transactions()
    elif choice == "📝 執行盤點":
        page_inventory()
    elif choice == "📋 品項管理":
        page_items()
    elif choice == "📊 數據分析":
        page_analytics()
    elif choice == "🛒 叫貨出表":
        page_order()
    elif choice == "⚙️ 系統設定":
        page_settings()


if __name__ == "__main__":
    main()
