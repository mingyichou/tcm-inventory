-- ══════════════════════════════════════════════
--  Migration v4: 各分類獨立參數
--  目標庫存備貨倍數 / 異常耗用警示閾值 / 建議叫貨安全係數
--  請在 Supabase SQL Editor 中執行此腳本
-- ══════════════════════════════════════════════

-- 1. 在 categories 加入三個參數欄位（沿用原全域預設值）
ALTER TABLE categories ADD COLUMN IF NOT EXISTS stock_target_multiplier NUMERIC NOT NULL DEFAULT 2.0;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS anomaly_threshold       NUMERIC NOT NULL DEFAULT 1.5;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS safety_factor           NUMERIC NOT NULL DEFAULT 1.2;

-- 2. 用原本 system_settings 的全域值回填現有分類（若存在），讓行為與升級前一致
UPDATE categories c SET
  stock_target_multiplier = COALESCE(
    (SELECT value::NUMERIC FROM system_settings WHERE key = 'stock_target_multiplier'), 2.0),
  anomaly_threshold = COALESCE(
    (SELECT value::NUMERIC FROM system_settings WHERE key = 'anomaly_threshold'), 1.5),
  safety_factor = COALESCE(
    (SELECT value::NUMERIC FROM system_settings WHERE key = 'safety_factor'), 1.2);

-- system_settings 表保留不動（不再被程式讀取，留作歷史備查）
