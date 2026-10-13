# NexusAI Lite 系统架构

> 本文档描述 `src/nexusai_lite/` 各模块的职责、依赖与数据流。以实际代码为准，与 [`reference.md`](reference.md) V3.1 对应。详见 [`README.md`](../README.md)、[`DATA_MODEL.md`](DATA_MODEL.md)、[`STATE_MACHINE.md`](STATE_MACHINE.md)、[`EVENTS.md`](EVENTS.md)、[`SECURITY.md`](SECURITY.md)、[`PROVIDER_ADAPTER.md`](PROVIDER_ADAPTER.md)、[`DEPLOYMENT.md`](DEPLOYMENT.md)。

NexusAI Lite 是单用户/小团队的智能 API 错峰调度与夜间开发工具。核心主线：采集官方信息与自身调用 → 分析时段规律 → 错峰调度 → 夜间开发（沙箱 + 补丁草案供人工检查）→ 反馈优化（预测 / 影子模式共阶段 4）。

## 总体分层

```text
CLI（cli.py）: db / config / collect / stats / analyze / request
   |
   +-- 采集 (S1)      collector/official.py, collector/window_rules.py
   +-- 记录统计 (S1)  api_client.py -> stats.py（api_request_logs / cost_records）
   +-- 分析 (S1)      analysis.py（时段桶统计）
   +-- 任务与状态机    tasks.py（tasks / task_runs / task_attempts）
   +-- 调度 (S2)      scheduler.py + circuit_breaker.py + budget.py + retry_policy.py
   +-- 告警 (S2/S3)   alerts.py（alerts 表）
   +-- 夜间开发 (S3)  nightdev.py -> executor.py -> sandbox.py + whitelist.py + secretscan.py
   +-- 优化 (S4)      forecast.py（只读预测） + shadow.py（影子模式）
   |
   公共基础设施：db.py（SQLite/WAL/迁移）+ paths.py + secrets.py + logging_setup.py
```

## 模块一览

| 模块 | 阶段 | 职责 | 读写表 |
|---|---|---|---|
| `cli.py` | 横切 | 命令行入口（`nexusai-lite`），子命令 `db init` / `config list&#124;show&#124;propose&#124;activate&#124;rollback` / `collect` / `stats` / `analyze` / `request` | 调用各模块 |
| `db.py` | 横切 | SQLite 连接（WAL/外键/busy_timeout）、按文件名顺序幂等迁移、`init_db` | `schema_migrations` |
| `paths.py` | 横切 | 解析数据目录与数据库路径（`NEXUSAI_DATA_DIR` / `NEXUSAI_DB_PATH`） | 无 |
| `secrets.py` | S1-06 | Provider API Key 读取（`{PROVIDER}_API_KEY`）、登记注册表、`mask_secret` | 无 |
| `logging_setup.py` | S1-07 | `nexusai` 包级 logger、`redact_text` 全链路脱敏、JSON/文本输出 | 无 |
| `config.py` | S1-09 | 版本化配置（Pydantic v2）+ 审计，`propose/activate/rollback` | `config_versions` `audit_events` |
| `collector/official.py` | S1-01 | 抓取官方价格页与服务状态页、保存快照、失败不伪造 | `official_snapshots` `window_rules` |
| `collector/window_rules.py` | S1-02 | 解析计价时段、版本化管理 `window_rules`、`is_peak`/`is_workday` | `window_rules` |
| `api_client.py` | S1-03 | 统一 API Client：请求、有限重试（尊重 Retry-After）、错误分类、用量/成本、流式 | 经 logger 写 `api_request_logs` |
| `records.py` | S1-03 | 冻结接口：`ErrorCategory`、`Usage`、`RequestRecord`、`RequestLogger` 协议 | 无 |
| `providers/` | S1-03 | `ProviderAdapter` 抽象基类 + `DeepSeekAdapter` 实现 | 无 |
| `stats.py` | S1-04 | 持久化请求记录、聚合自身调用统计（成功率/429/5xx/延迟分位/Token/成本） | `api_request_logs` `cost_records` |
| `analysis.py` | S1-05 | 按 工作日高峰/空闲/夜间/周末/节假日 五桶统计，无状态 | 只读 `api_request_logs` |
| `tasks.py` | S2-01..03 | 任务状态机（幂等/有限重试/恢复/UNKNOWN），状态转换在 `BEGIN IMMEDIATE` 事务内 | `tasks` `task_runs` `task_attempts` |
| `retry_policy.py` | S2-04 | 429/503 退避与有限重试策略（纯函数） | 无 |
| `circuit_breaker.py` | S2-05 | 简化熔断 CLOSED/OPEN/HALF_OPEN，维度隔离、审计 | `circuit_breakers` `audit_events` |
| `scheduler.py` | S2-06 | 错峰调度决策链 + 决策记录（只读调度器，不改任务状态） | `schedule_decisions` |
| `budget.py` | S2-07 | 预算账户与预留，原子防并发突破硬上限 | `budget_accounts` `budget_reservations` |
| `alerts.py` | S2-08 | 告警检测器 + 去重持久化 + 派发，载荷脱敏 | `alerts` |
| `nightdev.py` | S3-01 | 授权窗口（夜间/周末/节假日）内创建并入队开发任务 | `tasks` 读 `window_rules` |
| `executor.py` | S3-02/05/06 | 开发任务执行器：守卫管线（预算→白名单→沙箱→密钥扫描→补丁草案） | `tasks` `task_runs` `task_attempts` `budget_*` |
| `sandbox.py` | S3-03 | 受限本地沙箱（非 root、rlimits、env scrub、`unshare -n` 诚实探测网络隔离） | 无 |
| `whitelist.py` | S3-04 | 命令/目录白名单 + 工具参数结构化校验 | 无 |
| `secretscan.py` | S3-07 | Secret Scan 与静态安全扫描、高危阻断并告警 | 经 AlertManager 写 `alerts` |
| `forecast.py` | S4-01/02 | 只读季节性预测与时间序列切分回测（advisory-only） | 只读 `api_request_logs` `config_versions` |
| `shadow.py` | S4-03/04 | 影子模式：只记录「调度器将如何选择」，策略更新走人工审批 | `shadow_recommendations` |

