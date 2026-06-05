-- migration_v5：inventory_logs 加上 (session_id, product_id) 唯一限制
-- 目的：從資料庫層杜絕「同一盤點 session、同一品項出現多筆 log」的重複資料。
--       這是 upsert(on_conflict="session_id,product_id") 能生效的前提，也是最後一道防線。
--
-- 重要：執行前必須先清除既有重複資料，否則 ADD CONSTRAINT 會失敗。
--       重複清理已由 _cleanup_dups.py 完成（保留每組 id 最大的那筆＝最後修正值）。
--       若仍有殘留，可先用下方查詢確認：
--
--   SELECT session_id, product_id, COUNT(*)
--   FROM inventory_logs
--   GROUP BY session_id, product_id
--   HAVING COUNT(*) > 1;
--
-- 在 Supabase SQL Editor 執行：

ALTER TABLE inventory_logs
    ADD CONSTRAINT uq_inventory_logs_session_product
    UNIQUE (session_id, product_id);
