-- migration_v8：參數調整
--  1. 目標庫存備貨倍數 / 建議叫貨安全係數 可「依櫃位 A/B/C/D 分別設定」
--     （櫃位取第一個字母：A、A2、A70 都算 A；無櫃位或非 A–D 開頭比照 B；不改動櫃位欄位本身）
--     預設四個科中分類開啟依櫃位設定，水藥材 / 高貴藥材 / 非健保藥材維持不分櫃位。
--  2. 平均耗用計算次數、異常耗用警示閾值 改為全域單一值（存 system_settings），
--     不再逐分類設定；categories 的 avg_window / anomaly_threshold 欄位保留不動（程式不再讀取）。
-- 在 Supabase SQL Editor 執行：

-- 1. 依櫃位欄位（初始值複製目前的不分櫃位數值，行為與升級前一致）
ALTER TABLE categories ADD COLUMN IF NOT EXISTS by_cabinet   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS multiplier_a NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS multiplier_b NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS multiplier_c NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS multiplier_d NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS safety_a     NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS safety_b     NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS safety_c     NUMERIC;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS safety_d     NUMERIC;

UPDATE categories SET
    multiplier_a = COALESCE(multiplier_a, stock_target_multiplier),
    multiplier_b = COALESCE(multiplier_b, stock_target_multiplier),
    multiplier_c = COALESCE(multiplier_c, stock_target_multiplier),
    multiplier_d = COALESCE(multiplier_d, stock_target_multiplier),
    safety_a     = COALESCE(safety_a, safety_factor),
    safety_b     = COALESCE(safety_b, safety_factor),
    safety_c     = COALESCE(safety_c, safety_factor),
    safety_d     = COALESCE(safety_d, safety_factor);

UPDATE categories SET by_cabinet = TRUE
WHERE name IN ('科學中藥複方', '科學中藥單方', '自費科中複方', '自費科中單方');

-- 2. 全域參數（沿用目前各分類的設定值；目前全部為 N=6、閾值 2.0）
INSERT INTO system_settings (key, value, description)
VALUES ('avg_window',
        COALESCE((SELECT MAX(avg_window)::text FROM categories), '6'),
        '平均耗用計算次數（近 N 次盤點）')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, description = EXCLUDED.description;

INSERT INTO system_settings (key, value, description)
VALUES ('anomaly_threshold',
        COALESCE((SELECT MAX(anomaly_threshold)::text FROM categories), '1.5'),
        '異常耗用警示閾值（倍數）')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, description = EXCLUDED.description;

-- 驗證：
-- SELECT id, name, by_cabinet, stock_target_multiplier, multiplier_a, multiplier_b, multiplier_c, multiplier_d,
--        safety_factor, safety_a, safety_b, safety_c, safety_d FROM categories ORDER BY id;
-- SELECT * FROM system_settings WHERE key IN ('avg_window', 'anomaly_threshold');