## 依赖与数据流

```text
collect（cli）
  -> collector/official.py: official_snapshots
  -> collector/window_rules.py: window_rules（解析计价时段，upsert 版本）

request / 执行器（api_client.py）
  -> providers/<adapter>: build_request / classify_error / extract_usage / estimate_cost
  -> stats.DbRequestLogger: api_request_logs + cost_records（request_id 去重）

analyze（cli）
  -> window_rules（读取活跃时段规则）+ analysis.period_stats: 只读统计

nightdev.plan（S3-01）
  -> window_rules（读取规则；rule 缺失时 is_allowed_window 保守返回 False）
  -> tasks.create_task / queue（idempotency_key = nightdev:{name}:{local_date}）

调度（scheduler, S2-06）
  决策链：安全/授权 > 预算硬上限(budget) > 熔断(circuit_breaker) > 自身容量
        > 任务优先级/截止时间 > 官方时段规则(window_rules)
  -> schedule_decisions（decision + reason_codes + metrics_summary + policy_version）

执行（executor, S3）
  tasks <-> task_runs / task_attempts <-> budget_reservations
  -> whitelist.validate_tool_call（命令/目录白名单 + shell 元字符拒绝）
  -> sandbox.LocalSandbox.run（rlimits + env scrub + 网络隔离）
  -> secretscan.assert_clean（高危密钥阻断）
  -> 补丁草案 drafts/<task_id>.patch（仅草案，branch=None，不合并/不部署）

优化（S4，advisory-only）
  forecast.predict_peaks / backtest: 只读 api_request_logs，不写调度状态
  shadow.recommend: 写 shadow_recommendations（不改 tasks/schedule_decisions）
  -> propose_policy_update: 生成 proposed 配置（human_approval=True），激活仍人工 CLI
```

## 关键设计点

- **数据边界（§1.3）**：官方公开信息（`official_snapshots` / `window_rules`）与自身调用（`api_request_logs` / `cost_records`）分开存储，绝不混淆。
- **统一入口（§12 规则 1）**：所有模型请求唯一经 `api_client.ApiClient`，业务模块不直接调用供应商 SDK。
- **只读调度器**：`scheduler.decide` 只评估并写 `schedule_decisions`，不改任务状态；消费者按 `ALLOW` 取走执行。
- **advisory-only 优化**：`forecast` 与 `shadow` 只做只读建议，不控制真实调度；配置激活始终是人工 CLI 步骤。
- **状态一致性（§3）**：任务、配置、预算、告警、熔断的所有写操作都在 `BEGIN IMMEDIATE` 事务内，依赖 SQLite WAL + 显式事务。
- **时间戳约定**：统一 UTC ISO-8601 TEXT（见 `db.utcnow_iso`）。
