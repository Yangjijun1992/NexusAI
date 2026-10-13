# NexusAI Lite 安全模型

> 本文档诚实描述本版本实际的安全边界，对应 [`reference.md`](reference.md) §4、§5.2、§11、§12。**不宣称代码里不存在的隔离能力**。核心事实：夜间开发沙箱是**受限本地子进程沙箱**（非容器），网络隔离为「尽力而为并在不可用时如实声明」。相关：[`DEPLOYMENT.md`](DEPLOYMENT.md)、[`EVENTS.md`](EVENTS.md)。

## 沙箱现实与限制（`sandbox.py`）

> **重要的诚实声明：`LocalSandbox` 不是 OS 沙箱 / 容器隔离边界。** 它是一个
> 资源与环境硬化的**同用户子进程**环境。被授权解释器（如 `python -c "..."`、
> `git`）可以读取当前用户能读取的任何宿主文件（`/etc/passwd`、同用户的密钥文件、
> 其他工作目录等），本沙箱**不会**阻止这种同权限文件系统读取。若需运行**不可信**、
> **不受信任的任意代码**，必须使用（当前未实现的）`ContainerSandbox` 后端或真正的
> 容器/虚拟机隔离。`network_isolated` 标志如实反映网络命名空间是否真实生效，绝不
> 伪称。

`LocalSandbox` 在单机环境下真实强制以下隔离（均为资源/环境硬化，**非** OS 边界）：

- **非 root 运行（§4.1）**：构造与 `run` 都检查 `os.geteuid() == 0`，为 root 时抛 `SandboxError` 拒绝执行。
- **per-run 工作目录**：每次 run 创建唯一 `ws-<uuid>` 目录，结束后 `shutil.rmtree` 销毁（`keep_workspace=True` 时保留供调试）。
- **环境净化（env scrub）**：不继承宿主机完整环境。仅白名单注入 `PATH` / `LANG` / `LC_ALL` / `TZ`；`HOME` / `TMPDIR` / `XDG_CACHE_HOME` / `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME` 指向隔离工作区。
- **rlimits（`apply_rlimits`）**：CPU（`RLIMIT_CPU`）、内存（`RLIMIT_AS`）、单文件大小（`RLIMIT_FSIZE`）、打开文件数（`RLIMIT_NOFILE`）、进程数（`RLIMIT_NPROC`）。除 `NPROC` 外全部 fail-closed（设置失败即抛错）。
- **不 shell 拼接**：`shell=False` + `argv` 列表执行；cwd 经 `DirectoryWhitelist` 约束在工作区根内。
- **输出上限**：stdout/stderr 截断到 64 KiB（`MAX_OUTPUT_CHARS`）。

### 诚实的网络隔离声明（重要）

- 网络隔离依赖 `unshare -n`（Linux 网络命名空间）。**本地实现会真实探测该机制是否可用**：非 root + 无 `CAP_SYS_ADMIN` 时通常不可用。
- 仅当 `unshare -n` 探测成功才应用命名空间，并把 `SandboxResult.network_isolated` 置为 `True`；**否则如实置 `False`，绝不伪称已隔离出站网络**。
- 因此：**本工具不保证默认阻断出站网络**。在 `unshare -n` 不可用的环境（多数无特权场景），被沙箱执行的命令仍可访问网络。对外传数据（代码、日志、Prompt、密钥）的防护主要依赖命令白名单 + Secret Scan + 人工检查，而非强网络隔离。

### 容器后端

- `ContainerSandbox` 是未来 Docker/microVM 后端的接缝，`run` **始终抛出 `NotImplementedError`**，不提供假实现。本版本没有真实容器隔离。

## 命令/目录白名单（`whitelist.py`）

- **显式命令白名单（非黑名单）**：`CommandWhitelist` 只允许列出的命令 basename（小写规范化），空 argv 视为不允许。
- **目录白名单**：`DirectoryWhitelist` 用 `Path.resolve`（展开符号链接与 `..`）约束路径落在授权根目录内，越界抛 `PathNotAllowedError`。
- **工具参数结构化校验（`validate_tool_call`）**：
  - `argv[0]` 必须在命令白名单；
  - `argv` 任一元素含 shell 元字符（`; | & $ \` > <` 与换行）即拒绝，防参数注入与命令拼接绕过；
  - cwd 与绝对路径参数解析后必须落在授权 roots 内。
- 沙箱与白名单在 `executor` 中强制组合：`validate_tool_call` 在进沙箱前校验，`LocalSandbox` 再次以目录白名单约束 cwd。危险操作由代码策略阻止，而非仅靠模型 Prompt 拒绝。

## 密钥与凭据（`secrets.py`）

