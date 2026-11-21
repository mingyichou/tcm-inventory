-- migration_v10：品項進貨價格與成本表
--  1. product_prices：各品項 × 廠牌的進價（全院共用），可存文字備註（如「時價」「無」）
--  2. price_table_brands：進價/成本表要顯示哪些廠牌欄位（分單方/複方）、各欄位的成本倍數（成本＝進價×倍數）、顯示順序
--     formula 存使用者填的算式（如 5/6*0.65），multiplier 為算出的數值（程式計算用）
--     之後要把現有廠牌加入表格，直接在 系統設定 → 💰 進價表參數 新增一列即可，不必改程式
--  3. clinic_stock 加 brand3_id（第三廠牌，叫貨順序表的「廠3」）
--  4. system_settings 加 高價標示界線：成本 ≥ 界線 → 橘色粗體（單方 280、複方 450）
-- 在 Supabase SQL Editor 執行：

CREATE TABLE IF NOT EXISTS product_prices (
    id          SERIAL PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    brand_id    INTEGER NOT NULL REFERENCES brands(id),
    price       NUMERIC(10,2),
    note        VARCHAR(50),
    updated_at  TIMESTAMP DEFAULT NOW(),
    updated_by  INTEGER REFERENCES users(id),
    UNIQUE(product_id, brand_id)
);

CREATE TABLE IF NOT EXISTS price_table_brands (
    id          SERIAL PRIMARY KEY,
    kind        VARCHAR(10) NOT NULL CHECK (kind IN ('單方', '複方')),
    brand_id    INTEGER NOT NULL REFERENCES brands(id),
    multiplier  NUMERIC(12,8) NOT NULL DEFAULT 0.7,
    formula     VARCHAR(50),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, brand_id)
);

ALTER TABLE clinic_stock ADD COLUMN IF NOT EXISTS brand3_id INTEGER REFERENCES brands(id);

-- 初始廠牌欄位與倍數（院長提供的計算式；K 裝為 5 入，÷5 換算成每罐成本）
INSERT INTO price_table_brands (kind, brand_id, multiplier, formula, sort_order) VALUES
    ('複方', (SELECT id FROM brands WHERE name='天一'),   5.0/6*0.65,   '5/6*0.65',   1),
    ('複方', (SELECT id FROM brands WHERE name='港香蘭'), 10.0/11*0.65, '10/11*0.65', 2),
    ('複方', (SELECT id FROM brands WHERE name='港K裝'),  0.65/5,       '0.65/5',     3),
    ('複方', (SELECT id FROM brands WHERE name='莊松榮'), 10.0/11*0.7,  '10/11*0.7',  4),
    ('複方', (SELECT id FROM brands WHERE name='科達'),   0.7,          '0.7',        5),
    ('複方', (SELECT id FROM brands WHERE name='順天堂'), 0.7,          '0.7',        6),
    ('複方', (SELECT id FROM brands WHERE name='順K裝'),  0.7/5,        '0.7/5',      7),
    ('單方', (SELECT id FROM brands WHERE name='天一'),   0.65,         '0.65',       1),
    ('單方', (SELECT id FROM brands WHERE name='港香蘭'), 0.65,         '0.65',       2),
    ('單方', (SELECT id FROM brands WHERE name='莊松榮'), 0.7,          '0.7',        3),
    ('單方', (SELECT id FROM brands WHERE name='科達'),   0.7,          '0.7',        4),
    ('單方', (SELECT id FROM brands WHERE name='順天堂'), 0.7,          '0.7',        5)
ON CONFLICT (kind, brand_id) DO NOTHING;

INSERT INTO system_settings (key, value, description) VALUES
    ('high_price_single',   '280', '單方高價標示界線（成本 ≥ 此值橘色粗體）'),
    ('high_price_compound', '450', '複方高價標示界線（成本 ≥ 此值橘色粗體）')
ON CONFLICT (key) DO NOTHING;

-- 驗證：
-- SELECT * FROM price_table_brands ORDER BY kind, sort_order;
-- SELECT * FROM system_settings WHERE key LIKE 'high_price_%';
-- SELECT column_name FROM information_schema.columns WHERE table_name='clinic_stock';
