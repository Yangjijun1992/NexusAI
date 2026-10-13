-- 001_initial.sql — NexusAI Lite 初始 schema
-- 所有时间戳以 UTC ISO-8601 TEXT 存储；ID 为 UUID4 字符串（除非标注 INTEGER AUTOINCREMENT）。

-- 迁移追踪表（IF NOT EXISTS：migrate() 框架也会幂等创建本表）
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- 逻辑任务与当前状态
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    task_type TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL,
    attempt_no INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    deadline_at TEXT,
    estimated_cost REAL,
    actual_cost REAL,
    cost_status TEXT,
    error_code TEXT,
    error_category TEXT,
    safe_error_message TEXT,
    raw_error_reference TEXT,
    policy_version TEXT,
    trace_id TEXT
);

-- 单次执行记录
CREATE TABLE task_runs (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    error_code TEXT,
    safe_error_message TEXT,
    trace_id TEXT
);

-- 单次尝试记录
CREATE TABLE task_attempts (
    attempt_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES task_runs(execution_id),
    task_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_category TEXT,
    retry_reason TEXT,
    may_be_billed INTEGER NOT NULL DEFAULT 0,
    cost_estimate REAL,
    request_id TEXT,
    trace_id TEXT
);

-- 自身 API 请求日志（去重键 request_id）
CREATE TABLE api_request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    task_id TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status_code INTEGER,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL,
    cost_status TEXT,
    error_code TEXT,
    error_category TEXT,
    safe_error_message TEXT,
    raw_error_reference TEXT,
    streaming INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    extra_json TEXT
);
CREATE INDEX idx_api_request_logs_started_at ON api_request_logs(started_at);
CREATE INDEX idx_api_request_logs_provider_model ON api_request_logs(provider, model);

-- 成本记录
CREATE TABLE cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    attempt_id TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    pricing_version TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    actual_cost REAL,
    cost_status TEXT NOT NULL,
    basis TEXT,
    effective_at TEXT,
    source TEXT,
    created_at TEXT NOT NULL
);
-- 去重键：同一 request_id 下不同 attempt 去重；NULL attempt 归并
CREATE UNIQUE INDEX idx_cost_records_request_attempt
    ON cost_records(request_id, COALESCE(attempt_id, ''));

-- 官方信息采集快照（类别 A：官方公开信息，与自身观测分开存储）
CREATE TABLE official_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    parse_status TEXT,
    raw_content TEXT,
    raw_sha256 TEXT,
    parsed_json TEXT,
    error_code TEXT,
    error_message TEXT
);
CREATE INDEX idx_official_snapshots_type_fetched ON official_snapshots(snapshot_type, fetched_at);

-- 官方时段规则
CREATE TABLE window_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    rule_json TEXT NOT NULL,
    source TEXT NOT NULL,
    source_snapshot_id INTEGER REFERENCES official_snapshots(id),
    effective_at TEXT,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);

-- 调度决策记录
CREATE TABLE schedule_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    task_id TEXT,
    decision TEXT NOT NULL,
    reason_codes TEXT,
    metrics_summary TEXT,
    sample_count INTEGER,
    data_quality TEXT,
    policy_version TEXT,
    decided_at TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    manual_override INTEGER NOT NULL DEFAULT 0,
    override_reason TEXT
);

-- 版本化配置
CREATE TABLE config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    diff_json TEXT,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    effective_at TEXT,
    change_reason TEXT,
    previous_version INTEGER,
    rollback_supported INTEGER NOT NULL DEFAULT 1,
    UNIQUE(config_id, version)
);

-- 审计事件
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject TEXT,
    object_type TEXT,
    object_id TEXT,
    at TEXT NOT NULL,
    detail_json TEXT
);
