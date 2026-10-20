"""
資料庫模組 — Supabase PostgreSQL 連線與 CRUD 操作
所有 SQL 操作封裝在此，主程式不直接寫 SQL。
"""

import httpx
from supabase import create_client, Client
import streamlit as st

# 逾時/連線中斷類錯誤（可安全重試的那種）
NETWORK_ERRORS = (httpx.TimeoutException, httpx.TransportError)


def _harden_http(sb: Client) -> None:
    """把 postgrest 的 httpx session 換成：HTTP/1.1、逾時 10 秒、keep-alive 20 秒。

    Why: 套件預設 HTTP/2 + 逾時 120 秒。HTTP/2 所有查詢共用一條連線，一旦該連線卡住
         （實測曾卡 56 秒、也出現過 ReadTimeout），整頁所有查詢一起等。改成短逾時後，
         卡住最多 10 秒就會由 with_retry() 換新連線重試，而不是等 2 分鐘。"""
    old = sb.postgrest.session
    sb.postgrest.session = httpx.Client(
        base_url=old.base_url,
        headers=dict(old.headers),
        http2=False,
        timeout=httpx.Timeout(10.0, connect=8.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=20.0),
        follow_redirects=True,
    )
    try:
        old.close()
    except Exception:
        pass


@st.cache_resource(show_spinner=False)
def get_supabase_client() -> Client:
    """取得 Supabase 連線（使用 secret_key 以繞過 RLS）。

    Why: 以 cache_resource 讓整個伺服器共用同一個 client（底層 httpx 連線池），
         否則每次 rerun 都重新建立 client、重做 TLS 交握，是頁面每次操作都變慢的主因之一。"""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["secret_key"]
    sb = create_client(url, key)
    _harden_http(sb)
    return sb


def with_retry(fn, attempts: int = 3):
    """執行 fn()；遇到逾時/連線中斷時，丟棄快取的 client（換新連線）重試，最多 attempts 次。
    fn 內部必須自行呼叫 get_supabase_client()，重試時才會拿到新連線。"""
    for i in range(attempts):
        try:
            return fn()
        except NETWORK_ERRORS:
            if i == attempts - 1:
                raise
            get_supabase_client.clear()


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
