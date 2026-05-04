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


def fetch_all(query, page_size: int = 1000) -> list:
    """分頁讀取所有結果，繞過 Supabase 預設 1000 筆上限。

    用法：把 sb.table(...).select(...).eq(...).order(...) 串好（不要 .execute()）傳進來。
    Why: Supabase Python client 預設單次回傳上限 1000 筆。當 inventory_logs/transactions
         隨盤點次數累積超過 1000 筆時，預設查詢會靜默截斷，導致庫存頁顯示不完整、耗用計算錯誤。
    """
    rows = []
    offset = 0
    while True:
        batch = query.range(offset, offset + page_size - 1).execute().data
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows
