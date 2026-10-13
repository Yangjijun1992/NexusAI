-- 006_alerts.sql — 基础告警/通知持久化（S2-08）
-- 告警行记录严重级别、触发条件(code)、影响范围(scope)、关联事件(event_id)与
-- 去重键(dedup_key)。载荷中的 title/message/detail_json 在写入前已脱敏
-- （reference §4.3、§12 规则 6），绝不包含密钥、完整 Prompt 或敏感代码。
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_id TEXT NOT NULL UNIQUE,
  severity TEXT NOT NULL,
  code TEXT NOT NULL,
  scope TEXT,
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  event_id TEXT,
  occurred_at TEXT NOT NULL,
  status TEXT NOT NULL,
  dedup_key TEXT NOT NULL,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_code_scope_at ON alerts(code, scope, occurred_at);
