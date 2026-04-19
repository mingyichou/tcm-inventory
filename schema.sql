-- ══════════════════════════════════════════════
--  澤豐中醫聯盟 盤點管理系統 — 資料庫建置
--  請在 Supabase SQL Editor 中執行此腳本
-- ══════════════════════════════════════════════

-- 1. 單位表
CREATE TABLE IF NOT EXISTS units (
    id SERIAL PRIMARY KEY,
    name VARCHAR(20) NOT NULL UNIQUE
);

-- 2. 廠牌表
CREATE TABLE IF NOT EXISTS brands (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE
);

-- 3. 分類表
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    default_unit_id INTEGER REFERENCES units(id),
    default_spec_note VARCHAR(100)
);

-- 4. 診所表
CREATE TABLE IF NOT EXISTS clinics (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE
);

-- 5. 使用者表
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    clinic_id INTEGER REFERENCES clinics(id),
    role VARCHAR(20) NOT NULL DEFAULT 'staff' CHECK (role IN ('admin', 'manager', 'staff')),
    display_name VARCHAR(50)
);

-- 6. 品項主檔（兩院共用）
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    unit_id INTEGER NOT NULL REFERENCES units(id),
    brand1_id INTEGER REFERENCES brands(id),
    brand2_id INTEGER REFERENCES brands(id),
    spec_note VARCHAR(200),
    is_active BOOLEAN DEFAULT TRUE,
    UNIQUE(name, category_id)
);

-- 7. 各診所庫存表
CREATE TABLE IF NOT EXISTS clinic_stock (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    clinic_id INTEGER NOT NULL REFERENCES clinics(id),
    current_stock NUMERIC(10,2) NOT NULL DEFAULT 0,
    UNIQUE(product_id, clinic_id)
);

-- 8. 異動紀錄表
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    clinic_id INTEGER NOT NULL REFERENCES clinics(id),
    change_qty NUMERIC(10,2) NOT NULL,
    tx_date DATE NOT NULL DEFAULT CURRENT_DATE,
    tx_type VARCHAR(20) NOT NULL CHECK (tx_type IN ('進貨', '調撥', '廢棄')),
    note VARCHAR(500),
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);

-- 9. 盤點批次表
CREATE TABLE IF NOT EXISTS inventory_sessions (
    id SERIAL PRIMARY KEY,
    clinic_id INTEGER NOT NULL REFERENCES clinics(id),
    session_date DATE NOT NULL DEFAULT CURRENT_DATE,
    operator_id INTEGER NOT NULL REFERENCES users(id),
    status VARCHAR(20) NOT NULL DEFAULT '進行中' CHECK (status IN ('進行中', '已完成')),
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

-- 10. 盤點明細表
CREATE TABLE IF NOT EXISTS inventory_logs (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES inventory_sessions(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    clinic_id INTEGER NOT NULL REFERENCES clinics(id),
    last_count_qty NUMERIC(10,2),
    restock_qty_since_last NUMERIC(10,2),
    current_count_qty NUMERIC(10,2),
    consumed_qty NUMERIC(10,2),
    log_date DATE NOT NULL DEFAULT CURRENT_DATE
);

-- 11. 系統設定表
CREATE TABLE IF NOT EXISTS system_settings (
    key VARCHAR(100) PRIMARY KEY,
    value VARCHAR(200) NOT NULL,
    description VARCHAR(500)
);

-- ══════════════════════════════════════════════
--  初始資料
-- ══════════════════════════════════════════════

-- 單位
INSERT INTO units (name) VALUES ('罐'), ('帖'), ('錢'), ('克'), ('兩'), ('錠'), ('包'), ('盒')
ON CONFLICT (name) DO NOTHING;

-- 廠牌
INSERT INTO brands (name) VALUES ('港香蘭'), ('天一'), ('莊松榮'), ('科達'), ('順天堂'), ('仙豐')
ON CONFLICT (name) DO NOTHING;

-- 診所
INSERT INTO clinics (name) VALUES ('澤豐'), ('澤沛')
ON CONFLICT (name) DO NOTHING;

-- 分類（帶預設單位）
INSERT INTO categories (name, default_unit_id, default_spec_note) VALUES
    ('科學中藥複方', (SELECT id FROM units WHERE name='罐'), '200g'),
    ('科學中藥單方', (SELECT id FROM units WHERE name='罐'), '200g'),
    ('自費科中複方', (SELECT id FROM units WHERE name='罐'), '200g'),
    ('自費科中單方', (SELECT id FROM units WHERE name='罐'), '200g'),
    ('水藥材', (SELECT id FROM units WHERE name='錢'), NULL)
ON CONFLICT (name) DO NOTHING;

-- 系統設定
INSERT INTO system_settings (key, value, description) VALUES
    ('anomaly_threshold', '1.5', '異常耗用警示閾值（倍數）'),
    ('safety_factor', '1.2', '建議叫貨安全係數'),
    ('stock_target_multiplier', '2.0', '目標庫存備貨倍數')
ON CONFLICT (key) DO NOTHING;

-- 管理者帳號（密碼: admin123，上線前請修改）
INSERT INTO users (username, password_hash, clinic_id, role, display_name) VALUES
    ('admin', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9', NULL, 'admin', '院長')
ON CONFLICT (username) DO NOTHING;
