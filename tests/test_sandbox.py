"""本地受限沙箱测试（S3-03）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexusai_lite import sandbox as sandbox_module
from nexusai_lite.sandbox import (
    ContainerSandbox,
    FakeSandbox,
    LocalSandbox,
    ResourceLimits,
    SandboxError,
    SandboxResult,
    apply_rlimits,
    decide_network,
)
from nexusai_lite.whitelist import PathNotAllowedError

_PY = sys.executable


def _default_result() -> SandboxResult:
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


# ---------- 环境净化 ----------


def test_env_does_not_scrub_host_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主机独有环境变量不得泄漏给子进程（不继承完整宿主环境）。"""
    monkeypatch.setenv("NEXUSAI_HOST_SECRET", "super-secret")
    sandbox = LocalSandbox(workspace_root=tmp_path)
    result = sandbox.run(
        [_PY, "-c", "import os; print(os.environ.get('NEXUSAI_HOST_SECRET', '<none>'))"],
        cwd=tmp_path,
    )
    assert "<none>" in result.stdout


def test_env_injects_isolated_workspace_vars(tmp_path: Path) -> None:
    """HOME/TMPDIR/XDG_* 应指向隔离工作区。"""
    sandbox = LocalSandbox(workspace_root=tmp_path, keep_workspace=True)
    code = (
        "import os; print(os.environ['HOME']); "
        "print(os.path.basename(os.environ['TMPDIR']))"
    )
    result = sandbox.run([_PY, "-c", code], cwd=tmp_path)
    lines = result.stdout.splitlines()
    assert "ws-" in lines[0]  # HOME 指向 per-run 工作区
    assert lines[1] == "tmp"  # TMPDIR 指向工作区下 tmp 子目录


# ---------- 超时 ----------


