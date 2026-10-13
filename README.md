# NexusAI Lite

NexusAI Lite（智枢 AI 错峰调度与夜间开发工具）是面向单用户/小团队的智能 API 错峰调度与夜间开发工具。它采集 DeepSeek 官方公开的计价与服务状态信息、记录自身 API 调用与成本、分析错峰规律，并据此在高峰时段降载非紧急任务、在授权夜间窗口内执行已授权的开发任务（仅生成补丁草案供人工检查）。

**NexusAI Lite** is a single-user/small-team tool for DeepSeek API off-peak scheduling and sandboxed night-time development — draft-only, no auto-merge.

> 详细产品需求与技术设计见 [`docs/reference.md`](docs/reference.md)（V3.1）。

## 环境要求

- Python 3.12（`pyproject.toml` 声明 `>=3.11`）
- [uv](https://docs.astral.sh/uv/)（依赖与 `.venv` 由 `uv.lock` 锁定）
- SQLite（标准库内置，WAL 模式）

## 快速开始

```bash
# 安装依赖并创建虚拟环境
uv sync

# 配置 DeepSeek API Key（存环境变量，绝不提交）
export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 初始化数据库（创建 schema 与默认配置 v1）
uv run nexusai-lite db init

# 采集官方价格页与服务状态页
uv run nexusai-lite collect

# 汇总自身 API 调用统计
uv run nexusai-lite stats

# 按时段桶分析自身调用规律
uv run nexusai-lite analyze

# 发起一次 DeepSeek 模型请求（经统一 API Client）
uv run nexusai-lite request --prompt "你好"

# 查看当前生效配置
uv run nexusai-lite config show
```

使用自定义数据目录 / 数据库路径（可选）：

```bash
export NEXUSAI_DATA_DIR=/path/to/data
export NEXUSAI_DB_PATH=/path/to/nexusai.db
uv run nexusai-lite db init
uv run nexusai-lite config show
```

## CLI 命令

```bash
uv run nexusai-lite db init                          # 初始化数据库
uv run nexusai-lite config list                      # 列出配置版本
uv run nexusai-lite config show [--version N] [--json]
uv run nexusai-lite config propose --file c.yaml --reason "调整高峰时段"
uv run nexusai-lite config activate --version N      # 激活（人工步骤）
uv run nexusai-lite config rollback --version N      # 回滚
uv run nexusai-lite collect                          # 采集官方价格/状态页
uv run nexusai-lite stats [--days N] [--group-by provider|model] [--json]
uv run nexusai-lite analyze [--days N] [--json]
uv run nexusai-lite request --prompt "..." [--model M] [--max-tokens N]
```

> CLI 命令全集以 `src/nexusai_lite/cli.py` 为准。任务状态机、调度器、预算、熔断、告警、夜间开发执行器、预测与影子模式目前为**模块级能力**，未全部封装为 CLI 子命令。详见 [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md)。

## 环境变量

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（存环境变量；`.env` 亦可，已被 gitignore） |
| `NEXUSAI_DATA_DIR` | 数据目录（覆盖默认 `~/.local/share/nexusai-lite`） |
| `NEXUSAI_DB_PATH` | 数据库文件路径（覆盖默认 `data_dir/nexusai.db`） |

`.env.example` 只包含变量名与非敏感占位值，不含真实密钥。

## 开发

```bash
uv run pytest             # 运行测试
uv run ruff check .       # 代码风格与静态检查
uv run basedpyright src tests   # 类型检查
```

## 架构概览

- 官方信息采集（$S1）→ 自身请求记录与统计 → 时段规律分析
- 任务状态机（幂等/有限重试/`UNKNOWN`）→ 错峰调度（预算、熔断、容量、优先级、时段规则）
- 夜间开发（授权窗口、本地沙箱 + 命令/目录白名单 + Secret Scan + 补丁草案）
- 阶段 4 预测与影子模式（只读 / advisory-only）

详见文档（中文，以实际代码为准）：

| 文档 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块地图与数据流 |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | 数据表 / 迁移 / 约定 |
| [`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md) | 任务状态机 / 幂等 / 恢复 / UNKNOWN |
| [`docs/EVENTS.md`](docs/EVENTS.md) | 审计事件与告警类型 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | 沙箱现实、密钥、日志脱敏、人工门 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 本地部署与定时调度 |
| [`docs/PROVIDER_ADAPTER.md`](docs/PROVIDER_ADAPTER.md) | Provider Adapter 契约与新增 Provider |
| [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) | 已知限制、阻塞项与路线图 |

## 阻塞项（待确认，使用安全默认值并标记待定）

以下属于**人工业务决策**，当前以安全默认值落地并标记待定，**开发 Agent 不得自行扩大权限或成本**（详见 [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md)）：

- **预算数值**：`budget` 各上限字段为 `null`（币种待定），价格表 `pricing.table` 为空（`pricing_version="unverified-seed"`），成本估算在官方价格采集前返回未知。
- **沙箱形态**：本地受限进程沙箱，网络隔离依赖 `unshare -n` 可用性（`SECURITY.md`）。

## 目录结构

```
pyproject.toml          # 项目与工具配置（ruff/basedpyright/pytest/uv）
uv.lock                 # 锁定依赖
.python-version         # Python 版本（3.12）
src/nexusai_lite/
  cli.py                # CLI 入口（nexusai-lite）
  config.py             # 版本化配置管理（Pydantic v2）
  tasks.py              # 任务状态机
  scheduler.py          # 错峰调度器
  circuit_breaker.py    # 简化熔断
  budget.py             # 预算硬上限
  alerts.py             # 告警
  api_client.py / records.py   # 统一 API Client
  providers/            # Provider Adapter（base/deepseek）
  collector/            # 官方采集与时段规则
  stats.py / analysis.py       # 统计与分析
  forecast.py / shadow.py      # 预测与影子模式（只读）
  nightdev.py / executor.py    # 夜间开发规划与执行
  sandbox.py / whitelist.py / secretscan.py   # 沙箱/白名单/密钥扫描
  secrets.py / logging_setup.py / db.py / paths.py
  migrations/001..008   # SQLite 迁移
docs/                   # 参考文档与本文档
tests/                  # pytest 测试
```
