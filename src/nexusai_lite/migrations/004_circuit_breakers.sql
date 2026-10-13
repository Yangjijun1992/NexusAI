-- 004_circuit_breakers.sql — 简化熔断持久化（S2-05）
-- 以 dimension 为隔离维度（例如 "provider:deepseek" / "model:deepseek-chat"）保存
-- CLOSED/OPEN/HALF_OPEN 状态与计数器，使进程重启后不把 OPEN 静默重置为 CLOSED
-- （reference F5、§4.4、§12）。时间戳以 UTC ISO-8601 TEXT 存储。
CREATE TABLE IF NOT EXISTS circuit_breakers (
  dimension TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  failure_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  in_flight_probes INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT,
  last_change_at TEXT NOT NULL
);
