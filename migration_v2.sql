-- ══════════════════════════════════════════════
--  Migration v2: 每診所獨立廠牌與啟用狀態
--  請在 Supabase SQL Editor 中執行此腳本
-- ══════════════════════════════════════════════

-- 1. 在 clinic_stock 加入 per-clinic 欄位
ALTER TABLE clinic_stock ADD COLUMN IF NOT EXISTS brand1_id INTEGER REFERENCES brands(id);
ALTER TABLE clinic_stock ADD COLUMN IF NOT EXISTS brand2_id INTEGER REFERENCES brands(id);
ALTER TABLE clinic_stock ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

-- 2. 從 products 表複製現有廠牌/啟用狀態作為初始值
UPDATE clinic_stock cs SET
  brand1_id = p.brand1_id,
  brand2_id = p.brand2_id,
  is_active = COALESCE(p.is_active, TRUE)
FROM products p
WHERE cs.product_id = p.id
AND cs.brand1_id IS NULL;

-- 3. 確保所有品項在兩間診所都有 clinic_stock 紀錄
INSERT INTO clinic_stock (product_id, clinic_id, current_stock, brand1_id, brand2_id, is_active)
SELECT p.id, c.id, 0, p.brand1_id, p.brand2_id, COALESCE(p.is_active, TRUE)
FROM products p CROSS JOIN clinics c
WHERE NOT EXISTS (
    SELECT 1 FROM clinic_stock cs WHERE cs.product_id = p.id AND cs.clinic_id = c.id
);

-- 4. 清理空的盤點批次（有 session 但沒有 logs 的記錄）
DELETE FROM inventory_sessions s
WHERE NOT EXISTS (
    SELECT 1 FROM inventory_logs l WHERE l.session_id = s.id
);
