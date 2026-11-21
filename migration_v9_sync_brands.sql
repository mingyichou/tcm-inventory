-- migration_v9：診所間廠牌同步開關
--  在「品項管理」修改任一診所的第一/第二廠牌時，是否同步套用到其他診所的同一品項（雙向）。
--  1 = 開啟（目前澤豐/澤沛廠牌一致，暫時同步維護）；0 = 關閉（日後兩診所廠牌要分開管理時）。
--  程式缺值時預設視為開啟；此開關可直接在系統設定 → 參數 頁切換，不必改程式。
--  品項名稱/分類/單位/規格存在共用的 products 表，本來就兩診所一致；櫃位與啟用狀態永遠各診所獨立。
-- 在 Supabase SQL Editor 執行（程式啟動也會在切換開關時自動 upsert，這裡只是把初始值寫清楚）：

INSERT INTO system_settings (key, value, description)
VALUES ('sync_brands_across_clinics', '1', '診所間同步第一/第二廠牌（1=開啟，0=各診所獨立）')
ON CONFLICT (key) DO NOTHING;

-- 驗證：
-- SELECT * FROM system_settings WHERE key = 'sync_brands_across_clinics';
