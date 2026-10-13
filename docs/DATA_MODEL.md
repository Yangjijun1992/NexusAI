# NexusAI Lite 数据模型

> 以 `src/nexusai_lite/migrations/001..008` 为准。所有时间戳统一以 **UTC ISO-8601 TEXT** 存储（`db.utcnow_iso()` 返回带 `+00:00` 的字符串）；除标注 `INTEGER AUTOINCREMENT` 外，主键为 UUID4 十六进制字符串。迁移由 `db.migrate` 按文件名顺序、在 `BEGIN IMMEDIATE` 事务内幂等应用，已应用版本记于 `schema_migrations`。

- 迁移文件：`001_initial.sql`、`002_task_limits.sql`、`003_window_rules_unique_version.sql`、`004_circuit_breakers.sql`、`005_budget.sql`、`006_alerts.sql`、`007_indexes.sql`、`008_optimization.sql`。
- 相关文档：[`ARCHITECTURE.md`](ARCHITECTURE.md)、[`EVENTS.md`](EVENTS.md)。

## 001_initial.sql — 核心表

### schema_migrations
迁移追踪表（`db.migrate` 幂等创建）。
| 列 | 类型 | 说明 |
|---|---|---|
| version | TEXT PK | 迁移版本（文件名 stem） |
| applied_at | TEXT NOT NULL | 应用时间（UTC） |

### tasks
逻辑任务与当前状态（§3、F6、S2-01）。`002` 补充限制字段。
| 列 | 类型 | 说明 |
|---|---|---|
| task_id | TEXT PK | 任务唯一 ID |
| idempotency_key | TEXT UNIQUE | 幂等键；重复提交返回既有任务 |
| task_type | TEXT | 任务类型（如 `llm`） |
| priority | TEXT DEFAULT 'normal' | 优先级 |
| status | TEXT NOT NULL | 状态机状态（见 STATE_MACHINE） |
| attempt_no | INTEGER DEFAULT 0 | 当前尝试次数 |
| created_at / started_at / finished_at / deadline_at | TEXT | 生命周期时间戳 |
| estimated_cost / actual_cost | REAL | 估算 / 实际成本 |
| cost_status | TEXT | `estimated/actual/unknown` 等 |
| error_code / error_category / safe_error_message / raw_error_reference | TEXT | 脱敏错误字段（§4.3） |
| policy_version | TEXT | 决策所用配置版本 |
| trace_id | TEXT | 链路 ID |

`002` 增加：`max_attempts INTEGER NOT NULL DEFAULT 3`、`max_execution_seconds INTEGER NOT NULL DEFAULT 3600`、`max_cost REAL`（NULL=未设上限）。

### task_runs
单次执行记录。
| 列 | 类型 | 说明 |
|---|---|---|
| execution_id | TEXT PK | 执行 ID |
| task_id | TEXT NOT NULL REFERENCES tasks | 关联任务 |
| started_at / finished_at | TEXT | 执行起止 |
| status | TEXT | 执行状态 |
| error_code / safe_error_message / trace_id | TEXT | 错误与链路 |

### task_attempts
单次尝试记录。
| 列 | 类型 | 说明 |
|---|---|---|
| attempt_id | TEXT PK | 尝试 ID |
| execution_id | TEXT NOT NULL REFERENCES task_runs | 所属执行 |
| task_id | TEXT NOT NULL | 任务 |
| attempt_no | INTEGER NOT NULL | 尝试序号 |
| started_at / finished_at | TEXT | 起止 |
| error_category | TEXT | 错误分类 |
| retry_reason | TEXT | 重试原因（已脱敏） |
| may_be_billed | INTEGER DEFAULT 0 | 是否可能计费 |
| cost_estimate | REAL | 成本估算 |
| request_id / trace_id | TEXT | 关联请求与链路 |

### api_request_logs
自身 API 请求日志（去重键 `request_id`）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| request_id | TEXT NOT NULL UNIQUE | 去重键 |
| task_id | TEXT | 关联任务 |
| provider / model | TEXT | 供应商与模型 |
| started_at / ended_at | TEXT | 起止（UTC） |
| status_code | INTEGER | HTTP 状态码 |
| latency_ms | INTEGER | 延迟（毫秒） |
| input_tokens / output_tokens | INTEGER | Token 用量 |
| retry_count | INTEGER DEFAULT 0 | 重试次数 |
| estimated_cost / cost_status | REAL / TEXT | 成本 |
| error_code / error_category / safe_error_message / raw_error_reference | TEXT | 脱敏错误 |
| streaming | INTEGER DEFAULT 0 | 是否流式 |
| trace_id / extra_json | TEXT | 链路与扩展 |
索引：`idx_api_request_logs_started_at`、`idx_api_request_logs_provider_model`。

