"""
登入驗證模組
"""

import hashlib
import streamlit as st
from database import get_supabase_client


def hash_password(password: str) -> str:
    """SHA-256 雜湊密碼。"""
    return hashlib.sha256(password.encode()).hexdigest()


def check_login(username: str, password: str) -> dict | None:
    """驗證帳號密碼，回傳使用者資訊或 None。"""
    try:
        sb = get_supabase_client()
        pw_hash = hash_password(password)

        resp = sb.table("users").select(
            "id, username, password_hash, clinic_id, role, display_name"
        ).eq("username", username).execute()

        if not resp.data:
            return None

        user = resp.data[0]
        if user["password_hash"] != pw_hash:
            return None

        # 取得診所名稱
        clinic_name = None
        if user["clinic_id"]:
            clinic_resp = sb.table("clinics").select("name").eq("id", user["clinic_id"]).execute()
            if clinic_resp.data:
                clinic_name = clinic_resp.data[0]["name"]

        return {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "display_name": user["display_name"] or user["username"],
            "clinic_id": user["clinic_id"],
            "clinic_name": clinic_name,
        }

    except Exception as e:
        st.error(f"登入驗證錯誤：{e}")
        return None


def show_login_page():
    """顯示登入頁面。"""
    col1, col2, col3 = st.columns([1, 1.5, 1])

    with col2:
        st.markdown("")
        st.markdown("")
        st.markdown(
            "<h1 style='text-align:center; color:#6A5ACD;'>💊 澤豐中醫聯盟</h1>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<h3 style='text-align:center; color:#888;'>盤點管理系統</h3>",
            unsafe_allow_html=True,
        )
        st.markdown("")

        with st.form("login_form"):
            username = st.text_input("帳號", placeholder="請輸入帳號")
            password = st.text_input("密碼", type="password", placeholder="請輸入密碼")
            submitted = st.form_submit_button("登入", use_container_width=True, type="primary")

            if submitted:
                if not username or not password:
                    st.warning("請輸入帳號和密碼")
                else:
                    user = check_login(username, password)
                    if user:
                        st.session_state.logged_in = True
                        st.session_state.user = user
                        st.rerun()
                    else:
                        st.error("帳號或密碼錯誤")
