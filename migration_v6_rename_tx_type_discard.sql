-- migration_v6：異動類型「廢棄」改名為「退貨/廢棄」
-- 影響：transactions.tx_type 既有 30 筆「廢棄」需更名，且欄位有 CHECK 限制需同步放寬。
-- 必須一次跑完（drop → update → add），順序不可顛倒，否則 UPDATE 會被舊 CHECK 擋下。
-- 在 Supabase SQL Editor 執行：

-- 1. 移除舊的 tx_type CHECK 限制（自動找出名稱，避免硬編 constraint 名）
DO $$
DECLARE c text;
BEGIN
  SELECT conname INTO c
  FROM pg_constraint
  WHERE conrelid = 'transactions'::regclass
    AND contype = 'c'
    AND pg_get_constraintdef(oid) LIKE '%tx_type%';
  IF c IS NOT NULL THEN
    EXECUTE format('ALTER TABLE transactions DROP CONSTRAINT %I', c);
  END IF;
END $$;

-- 2. 既有資料更名
UPDATE transactions SET tx_type = '退貨/廢棄' WHERE tx_type = '廢棄';

-- 3. 重新建立 CHECK 限制（含新值）
ALTER TABLE transactions
  ADD CONSTRAINT transactions_tx_type_check
  CHECK (tx_type IN ('進貨', '調撥', '退貨/廢棄'));

-- 驗證：應只剩 進貨 / 調撥 / 退貨/廢棄
-- SELECT tx_type, COUNT(*) FROM transactions GROUP BY tx_type;