### cost_records
成本记录。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| request_id | TEXT NOT NULL | 请求 |
| attempt_id | TEXT | 尝试（可空） |
| provider / model / pricing_version | TEXT | 维度 |
| input_tokens / output_tokens | INTEGER | 用量 |
| estimated_cost / actual_cost | REAL | 成本 |
| cost_status | TEXT NOT NULL | `estimated/actual/reconciled/unknown` |
| basis / source | TEXT | 估算依据与来源 |
| effective_at / created_at | TEXT | 时间 |
唯一索引：`idx_cost_records_request_attempt ON cost_records(request_id, COALESCE(attempt_id,''))`（去重）。

### official_snapshots
官方公开信息采集快照（类别 A，与自身观测分开）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| snapshot_type | TEXT NOT NULL | `pricing` / `status` 等 |
| source_url | TEXT NOT NULL | 来源 URL |
| fetched_at | TEXT NOT NULL | 抓取时间 |
| status | TEXT NOT NULL | 抓取状态（`ok`/失败） |
| parse_status | TEXT | 解析状态 |
| raw_content | TEXT | 原始内容（>1MB 截断） |
| raw_sha256 | TEXT | 原始内容哈希 |
| parsed_json | TEXT | 解析结果 |
| error_code / error_message | TEXT | 失败信息（脱敏） |
索引：`idx_official_snapshots_type_fetched`。

### window_rules
官方时段规则（可版本化）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| version | INTEGER NOT NULL | 规则版本（`003` 上加唯一索引） |
| rule_json | TEXT NOT NULL | 规则内容（timezone/workdays/peak_windows/source） |
| source | TEXT NOT NULL | 数据来源（如 `official-pricing-page` / `manual`） |
| source_snapshot_id | INTEGER REFERENCES official_snapshots | 来源快照 |
| effective_at / created_at | TEXT | 时间 |
| active | INTEGER DEFAULT 0 | 是否当前活跃 |

### schedule_decisions
调度决策记录。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| decision_id | TEXT NOT NULL UNIQUE | 决策 ID |
| task_id | TEXT | 任务 |
| decision | TEXT NOT NULL | `allow/delay/pause/reject/review_required` |
| reason_codes | TEXT | JSON 原因代码数组 |
| metrics_summary | TEXT | JSON 小型度量 |
| sample_count | INTEGER | 样本数 |
| data_quality | TEXT | 数据质量（`ok`/`NO_RULE`/`INSUFFICIENT_SAMPLE`） |
| policy_version | TEXT | 规则版本 |
| decided_at | TEXT | 决策时间 |
| old_value / new_value | TEXT | 覆盖前后值 |
| manual_override | INTEGER DEFAULT 0 | 是否人工覆盖 |
| override_reason | TEXT | 覆盖原因 |

### config_versions
版本化配置（§7、S1-09）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| config_id | TEXT NOT NULL | 配置 ID（默认 `nexusai-lite`） |
| version | INTEGER NOT NULL | 版本号 |
| payload_json | TEXT NOT NULL | 配置载荷 |
| checksum | TEXT NOT NULL | canonical JSON 的 sha256 |
| status | TEXT NOT NULL | `proposed/active/superseded` |
| diff_json | TEXT | 相对上一版本的差异 |
| created_by / approved_by | TEXT | 提交/审批主体 |
| created_at / effective_at | TEXT | 时间 |
| change_reason | TEXT | 变更原因 |
| previous_version | INTEGER | 上一版本 |
| rollback_supported | INTEGER DEFAULT 1 | 是否支持回滚 |
唯一约束：`UNIQUE(config_id, version)`。索引：`idx_config_versions_status`（007）。

### audit_events
统一审计事件（见 [`EVENTS.md`](EVENTS.md)）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| event_type | TEXT NOT NULL | 事件类型 |
| subject | TEXT | 主体（人/系统/维度） |
| object_type | TEXT | 对象类型 |
| object_id | TEXT | 对象 ID |
| at | TEXT NOT NULL | 事件时间（UTC） |
| detail_json | TEXT | 结构化细节 |

## 004_circuit_breakers.sql — 熔断持久化

简化熔断按 dimension 隔离持久化，使重启后不把 OPEN 静默重置为 CLOSED。
| 列 | 类型 | 说明 |
|---|---|---|
| dimension | TEXT PK | 隔离维度（如 `provider:deepseek`、`model:deepseek-chat`） |
| state | TEXT NOT NULL | `closed/open/half_open` |
| failure_count / success_count | INTEGER DEFAULT 0 | 计数 |
| in_flight_probes | INTEGER DEFAULT 0 | 半开在途探测数 |
| opened_at / last_change_at | TEXT | 时间 |