- API Key 仅存于环境变量 `{PROVIDER}_API_KEY`（如 `DEEPSEEK_API_KEY`）或本地 `.env`；绝不进入代码、日志、Prompt 或命令行参数。
- `.env` 惰性加载一次（不覆盖既有环境变量）。
- 读到的密钥自动登记到进程级注册表（`register_secret`），供 `redact_text` 在日志写出前整值替换。
- `mask_secret` 最多保留末尾 4 字符（`***<tail>`）。
- 模型调用经统一入口 `api_client` 添加 `Authorization: Bearer`，Provider Adapter 不接触密钥。
- 怀疑泄露时：人工吊销/轮换环境变量并暂停关联任务（告警 `AUTHENTICATION_FAILURE` / `SECRET_LEAK_DETECTED`）。

## 日志与数据脱敏（`logging_setup.py`）

- `nexusai` 包级 logger 的 handler 附带 `RedactionFilter`，写前对 msg/args/exc_text 脱敏；文本格式器与 JSON 格式器都会再脱敏一次。
- `redact_text`：①替换所有已登记秘密值；②按正则替换内置敏感模式（`sk-` 密钥、Authorization header、Bearer token、api key/token/secret、cookie）为 `[REDACTED]`。
- 默认不保存完整 Prompt 与完整响应；`retention.store_full_prompt` 默认 `False`（配置项）。
- 业务数据库只存 `error_code` / `error_category` / `safe_error_message`（脱敏）与受控 `raw_error_reference`（`sha256:<前16位>`），原始错误体只进受控日志系统（§4.3）。

## 数据保留与脱敏

- `retention.log_retention_days` 默认 90 天（配置项，可在 `config` 管理）。
- 告警载荷、调度决策 `metrics_summary`、密钥扫描摘录均经 `redact_text` 脱敏。
- Secret Scan 的文件摘录逐字以 `[REDACTED]` 替换（`secretscan._line_and_excerpt`）。

## 夜间开发：draft-only + 人工审批（§12 规则 8）

- 执行器 `DevExecutor` 只生成补丁草案 `drafts/<task_id>.patch`，`branch` 恒 `None`，**不创建生产分支、不自动合并、不自动部署**。
- 窗口门控（`nightdev.is_allowed_window`）：仅 ①假日日期、②非工作日（规则定义的周末）、③`[night_start, night_end)` 夜间窗口（支持跨午夜）为授权窗口；**活跃时段规则缺失时保守返回 False，不启动**。工作日白天（即使空闲计价时段）不视为授权窗口。
- 每日幂等：`idempotency_key = nightdev:{name}:{local_date}`，避免重复创建。
- `_confirm_draft` 确认补丁草案已持久化；返回 False（外部副作用不确定）时任务转 UNKNOWN 交人工。

## 人工审批门（§4.4、§7、§12）

- **配置提交**：`config propose` 的受保护配置项（`budget` / `sandbox` / `whitelist` / `concurrency`，见 `config._PROTECTED_KEYS`）仅在 `human_approval=True` 时允许修改，否则抛 `ProtectedConfigError`；CLI 路径调用时传 `human_approval=True`。影子模式 `propose_policy_update` 有额外防御：即使 `human_approval=True` 也只允许触碰 `windows.peak_windows`，任何受保护键出现在提案差异中都会被拒绝。
- **配置激活/回滚**：`config activate` / `config rollback` 是人工 CLI 步骤；影子模式 `propose_policy_update` 只生成 `proposed` 版本，激活仍由人工执行。
- **UNKNOWN 任务**：`resolve_unknown` 仅人工处置；有 `TASK_UNKNOWN` 告警提示接管。
- **生产变更**：代码合并、部署、预算上调、权限扩大、安全策略变更默认需人工审批；夜间开发结果须人工检查后才可继续。

## 应避免的误解

- 本工具**不是**强隔离容器运行时；`ContainerSandbox` 未实现。
- 网络隔离是**尽力而为**的（依赖 `unshare -n` 可用性），不能依赖它作为唯一防线。
- 沙箱是单机**同用户受限子进程**（rlimits + env scrub + 白名单），**不是** OS 或虚拟化
  层面的隔离边界；被授权解释器可读取当前用户可读的任何宿主文件，不受沙箱阻止。
  宿主机的共享内核与文件系统仍需人工谨慎对待；运行不可信任意代码须用（未实现的）
  `ContainerSandbox` 后端。

- 参考文档中的 Docker Socket、云平台元数据、宿主敏感目录等阻断主要依赖「不提供访问手段 + 目录/命令白名单 + 人工检查」，而非容器级强制（当前版本）。

## 关联文档

- §4.1/§11 沙箱与网络白名单属于待确认项（`KNOWN_LIMITATIONS.md`）。
- 告警与审计事件：`EVENTS.md`。
