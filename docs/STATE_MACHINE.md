# NexusAI Lite 任务状态机

> 以 `src/nexusai_lite/tasks.py` 为准，对应 [`reference.md`](reference.md) §3 精简状态集与 S2-01/02/03。所有状态转换在 `TaskService` 内的 `BEGIN IMMEDIATE` 事务中执行（SQLite 事务 + UNIQUE 约束保证单机一致性，不引入分布式租约或发件箱）。

## 状态集

`TaskStatus`（`str, enum.Enum`）：

`PENDING`、`QUEUED`、`RUNNING`、`RETRY_WAIT`、`PAUSED`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`EXPIRED`、`UNKNOWN`。

## 允许转换表（TRANSITIONS）

| 从状态 | 允许到的状态 |
|---|---|
| PENDING | QUEUED, CANCELLED |
| QUEUED | RUNNING, PAUSED, CANCELLED, EXPIRED |
| RUNNING | SUCCEEDED, FAILED, RETRY_WAIT, PAUSED, CANCELLED, EXPIRED, UNKNOWN |
| RETRY_WAIT | QUEUED, CANCELLED, EXPIRED |
| PAUSED | QUEUED, CANCELLED |
| UNKNOWN | QUEUED, FAILED, SUCCEEDED（仅经 `resolve_unknown` 人工处理） |
| SUCCEEDED | （终态，不可再转） |
| FAILED | （终态，不可再转） |
| CANCELLED | （终态，不可再转） |
| EXPIRED | （终态，不可再转） |

图示：

```text
PENDING -> QUEUED -> RUNNING -> SUCCEEDED（终态）
             |         |  -> RETRY_WAIT -> QUEUED
             |         |  -> FAILED（终态）
             |         |  -> CANCELLED（终态）
             |         |  -> EXPIRED（终态）
             |         |  -> PAUSED -> QUEUED
             |         |  -> UNKNOWN ->（人工）QUEUED / FAILED / SUCCEEDED
             |         |
             +- PAUSED -+
```

- `TERMINAL = {SUCCEEDED, FAILED, CANCELLED, EXPIRED}`：不可再转换。
- `UNKNOWN` 只能由人工 `resolve_unknown(task_id, outcome)` 转至 `QUEUED` / `FAILED` / `SUCCEEDED`。

## 转换方法（TaskService）

| 方法 | 转换 | 说明 |
|---|---|---|
| `create_task` | → PENDING | 幂等创建；非空 `idempotency_key` 重复提交返回既有任务；并发竞态由 UNIQUE 约束经 `IntegrityError` 回退按键查询 |
| `queue` / `unpause` | PENDING / RETRY_WAIT / PAUSED → QUEUED | 进入可执行队列 |
| `start` / `start_run` | QUEUED → RUNNING | 记录 `started_at`；`start_run` 额外写 `task_runs` 行并返回 `execution_id` |
| `succeed` | RUNNING → SUCCEEDED | 记 `finished_at` 与 `actual_cost`/`cost_status` |
| `fail` | RUNNING → FAILED | 写脱敏错误字段与成本 |
| `schedule_retry` | RUNNING → RETRY_WAIT（或 FAILED） | 递增 `attempt_no`；若下次尝试超过 `max_attempts` 或累计成本突破 `max_cost`，转 FAILED（错误码 `MAX_ATTEMPTS_OR_BUDGET`） |
| `pause` | QUEUED / RUNNING → PAUSED | 暂停（含高峰降载暂停非紧急任务） |
| `cancel` | 非终态 → CANCELLED | 记 `finished_at`，错误码 `CANCELLED`、分类 `cancelled`，`safe_error_message` 注明「外部副作用可能已发生」 |
| `expire` | 非终态 → EXPIRED | 超过 `deadline_at` 或有效期 |
| `mark_unknown` | RUNNING → UNKNOWN | 执行结果不确定，不盲目重试；分类 `unknown_error` |
| `resolve_unknown` | UNKNOWN → QUEUED / FAILED / SUCCEEDED | 仅人工处置 |
| `finish_run` | 关闭一次执行记录 | 写 `task_runs.finished_at` 与结果 |
| `record_attempt` | 写 `task_attempts` 行 | 返回 `attempt_id` |

每个转换前调用 `_validate_transition`，非法转换抛 `InvalidTransitionError`。

## 幂等 / 尝试 / 恢复 / UNKNOWN 语义

### 幂等创建（§12 规则 2）
- `tasks.idempotency_key` 为 `UNIQUE`；`create_task` 先按键查询，已存在直接返回既有任务。
- 并发竞态：预检后另一连接已插入同一幂等键时，捕获 `IntegrityError` 回退到按键查询。
- 队列/消费者「至少一次投递」的重复投递不产生重复任务或重复执行。

### 尝试（Attempt）
- 每次执行（`execution_id`）可有多次尝试（`attempt_id` + `attempt_no`）。
- `schedule_retry` 按 `_retry_blocked` 判断：`attempt_no + 1 >= max_attempts` 或突破 `max_cost` 时停止重试转 FAILED。
- 重试原因经 `redact_text` 脱敏后写入 `safe_error_message` 以保持可追踪。

### 恢复（recover / expire_overdue）
- `TaskService.recover(stale_after_seconds=None)` 保守崩溃恢复：
  - `RUNNING` 超过执行窗口（默认取 `max_execution_seconds`）→ 转 **UNKNOWN**（不确定是否已产生副作用，不盲目重试，§12 规则 3）。
  - 其余非终态（`QUEUED/RUNNING/RETRY_WAIT/PAUSED`）超过 `deadline_at` → 转 **EXPIRED**。
- `expire_overdue()` 专责把超截止时间的非终态转 EXPIRED。
- 重启后：可安全恢复的（超期→EXPIRED）与需人工确认的（超时运行→UNKNOWN）被区分处理。

### UNKNOWN 语义（§12 规则 3）
- 无法可靠确认上次执行是否完成或产生外部副作用时进入 `UNKNOWN`，绝不盲目重试。
- 提供人工处理入口 `resolve_unknown`，仅可转为 QUEUED（重新入队）、FAILED 或 SUCCEEDED。
- `alerts.detect_unknown_tasks` 会对 UNKNOWN 任务产生 `TASK_UNKNOWN` CRITICAL 告警并提示人工接管。

### 取消的残余外部执行（F6）
- `cancel` 在 `safe_error_message` 中记录「取消时间与可能的残余外部执行」，错误码 `CANCELLED`、分类 `cancelled`。

## 相关字段

- `tasks`：`created_at`、`started_at`、`finished_at`、`deadline_at`、`attempt_no`、`max_attempts`、`max_execution_seconds`、`max_cost`。
- 错误字段只存脱敏形式：`error_code`、`error_category`、`safe_error_message`、`raw_error_reference`（§4.3）。
