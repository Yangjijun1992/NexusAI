"""本地受限沙箱（S3-03）。

落实 §4.1、§5.2、§12 规则 5：执行边界通过**实际的隔离机制**与负向测试验证，
而非仅仅依赖容器隔离的宣称（§5.1 明确禁止仅凭容器隔离宣称安全）。

:class:`LocalSandbox` 在单机环境下实现它能真实强制的隔离：

- 以非 root 运行（§4.1 禁止 privileged）——否则抛出 :class:`SandboxError`。
- 每个 run 创建独立、权限受限的 per-run 工作目录，结束后销毁。
- **不继承宿主机完整环境变量**：仅显式白名单（``PATH``/``LANG``/``LC_ALL``/``TZ``）
  注入，且 ``HOME``/``TMPDIR``/``XDG_*`` 指向隔离工作目录。
- 通过 ``preexec_fn`` 施加 rlimits（CPU、内存、文件大小、FD、进程数）。
- 以 ``shell=False`` 与 ``argv``（非字符串命令）执行，不做 shell 拼接。
- cwd 用 :class:`DirectoryWhitelist` 约束在工作区根内（§4.1 禁止挂载宿主根/敏感目录）。

**诚实声明（本模块不宣称做不到的隔离）：**

- 网络隔离依赖 ``unshare -n``。本地实现会探测该机制是否**真实可用**（非 root
  + 无 CAP_SYS_ADMIN 时通常不可用），仅当探测成功才应用命名空间并把
  ``SandboxResult.network_isolated`` 置为 ``True``；否则如实置 ``False``，
  绝不伪称已隔离出站网络。
- :class:`ContainerSandbox` 是未来 Docker/microVM 后端的接缝，`run` 抛出
  ``NotImplementedError``，不提供假实现。
"""

from __future__ import annotations

import contextlib
import os
import resource
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from nexusai_lite.whitelist import DirectoryWhitelist

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

# 捕获输出上限（64 KiB），限制内存占用并防止日志被海量输出撑爆。
MAX_OUTPUT_CHARS = 64 * 1024

# 环境变量显式白名单：仅这些从宿主机继承，其余一律不注入。
_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ")

_NET_UNSHARE_CACHE: dict[str, bool] = {}


class SandboxError(RuntimeError):
    """沙箱无法安全创建或执行的错误（如以 root 运行）。"""


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """资源限额（均为硬上限，同应用于子进程的 soft/hard rlimit）。

    Args:
        cpu_seconds: CPU 时间上限（秒），对应 RLIMIT_CPU。
        memory_mb: 地址空间上限（MiB），对应 RLIMIT_AS。
        max_processes: 进程数上限，对应 RLIMIT_NPROC。
        max_file_size_mb: 单文件大小上限（MiB），对应 RLIMIT_FSIZE。
        max_open_files: 打开文件描述符上限，对应 RLIMIT_NOFILE。
        wall_timeout_seconds: 默认墙钟超时（秒）。
    """

    cpu_seconds: int = 30
    memory_mb: int = 512
    max_processes: int = 64
    max_file_size_mb: int = 64
    max_open_files: int = 256
    wall_timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """一次沙箱执行的受控结果（已脱敏后的 stdout/stderr 见调用方处理）。"""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    killed_reason: str | None
    network_isolated: bool
    sandbox_kind: str