def test_timeout_kills_sleeping_command(tmp_path: Path) -> None:
    """超时应终止命令并标记 timed_out。"""
    sandbox = LocalSandbox(workspace_root=tmp_path)
    result = sandbox.run(
        [_PY, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout_seconds=0.2
    )
    assert result.timed_out is True
    assert result.killed_reason == "timeout"
    assert result.exit_code is None


# ---------- 资源限额 ----------


def test_apply_rlimits_sets_all_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_rlimits 应尝试设置全部五类 rlimit。"""
    calls: list[tuple[object, object]] = []

    def fake_setrlimit(which: object, lim: object) -> None:
        calls.append((which, lim))

    monkeypatch.setattr(sandbox_module.resource, "setrlimit", fake_setrlimit)
    limits = ResourceLimits(
        cpu_seconds=30,
        memory_mb=128,
        max_processes=64,
        max_file_size_mb=64,
        max_open_files=256,
    )
    apply_rlimits(limits)

    applied = {which for which, _ in calls}
    assert sandbox_module.resource.RLIMIT_CPU in applied
    assert sandbox_module.resource.RLIMIT_AS in applied
    assert sandbox_module.resource.RLIMIT_NPROC in applied
    assert sandbox_module.resource.RLIMIT_FSIZE in applied
    assert sandbox_module.resource.RLIMIT_NOFILE in applied


def test_memory_rlimit_visible_in_child(tmp_path: Path) -> None:
    """子进程内应观察到已设置的 RLIMIT_AS（地址空间上限）。"""
    memory_mb = 128
    expected = memory_mb * 1024 * 1024
    sandbox = LocalSandbox(workspace_root=tmp_path)
    result = sandbox.run(
        [_PY, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS))"],
        cwd=tmp_path,
        limits=ResourceLimits(memory_mb=memory_mb),
    )
    assert str(expected) in result.stdout
    assert result.exit_code == 0


# ---------- cwd 约束 ----------


def test_cwd_outside_workspace_rejected(tmp_path: Path) -> None:
    """cwd 落在工作区根之外应被拒绝（DirectoryWhitelist 集成）。"""
    sandbox = LocalSandbox(workspace_root=tmp_path)
    with pytest.raises(PathNotAllowedError):
        sandbox.run([_PY, "-c", "pass"], cwd=tmp_path.parent)


# ---------- 工作区销毁 ----------


def test_workspace_destroyed_after_run(tmp_path: Path) -> None:
    """运行结束后 per-run 工作区应被销毁（默认 keep_workspace=False）。"""
    sandbox = LocalSandbox(workspace_root=tmp_path)
    sandbox.run([_PY, "-c", "pass"], cwd=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_workspace_kept_when_requested(tmp_path: Path) -> None:
    """keep_workspace=True 应保留 per-run 工作区。"""
    sandbox = LocalSandbox(workspace_root=tmp_path, keep_workspace=True)
    sandbox.run([_PY, "-c", "pass"], cwd=tmp_path)
    leftovers = [entry for entry in tmp_path.iterdir() if entry.name.startswith("ws-")]
    assert leftovers
    assert all(entry.is_dir() for entry in leftovers)


# ---------- 非 root ----------


def test_constructor_refuses_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """以 root 运行（euid==0）时应拒绝构造。"""
    monkeypatch.setattr(sandbox_module.os, "geteuid", lambda: 0)
    with pytest.raises(SandboxError):
        LocalSandbox(workspace_root=tmp_path)


def test_run_refuses_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """运行中转为 root（euid==0）应拒绝执行。"""
    sandbox = LocalSandbox(workspace_root=tmp_path)
    monkeypatch.setattr(sandbox_module.os, "geteuid", lambda: 0)
    with pytest.raises(SandboxError):
        sandbox.run([_PY, "-c", "pass"], cwd=tmp_path)


# ---------- 网络隔离（诚实） ----------


def test_network_isolated_false_when_namespace_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unshare -n 不可用（探测为 False）时，network_isolated 应如实为 False。"""
    monkeypatch.setattr(sandbox_module, "_unshare_net_available", lambda: False)
    sandbox = LocalSandbox(workspace_root=tmp_path)
    result = sandbox.run([_PY, "-c", "pass"], cwd=tmp_path)
    assert result.network_isolated is False
    assert result.sandbox_kind == "local"


def test_decide_network_applies_unshare_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭网络且 unshare 可用时，应加 unshare -n 前缀并宣称隔离。"""
    monkeypatch.setattr(sandbox_module, "_unshare_net_available", lambda: True)
    prefix, isolated = decide_network(["cmd"], allow_network=False)
    assert prefix == ["unshare", "-n"]
    assert isolated is True


def test_decide_network_honest_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """unshare 不可用时即使关闭网络也不宣称隔离。"""
    monkeypatch.setattr(sandbox_module, "_unshare_net_available", lambda: False)
    prefix, isolated = decide_network(["cmd"], allow_network=False)
    assert prefix == []
    assert isolated is False


def test_decide_network_allow_network_never_isolates(monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_network=True 时即使 unshare 可用也不宣称隔离。"""
    monkeypatch.setattr(sandbox_module, "_unshare_net_available", lambda: True)
    prefix, isolated = decide_network(["cmd"], allow_network=True)
    assert prefix == []
    assert isolated is False


# ---------- argv 非 shell ----------


def test_argv_not_shell_literal_semicolon(tmp_path: Path) -> None:
    """含 ``;`` 的参数应按字面量传递，不被 shell 解释执行。"""
    sandbox = LocalSandbox(workspace_root=tmp_path)
    result = sandbox.run(
        [_PY, "-c", "import sys; print(sys.argv[1])", "hello; rm -rf /"],
        cwd=tmp_path,
    )
    assert result.exit_code == 0
    assert "hello; rm -rf /" in result.stdout


# ---------- 测试替身与容器接缝 ----------


def test_fake_sandbox_records_invocations() -> None:
    """FakeSandbox 应记录调用并返回脚本化结果。"""
    scripted = _default_result()
    fake = FakeSandbox(scripted)
    result = fake.run(["python", "-c", "x"], cwd=Path("/repo"))
    assert result is scripted
    assert fake.calls == [(("python", "-c", "x"), Path("/repo"))]


def test_container_sandbox_not_implemented() -> None:
    """ContainerSandbox.run 应抛出 NotImplementedError（不提供假实现）。"""
    container = ContainerSandbox()
    with pytest.raises(NotImplementedError, match="container backend"):
        container.run(["python"], cwd=Path("/"))
