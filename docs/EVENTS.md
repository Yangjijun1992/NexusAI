# NexusAI Lite 事件与审计

> 本文档枚举实际由代码发出的审计事件（`audit_events` 表）与告警（`alerts` 表）类型。以 `src/nexusai_lite/*.py` 为准，对应参考文档的「可解释、可追踪」（§4.4、§12 规则 6/12）。相关：[`DATA_MODEL.md`](DATA_MODEL.md)。

## 统一审计事件表 `audit_events`

所有写入事务内完成，字段统一：

| 列 | 说明 |
|---|---|
| event_type | 事件类型（下文枚举） |
| subject | 主体：人（如 `user`）、系统（`system`）、维度（如熔断 dimension）、`budget` |
| object_type | 对象类型（如 `config_versions`、`circuit_breakers`、`budget`） |
| object_id | 对象 ID（如版本号、dimension、account_id） |
| at | 事件时间（UTC ISO-8601） |
| detail_json | 结构化细节（JSON，已脱敏） |

## 审计事件类型（event_type）

| 事件类型 | 来源模块 | 触发时机 | detail_json |
|---|---|---|---|---|
| `ConfigProposed` | `config.propose_config` | 提交一份新配置（status=proposed） | 无 |
| `ConfigActivated` | `config.activate_config` / `seed_if_empty` | 激活某版本（把旧 active 降级 superseded）；`db init` 播种默认 v1 时由系统发出 | 无 |
| `ConfigRolledBack` | `config.rollback_config` | 回滚到某旧版本（重新激活该版本并降级当前 active） | 无 |
| `PolicyChanged` | `config.activate_config` / `config.rollback_config` | 配置生效（激活或回滚）时发布，携带版本与变更原因 | `{version, change_reason}` |
| `CircuitBreakerClosed` | `circuit_breaker.record_success` / `reset` | 半开连续成功达阈值关闭熔断，或人工重置 | `{state, at}` / `{state, reason: manual_reset}` |
| `CircuitBreakerHalfOpened` | `circuit_breaker._load_resolved` | OPEN 冷却期后自动转半开 | `{state, at}` |
| `CircuitBreakerOpened` | `circuit_breaker._open_from` | 连续失败达阈值 / 半开探测失败重新打开 | `{state, opened_at}` |
| `BudgetThresholdReached` | `budget.reserve` | 预留后用量越过软阈值 | `{committed, reserved, total}` |
| `BudgetLimitsChanged` | `budget.set_limits` | 账户限额被创建或修改 | `{scope, soft_limit, hard_limit, currency, changed}` |

> **关于 `ConfigRolledBack` 与 `PolicyChanged`（X-04 已完成）：**
> - **`ConfigRolledBack`**：`rollback_config` 现发出独立的 `ConfigRolledBack` 审计事件，并在重新激活目标版本后发布 `PolicyChanged`。
> - **`PolicyChanged`**：`activate_config` / `rollback_config` 均在配置生效时发布，携带 `{version, change_reason}`；`seed_if_empty`（播种默认 v1）仅发 `ConfigActivated`，不额外发 `PolicyChanged`。

## 告警 `alerts` 表

告警经 `AlertManager` 去重（`dedup_key = code|scope`，窗口默认 300 秒）、脱敏（`title/message/detail` 经 `redact_text` 并截断）、持久化并派发。`status`：`raised` / `dispatched` / `dispatch_failed`（通知失败有限重试）。严重级别：`info` / `warning` / `critical`。

### 内置检测器告警码（`alerts.py`）

| 代码 | 严重级别 | 触发条件 | scope / 关联事件 |
|---|---|---|---|
| `RATE_LIMIT_RATIO_HIGH` | warning / critical | 近 15 分钟 429 比率 ≥20%（warning）/ ≥50%（critical），样本 ≥5 | （全局） |
| `SERVER_ERROR_RATE_HIGH` | warning | 近 15 分钟 5xx/过载比率 ≥20%，样本 ≥5 | （全局） |
| `CIRCUIT_BREAKER_OPEN` | critical | 某熔断维度处于 OPEN | scope=dimension |
| `BUDGET_HARD_LIMIT` | critical | 账户已用超硬上限 | scope=账户 scope |
| `BUDGET_SOFT_THRESHOLD` | warning | 账户已用越软阈值 | scope=账户 scope |
| `TASK_UNKNOWN` | critical | 存在 UNKNOWN 任务（需人工接管） | scope=`task:<id>`，event_id=task_id |
| `AUTHENTICATION_FAILURE` | critical | 近 15 分钟存在认证错误（需人工轮换凭据） | scope=provider，event_id=request_id |

### 执行器 / 密钥扫描产生的告警码

| 代码 | 来源 | 触发 | scope |
|---|---|---|---|
| `BUDGET_EXCEEDED` | `executor` | 夜间开发任务预算预留超限（任务转 FAILED） | `task:<id>` |
| `SECRET_LEAK_DETECTED` | `secretscan.assert_clean` | Secret Scan 检测到高危密钥（`High` 级别），任务阻断并隔离工作区 | `task:<id>`（可空） |

## 统一事件字段（适用性）

- **审计事件**：`event_type` / `subject` / `object_type` / `object_id` / `at` / `detail_json` 全表统一。
- **告警**：`alert_id` / `severity` / `code` / `scope` / `title` / `message` / `event_id` / `occurred_at` / `status` / `dedup_key` / `detail_json`。
- 所有载荷在写入前已脱敏（§4.3、§12 规则 6）：绝不包含密钥、Authorization Header、完整 Prompt 或敏感代码。原始错误体仅以 `sha256:<前16位>` 引用进入 `raw_error_reference`。

## 相关

- 调度决策本身记录于 `schedule_decisions`（`decision_id` + `reason_codes` + `policy_version`），可通过 `scheduler.explain(decision_id)` 追溯，是格式化的「决策审计」，不同于 `audit_events`。参见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
