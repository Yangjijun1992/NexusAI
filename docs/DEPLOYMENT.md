# NexusAI Lite 部署

> 单用户/小团队本地单机部署。本工具**不提供自动部署 / 自动合并能力**；生产变更始终由本机使用者人工审批（§12 规则 8、§4.4）。相关：[`SECURITY.md`](SECURITY.md)、[`ARCHITECTURE.md`](ARCHITECTURE.md)、[`README.md`](../README.md)。

## 环境要求

- Python 3.12（见 `.python-version`；`pyproject.toml` 声明 `requires-python >=3.11`）。
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境由 `uv.lock` 锁定）。
- SQLite（内置于标准库，WAL 模式）。

## 本地安装

```bash
# 安装依赖并创建 .venv（按 uv.lock 锁定版本）
uv sync

# 配置 DeepSeek API Key（仅存环境变量，绝不提交）
export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 自定义数据目录 / 数据库路径（可选，见 paths.py）
export NEXUSAI_DATA_DIR=/path/to/data
export NEXUSAI_DB_PATH=/path/to/nexusai.db

# 初始化数据库（创建 schema + 默认配置 v1）
uv run nexusai-lite db init
```

默认数据目录：`~/.local/share/nexusai-lite/`（或 `$XDG_DATA_HOME/nexusai-lite/`）；默认数据库文件 `nexusai.db`。路径可被 `NEXUSAI_DATA_DIR` / `NEXUSAI_DB_PATH` 覆盖。

## 数据库（SQLite/WAL）

- `db.connect` 启用 `PRAGMA journal_mode=WAL`（并发读写）、`foreign_keys=ON`、`busy_timeout=5000`。
- 迁移按 `migrations/*.sql` 文件名顺序、在 `BEGIN IMMEDIATE` 事务内幂等应用，已应用版本记于 `schema_migrations`。
- 数据库文件、WAL/SHM 文件（`*.db-wal` / `*.db-shm`）被 `.gitignore` 忽略，不会提交。
- 备份：SQLite 单文件，可复制 `nexusai.db` 备份；恢复时直接放回即可（可参考「可运营性：数据库备份可恢复」验收项）。

## CLI 子命令（并存于 `cli.py`）

| 子命令 | 作用 |
|---|---|
| `db init` | 初始化数据库与默认配置 |
| `config list` | 列出配置版本 |
| `config show [--version N] [--json]` | 显示当前生效或指定版本配置 |
| `config propose --file F --reason R [--created-by U]` | 提交新配置（proposed） |
| `config activate --version N` | 激活某版本（人工步骤） |
| `config rollback --version N` | 回滚到某版本（等价激活） |
| `collect` | 采集官方价格页与服务状态页 |
| `maintain [--json]` | 执行维护（崩溃恢复 + 过期清理 + 陈旧预留清理），应周期性调度 |
| `stats [--days N] [--group-by provider\|model] [--json]` | 汇总自身调用统计 |
| `analyze [--days N] [--json]` | 按时段桶分析自身调用规律 |
| `request --prompt P [--model M] [--max-tokens N]` | 发起一次 DeepSeek 请求（经统一 API Client） |

全局参数：`--log-level {DEBUG,INFO,WARNING,ERROR}`、`--log-json`。

## 定时调度（可选）

- **采集（`collect`）**：可用系统 cron 定时执行，例如每日多次抓取官方价格/状态页。本工具**没有内置调度器 CLI**；需要周期性采集、夜间规划或告警扫描时，用系统 cron 或用户级 `systemd` timer 调用对应命令/模块即可。
- **维护（`maintain`）**：建议周期性（如每日）执行 `nexusai-lite maintain`。它从配置播种 `platform` 预算上限、清理已过期的陈旧预算预留、并做崩溃恢复（超期 `RUNNING` → `UNKNOWN`、超截止时间 → `EXPIRED`）。这满足 §3 崩溃恢复要求，配合执行器端到端保证不留悬挂预留或卡在 `RUNNING` 的任务。示例 cron（每日 01:00）：
  ```
  0 1 * * *  cd /path/to/nexusai && uv run nexusai-lite maintain
  ```
- **夜间开发规划（`nightdev`）**：`NightDevPlanner` 是模块级功能，当前**没有封装为 CLI 子命令**，也未暴露内置定时器。需自行在夜间窗口前调用其 `plan`（参考 `nightdev.py` 文档）将已授权任务创建并入队，再由执行器消费。请勿自行引入自动合并/自动部署。

> 说明：夜间窗口判断是**模块逻辑**（`nightdev.is_allowed_window`），不依赖 cron 时区；把 cron 设在全球协调时（UTC）时注意与配置中的 `Asia/Shanghai` 时区换算一致。

## 配置管理

配置经 `config_versions` 版本化，`db init` 播种默认 v1。修改流程：`config propose`（受保护项 `budget` / `sandbox` / `whitelist` / `concurrency` 需人工）→ `config list` 查看 → `config activate`（人工）→ 异常时 `config rollback`。每次生效写入审计（`EVENTS.md`）。

## 生产安全提示

- **无自动部署**：仓库/数据库变更、代码合并、生产发布默认需本机使用者人工审批。
- **密钥不落盘**：API Key 只放环境变量；`.env` 已被 `.gitignore` 忽略。
- **非 root 运行**：夜间开发沙箱拒绝以 root 执行（`sandbox.py`）。
- **诚实了解沙箱边界**：网络隔离依赖 `unshare -n` 可用性，多数无特权环境不可用；不要仅依赖沙箱作为唯一防线（见 `SECURITY.md`）。
- 参考文档要求**生产变更需人工审批、禁止自动合并/自动部署**；本工具不给出一键生产发布路径。
