-- ══════════════════════════════════════════════
--  Migration v3: 拍照盤點辨識報告
--  請在 Supabase SQL Editor 中執行此腳本
-- ══════════════════════════════════════════════

-- 拍照盤點辨識報告表
CREATE TABLE IF NOT EXISTS photo_recognition_reports (
    id SERIAL PRIMARY KEY,
    clinic_id INTEGER NOT NULL REFERENCES clinics(id),
    operator_id INTEGER REFERENCES users(id),
    recognized_at TIMESTAMP NOT NULL DEFAULT NOW(),
    session_date DATE,
    photo_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    unmatched_count INTEGER NOT NULL DEFAULT 0,
    unmatched_items JSONB,
    ai_model VARCHAR(50),
    double_check BOOLEAN DEFAULT FALSE,
    saved BOOLEAN DEFAULT FALSE,
    saved_at TIMESTAMP,
    saved_inserted INTEGER,
    saved_updated INTEGER
);

CREATE INDEX IF NOT EXISTS idx_photo_reports_clinic_time
    ON photo_recognition_reports (clinic_id, recognized_at DESC);
