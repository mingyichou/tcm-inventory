-- migration_v7：各分類新增「平均耗用計算次數」參數（近 N 次盤點的耗用平均）
-- 預設 6，與升級前行為一致；可在「系統設定 → 參數」逐分類調整。
-- 程式在此欄位尚未建立時會自動以 6 計算，但無法儲存設定，執行後即可儲存。
-- 在 Supabase SQL Editor 執行：

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS avg_window INTEGER NOT NULL DEFAULT 6
    CHECK (avg_window BETWEEN 1 AND 24);

-- 驗證：
-- SELECT id, name, avg_window FROM categories ORDER BY id;
