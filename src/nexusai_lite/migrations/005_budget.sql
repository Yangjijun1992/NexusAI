-- 005_budget.sql — 预算硬上限（S2-07）
-- 预算账户（budget_accounts）与预算预留（budget_reservations）：
-- reservation 在 BEGIN IMMEDIATE 事务中原子检查用量，防止并发竞争突破硬上限
-- （reference §12 规则 9 / §8 阶段 2 退出条件）。

CREATE TABLE IF NOT EXISTS budget_accounts (
  account_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  currency TEXT,
  soft_limit REAL,
  hard_limit REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
  reservation_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
  scope TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  committed_at TEXT,
  actual_amount REAL
);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_account_status
  ON budget_reservations(account_id, status);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_scope
  ON budget_reservations(scope);
