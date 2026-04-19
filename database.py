"""
資料庫模組 — Supabase PostgreSQL 連線與 CRUD 操作
所有 SQL 操作封裝在此，主程式不直接寫 SQL。
"""

from supabase import create_client, Client
import streamlit as st


def get_supabase_client() -> Client:
    """取得 Supabase 連線（使用 secret_key 以繞過 RLS）。"""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["secret_key"]
    return create_client(url, key)


def get_client() -> Client:
    """取得 Supabase 連線（使用 publishable key，受 RLS 控制）。"""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)