class Sandbox(Protocol):
    """沙箱统一接口，供 Stage-3 执行器调用。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        limits: ResourceLimits | None = None,
    ) -> SandboxResult:
        """在沙箱内执行 ``argv``，返回受控结果。

        Args:
            argv: 命令及其参数（不以 shell 拼接执行）。
            cwd: 命令工作目录（将被目录白名单规范化为授权路径）。
            timeout_seconds: 墙钟超时；为 ``None`` 时使用 limits 提供的默认值。
            limits: 资源限额；为 ``None`` 时使用默认 :class:`ResourceLimits`。
        """
        ...


class FakeSandbox:
    """测试替身：实现 :class:`Sandbox`，记录调用并按脚本返回结果。

    用于 Stage-3 执行器单元测试，不执行任何真实进程。
    """

    def __init__(self, result: SandboxResult | None = None) -> None:
        """初始化测试替身，可预置一个固定的脚本化结果。

        Args:
            result: 每次 ``run`` 返回的预置结果；为 ``None`` 时返回默认成功结果。
        """
        self._result = result
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        limits: ResourceLimits | None = None,
    ) -> SandboxResult:
        """记录调用并返回脚本化结果（不执行真实进程）。"""
        del timeout_seconds, limits  # 未使用；仅供接口兼容
        self.calls.append((tuple(argv), cwd))
        if self._result is not None:
            return self._result
        return SandboxResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
            killed_reason=None,
            network_isolated=False,
            sandbox_kind="fake",
        )


class ContainerSandbox:
    """容器/微虚拟机后端接缝（未来实现）。

    本版本不提供真实容器实现，调用 :meth:`run` 抛出 ``NotImplementedError``，
    避免出现“看起来支持容器但实际未隔离”的虚假安全界限。
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        limits: ResourceLimits | None = None,
    ) -> SandboxResult:
        """未来容器后端的占位实现：始终抛出 ``NotImplementedError``。"""
        del argv, cwd, timeout_seconds, limits
        msg = "container backend not implemented; requires Docker/microVM"
        raise NotImplementedError(msg)