## 005_budget.sql — 预算

### budget_accounts
预算账户。
| 列 | 类型 | 说明 |
|---|---|---|
| account_id | TEXT PK | 账户 ID |
| scope | TEXT NOT NULL | `platform` / `provider:<name>` / `model:<name>` / `project:<id>` / `task:<id>` |
| currency | TEXT | 币种 |
| soft_limit / hard_limit | REAL | 软/硬上限（NULL=未设） |
| created_at / updated_at | TEXT | 时间 |

### budget_reservations
预算预留（`reserve` 在 `BEGIN IMMEDIATE` 事务内原子校验并发，防突破硬上限）。
| 列 | 类型 | 说明 |
|---|---|---|
| reservation_id | TEXT PK | 预留 ID |
| account_id | TEXT NOT NULL REFERENCES budget_accounts | 账户 |
| scope | TEXT NOT NULL | 作用域 |
| amount | REAL NOT NULL | 预留金额 |
| status | TEXT NOT NULL | `active/committed/released/expired` |
| created_at / expires_at / committed_at | TEXT | 时间 |
| actual_amount | REAL | 实际金额 |
索引：`idx_budget_reservations_account_status`、`idx_budget_reservations_scope`。

## 006_alerts.sql — 告警

告警持久化（载荷先脱敏，绝不包含密钥/完整 Prompt/敏感代码）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| alert_id | TEXT NOT NULL UNIQUE | 告警 ID |
| severity | TEXT NOT NULL | `info/warning/critical` |
| code | TEXT NOT NULL | 触发条件代码（见 EVENTS） |
| scope | TEXT | 影响范围 |
| title / message | TEXT NOT NULL | 标题与详情（已脱敏/截断） |
| event_id | TEXT | 关联事件 ID |
| occurred_at | TEXT NOT NULL | 时间 |
| status | TEXT NOT NULL | `raised/dispatched/dispatch_failed` |
| dedup_key | TEXT NOT NULL | 去重键（`code|scope`） |
| detail_json | TEXT | 结构化细节 |
索引：`idx_alerts_code_scope_at`。

## 008_optimization.sql — 阶段 4（advisory-only）

### shadow_recommendations
影子模式建议记录：只记「调度器将如何选择」，**绝不改写 tasks / schedule_decisions**。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| recommendation_id | TEXT NOT NULL UNIQUE | 建议 ID |
| task_id | TEXT | 任务 |
| recommended_action | TEXT NOT NULL | `ALLOW/DELAY` 等 |
| reason_codes | TEXT | 原因代码 |
| predicted_metric | REAL | 预测指标 |
| model | TEXT | 来源模型 |
| created_at | TEXT NOT NULL | 时间 |
索引：`idx_shadow_recommendations_created_at`。

### forecast_runs
预测运行审计（可选，只读预测历史）。
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER AUTOINCREMENT PK | 自增 ID |
| run_id | TEXT NOT NULL UNIQUE | 运行 ID |
| model | TEXT NOT NULL | 模型名 |
| metric | TEXT NOT NULL | 预测指标 |
| horizon_days | INTEGER NOT NULL | 预测窗口（天） |
| train_start / train_end | TEXT | 训练数据范围 |
| prediction_count | INTEGER NOT NULL | 预测点数 |
| created_at | TEXT NOT NULL | 时间 |

## 007_indexes.sql — 补充索引

- `idx_tasks_status ON tasks(status)`
- `idx_task_runs_task_id ON task_runs(task_id)`
- `idx_task_attempts_task_id ON task_attempts(task_id)`
- `idx_cost_records_provider_model_status ON cost_records(provider, model, cost_status)`
- `idx_config_versions_status ON config_versions(status)`

## 003_window_rules_unique_version.sql

- `CREATE UNIQUE INDEX idx_window_rules_version ON window_rules(version)`：保证每个版本号唯一，供 upsert 以 `MAX(version)+1` 递增。

## 约定总结

- **UTC ISO-8601**：所有时间戳字段统一 UTC，展示时按本地时区换算（`analysis` / `nightdev` 用 `Asia/Shanghai` 等本地时区换算比较）。
- **脱敏**：错误只存 `error_code` / `error_category` / `safe_error_message` / 受控 `raw_error_reference`；原始错误体仅以 `sha256:<前16位>` 引用存储。
- **幂等去重**：`tasks.idempotency_key`、`api_request_logs.request_id`、`cost_records(request_id, COALESCE(attempt_id,''))`。
- **版本化/审计**：`config_versions`（配置版本）+ `audit_events`（所有安全检查与决策审计）。