class LocalSandbox:
    """受限本地执行环境（受限子进程沙箱）。

    Args:
        workspace_root: per-run 工作区的父目录；每个 run 在此下创建唯一子目录。
        allow_network: 是否允许出站网络。为 ``False`` 且 ``unshare -n`` 真实可用时
            才应用网络命名空间并把 ``network_isolated`` 置 ``True``。
        keep_workspace: 为 ``True`` 时保留 per-run 工作目录（供调试），否则销毁。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        allow_network: bool = False,
        keep_workspace: bool = False,
    ) -> None:
        """构造受限本地沙箱，拒绝以 root 运行。

        Args:
            workspace_root: per-run 工作区父目录。
            allow_network: 是否允许出站网络。
            keep_workspace: 是否保留 per-run 工作目录。
        """
        if os.geteuid() == 0:
            msg = "沙箱拒绝以 root 运行；请切换到非特权用户（§4.1）"
            raise SandboxError(msg)
        self.workspace_root = workspace_root
        self.allow_network = allow_network
        self.keep_workspace = keep_workspace
        self._dir_whitelist = DirectoryWhitelist([workspace_root])
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        limits: ResourceLimits | None = None,
    ) -> SandboxResult:
        """在隔离工作区内执行 ``argv``。

        Raises:
            SandboxError: 以 root 运行等无法安全执行的情况。
            PathNotAllowedError: ``cwd`` 落在 ``workspace_root`` 之外。
        """
        if os.geteuid() == 0:
            msg = "沙箱拒绝以 root 运行（§4.1）"
            raise SandboxError(msg)
        resolved_limits = limits or ResourceLimits()
        # cwd 必须被目录白名单约束在工作区根内（§4.1：禁止挂载宿主敏感目录）。
        resolved_cwd = self._dir_whitelist.resolve(cwd)

        workspace = self.workspace_root / f"ws-{uuid4().hex}"
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            return self._run_in_workspace(
                argv,
                cwd=resolved_cwd,
                workspace=workspace,
                timeout_seconds=timeout_seconds or resolved_limits.wall_timeout_seconds,
                limits=resolved_limits,
            )
        finally:
            if not self.keep_workspace:
                shutil.rmtree(workspace, ignore_errors=True)

    def _run_in_workspace(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        workspace: Path,
        timeout_seconds: float,
        limits: ResourceLimits,
    ) -> SandboxResult:
        argv_list = [str(element) for element in argv]
        prefix, network_isolated = decide_network(argv_list, allow_network=self.allow_network)
        full_argv = [*prefix, *argv_list]

        (workspace / "tmp").mkdir(parents=True, exist_ok=True)
        env = build_scrubbed_env(workspace)

        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - 执行已校验 argv 是本沙箱核心
                full_argv,
                cwd=cwd,
                env=env,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                preexec_fn=_make_preexec(limits),
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            return SandboxResult(
                exit_code=None,
                stdout=_truncate(exc.stdout),
                stderr=_truncate(exc.stderr),
                duration_seconds=duration,
                timed_out=True,
                killed_reason="timeout",
                network_isolated=network_isolated,
                sandbox_kind="local",
            )
        except OSError as exc:
            duration = time.monotonic() - started
            return SandboxResult(
                exit_code=None,
                stdout="",
                stderr=_truncate(f"无法启动命令: {exc}"),
                duration_seconds=duration,
                timed_out=False,
                killed_reason="launch_failed",
                network_isolated=network_isolated,
                sandbox_kind="local",
            )

        duration = time.monotonic() - started
        return SandboxResult(
            exit_code=completed.returncode,
            stdout=_truncate(completed.stdout),
            stderr=_truncate(completed.stderr),
            duration_seconds=duration,
            timed_out=False,
            killed_reason=None,
            network_isolated=network_isolated,
            sandbox_kind="local",
        )


def decide_network(argv: Sequence[str], *, allow_network: bool) -> tuple[list[str], bool]:
    """决定命令前缀与网络隔离标志（诚实：仅当命名空间真实可用才宣称隔离）。

    Returns:
        ``(command_prefix, network_isolated)``：``allow_network`` 为真，或
        ``unshare -n`` 不可用时返回 ``([], False)``；仅当关闭网络且命名空间
        真实可用才返回 ``(["unshare", "-n"], True)``。
    """
    del argv  # 前缀与命令本身无关；保留参数以匹配未来转发语义
    if allow_network:
        return [], False
    if _unshare_net_available():
        return ["unshare", "-n"], True
    return [], False


def _unshare_net_available() -> bool:
    """探测 ``unshare -n`` 是否能为当前用户真实创建网络命名空间（带缓存）。"""
    if "available" in _NET_UNSHARE_CACHE:
        return _NET_UNSHARE_CACHE["available"]
    unshare = shutil.which("unshare")
    if unshare is None:
        _NET_UNSHARE_CACHE["available"] = False
        return False
    try:
        probe = subprocess.run(  # noqa: S603 - unshare 来自 which()，需执行系统二进制
            [unshare, "-n", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        available = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        available = False
    _NET_UNSHARE_CACHE["available"] = available
    return available


def build_scrubbed_env(workspace: Path) -> dict[str, str]:
    """构造净化后的子进程环境：仅白名单变量 + 指向工作区的隔离变量。

    不继承宿主机完整环境（§4.1：不向执行体继承完整环境变量）。
    """
    env: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["HOME"] = str(workspace)
    env["TMPDIR"] = str(workspace / "tmp")
    env["XDG_CACHE_HOME"] = str(workspace / "cache")
    env["XDG_CONFIG_HOME"] = str(workspace / "config")
    env["XDG_DATA_HOME"] = str(workspace / "data")
    env["XDG_STATE_HOME"] = str(workspace / "state")
    return env


def _make_preexec(limits: ResourceLimits) -> Callable[[], None]:
    """返回在子进程 exec 之前施加 rlimits 的 ``preexec_fn`` 回调。"""

    def _apply() -> None:
        apply_rlimits(limits)

    return _apply


def apply_rlimits(limits: ResourceLimits) -> None:
    """在子进程内施加资源限额（soft=hard）。

    除 RLIMIT_NPROC 外全部 fail-closed：设置失败即抛出，避免在未施加限额的
    状态下运行不受控命令。RLIMIT_NPROC 按真实用户 ID 计数，在用户已存在大量
    进程时可能无法设置，故容忍其失败以免阻断所有运行。
    """
    cpu = max(1, limits.cpu_seconds)
    memory_bytes = limits.memory_mb * 1024 * 1024
    fsize_bytes = limits.max_file_size_mb * 1024 * 1024
    nofile = max(1, limits.max_open_files)

    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
    with contextlib.suppress(OSError):
        resource.setrlimit(
            resource.RLIMIT_NPROC,
            (max(1, limits.max_processes), max(1, limits.max_processes)),
        )


def _truncate(text: str | bytes | None) -> str:
    """截断捕获的输出到上限（64 KiB），避免内存被海量输出撑爆。"""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS]
