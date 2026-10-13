"""S3-02 / S3-05 / S3-06 测试：开发任务执行器守卫管线。

覆盖验收标准：预算预留、命令/白名单校验（沙箱不执行）、沙箱执行、密钥扫描
阻断、墙钟超时按 caps 重试/失败（不盲目重试）、外部副作用不确定进入
``UNKNOWN``、成本结算（实际 < 估算 → commit 存实际）。全部使用临时 SQLite 库
与 :class:`FakeSandbox`，不发网络、不执行真实代码、不触碰任何合并/部署路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

from nexusai_lite import db
from nexusai_lite.alerts import AlertManager
from nexusai_lite.config import activate_config, default_config, propose_config
from nexusai_lite.executor import DevExecutor, DraftOutput
from nexusai_lite.sandbox import FakeSandbox, ResourceLimits, SandboxResult
from nexusai_lite.tasks import Task, TaskService, TaskStatus
from nexusai_lite.whitelist import CommandWhitelist

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Sequence


_PATCH = (
    "diff --git a/readme.md b/readme.md\n"
    "index 0000000..1111111 100644\n"
    "--- a/readme.md\n"
    "+++ b/readme.md\n"
    "@@ -1,1 +1,2 @@\n"
    "+# Hello\n"
    "+a small change\n"
)


def _result(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    killed_reason: str | None = None,
) -> SandboxResult:
    """构造脚本化沙箱结果。"""
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.0,
        timed_out=timed_out,
        killed_reason=killed_reason,
        network_isolated=False,
        sandbox_kind="fake",
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（含 tasks/budget/alerts 迁移）。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


class _Env(NamedTuple):
    """执行器公共构造参数。"""

    workspace: Path
    drafts: Path
    commands: CommandWhitelist


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    """返回执行器公共构造参数（workspace/drafts/白名单）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drafts = tmp_path / "drafts"
    rules = CommandWhitelist({"git", "python", "pytest", "ruff"})
    return _Env(workspace=workspace, drafts=drafts, commands=rules)


def _make_task(  # noqa: PLR0913 - 表字段即任务构造契约
    conn: sqlite3.Connection,
    *,
    task_type: str = "git",
    estimated_cost: float | None = 0.01,
    max_attempts: int = 1,
    max_execution_seconds: int = 30,
    max_cost: float | None = None,
) -> str:
    """创建、入队并返回一个 ``QUEUED`` 任务 ID。"""
    service = TaskService(conn)
    task = service.create_task(
        task_type=task_type,
        estimated_cost=estimated_cost,
        max_attempts=max_attempts,
        max_execution_seconds=max_execution_seconds,
        max_cost=max_cost,
    )
    service.queue(task.task_id)
    return task.task_id


def _executor(  # noqa: PLR0913 - 注入参数即执行器构造契约
    conn: sqlite3.Connection,
    env: _Env,
    sandbox: FakeSandbox,
    *,
    alert_manager: AlertManager | None = None,
    argv_for: Callable[[Task], Sequence[str]] | None = None,
    actual_cost_for: Callable[[Task, SandboxResult], float] | None = None,
    confirm_draft: Callable[[DraftOutput], bool] | None = None,
) -> DevExecutor:
    """用给定沙箱构造执行器。"""
    return DevExecutor(
        conn,
        sandbox=sandbox,
        workspace_root=env.workspace,
        commands=env.commands,
        drafts_dir=env.drafts,
        default_limits=ResourceLimits(),
        alert_manager=alert_manager,
        argv_for=argv_for,
        actual_cost_for=actual_cost_for,
        confirm_draft=confirm_draft,
    )


def _git_argv(task: Task) -> tuple[str, ...]:
    """构造通过白名单的 git 命令。"""
    return ("git", "diff")


def _reservation(conn: sqlite3.Connection) -> dict[str, object] | None:
    """返回任务 scope 上最近一条预留（无则 None）。"""
    row = conn.execute(
        "SELECT * FROM budget_reservations WHERE scope LIKE 'task:%' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


def _alert_codes(conn: sqlite3.Connection) -> set[str]:
    return {str(r["code"]) for r in conn.execute("SELECT code FROM alerts").fetchall()}


# ------------------------------------------------------------------ happy path


def test_happy_path_writes_draft_and_commits_budget(
    conn: sqlite3.Connection, env: _Env, tmp_path: Path
) -> None:
    """成功路径：任务 SUCCEEDED、补丁落盘、白名单被校验、预算 commit、无合并/部署。"""
    task_id = _make_task(conn, estimated_cost=0.01)
    fake = FakeSandbox(_result(stdout=_PATCH))
    ex = _executor(conn, env, fake, argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.SUCCEEDED.value
    assert result.draft is not None
    assert result.draft.patch_path is not None
    assert result.draft.patch_path.exists()
    assert result.draft.branch is None  # 不创建生产分支
    assert result.draft.files_changed == 1
    assert fake.calls == [(("git", "diff"), env.workspace)]

    # 预算预留被 commit（实际成本已确定）。
    reservation = _reservation(conn)
    assert reservation is not None
    assert reservation["status"] == "committed"
    assert reservation["actual_amount"] == pytest.approx(0.01)

    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.SUCCEEDED


# ------------------------------------------------------------------ 白名单违规


def test_whitelist_violation_fails_without_running_sandbox(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """白名单违规 → FAILED(command_not_allowed)，沙箱不被调用。"""
    task_id = _make_task(conn)
    fake = FakeSandbox()
    ex = _executor(conn, env, fake, argv_for=lambda _t: ("rm", "-rf", "/"))

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.FAILED.value
    assert result.sandbox_result is None
    assert result.draft is None
    assert fake.calls == []  # 沙箱未执行
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.error_code == "COMMAND_NOT_ALLOWED"
    assert task.status is TaskStatus.FAILED
    # 预留被释放（未计费）。
    reservation = _reservation(conn)
    assert reservation is not None
    assert reservation["status"] == "released"


# ------------------------------------------------------------------ 预算超限


def test_budget_exceeded_fails_without_running_sandbox_and_alerts(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """预算硬上限 → FAILED(budget_exceeded)，沙箱不执行，触发 CRITICAL 告警。"""
    task_id = _make_task(conn, estimated_cost=5.0, max_cost=1.0)
    fake = FakeSandbox()
    alerts = AlertManager(conn)
    ex = _executor(conn, env, fake, alert_manager=alerts, argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.FAILED.value
    assert result.sandbox_result is None
    assert fake.calls == []  # 沙箱未执行
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.error_code == "BUDGET_EXCEEDED"
    assert "BUDGET_EXCEEDED" in _alert_codes(conn)
    # 无预留被创建（reserve 拒绝即无 reservation，无需 release）。
    assert _reservation(conn) is None


# ------------------------------------------------------------------ 密钥泄露


def test_secret_leak_fails_alerts_and_writes_no_clean_draft(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """产出含密钥 → FAILED(secret_leak)，告警，不持久化“干净”补丁。"""
    leaky_patch = _PATCH + "+sk-deadbeefdeadbeefdeadbeef\n"
    task_id = _make_task(conn)
    fake = FakeSandbox(_result(stdout=leaky_patch))
    alerts = AlertManager(conn)
    ex = _executor(conn, env, fake, alert_manager=alerts, argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.FAILED.value
    assert result.draft is None
    assert result.findings  # 至少一条高危发现
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.error_code == "SECRET_LEAK"
    assert "SECRET_LEAK_DETECTED" in _alert_codes(conn)
    drafts = env.drafts
    assert isinstance(drafts, Path)
    assert (drafts / f"{task_id}.patch").exists() is False


# ------------------------------------------------------------------ 超时


def test_timeout_with_max_attempts_one_fails_without_blind_retry(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """墙钟超时 + max_attempts=1 → FAILED（不盲目重试，沙箱仅调用一次）。"""
    task_id = _make_task(conn, max_attempts=1, max_execution_seconds=30)
    fake = FakeSandbox(_result(timed_out=True, killed_reason="timeout"))
    ex = _executor(conn, env, fake, argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.FAILED.value
    assert result.sandbox_result is not None
    assert result.sandbox_result.timed_out is True
    assert len(fake.calls) == 1  # 仅首次执行，未盲目重试
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.FAILED
    assert task.error_code == "MAX_ATTEMPTS_OR_BUDGET"


def test_timeout_with_attempts_left_goes_retry_wait(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """达到 caps 前超时 → RETRY_WAIT（有限重试受次数约束）。"""
    task_id = _make_task(conn, max_attempts=3, max_execution_seconds=30)
    fake = FakeSandbox(_result(timed_out=True, killed_reason="timeout"))
    ex = _executor(conn, env, fake, argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.RETRY_WAIT.value
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.RETRY_WAIT


# ------------------------------------------------------------------ 外部副作用不确定


def test_external_side_effect_uncertain_goes_unknown(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """补丁创建结果未确认 → UNKNOWN（不静默成功，交人工处理）。"""
    task_id = _make_task(conn)
    fake = FakeSandbox(_result(stdout=_PATCH))
    ex = _executor(conn, env, fake, argv_for=_git_argv, confirm_draft=lambda _d: False)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.UNKNOWN.value
    assert result.draft is not None  # 草案仍可供人工检查
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.UNKNOWN


# ------------------------------------------------------------------ 成本结算


def test_cost_actual_less_than_estimate_commits_actual(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """实际成本（0.001）< 估算（0.01）→ commit 存实际值。"""
    task_id = _make_task(conn, estimated_cost=0.01)
    fake = FakeSandbox(_result(stdout=_PATCH))
    ex = _executor(
        conn,
        env,
        fake,
        argv_for=_git_argv,
        actual_cost_for=lambda _t, _r: 0.001,
    )

    ex.run_task(task_id)

    reservation = _reservation(conn)
    assert reservation is not None
    assert reservation["status"] == "committed"
    assert reservation["actual_amount"] == pytest.approx(0.001)


# ------------------------------------------------------------------ 平台（每日）预算硬上限


def test_platform_daily_hard_limit_blocks_task(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """平台（每日）预算硬上限已满 → 任务被拒（budget_exceeded），沙箱不执行。"""
    payload = default_config().model_dump()
    payload["budget"] = {"daily_hard_limit": 0.5}
    version = propose_config(conn, payload, "设置每日硬上限", human_approval=True)
    activate_config(conn, version)

    task_id = _make_task(conn, estimated_cost=1.0)
    fake = FakeSandbox()
    alerts = AlertManager(conn)
    ex = _executor(conn, env, fake, alert_manager=alerts, argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.FAILED.value
    assert fake.calls == []  # 平台上限在进沙箱前拦截
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.error_code == "BUDGET_EXCEEDED"
    assert "BUDGET_EXCEEDED" in _alert_codes(conn)
    # 平台预留被拒（事务回滚），无任何悬挂预留。
    assert _reservation(conn) is None
    platform = conn.execute(
        "SELECT COUNT(*) AS c FROM budget_reservations WHERE scope='platform'"
    ).fetchone()
    assert platform is not None
    assert platform["c"] == 0


# ------------------------------------------------------------------ 未预期异常安全收尾


class _ExplodingSandbox(FakeSandbox):
    """模拟未预期异常（非受控错误）以验证安全收尾。"""

    def run(  # type: ignore[override] - 测试替身故意抛异常
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        limits: ResourceLimits | None = None,
    ) -> SandboxResult:
        del argv, cwd, timeout_seconds, limits
        msg = "boom: 未预期的 sqlite 锁 / 磁盘写失败"
        raise RuntimeError(msg)


def test_unexpected_exception_releases_reservations_and_leaves_failed(
    conn: sqlite3.Connection, env: _Env
) -> None:
    """执行器未预期异常 → 预留全部释放，任务转到 FAILED（不留 RUNNING）。"""
    task_id = _make_task(conn, estimated_cost=0.01)
    ex = _executor(conn, env, _ExplodingSandbox(), argv_for=_git_argv)

    result = ex.run_task(task_id)

    assert result.status == TaskStatus.FAILED.value
    task = TaskService(conn).get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.FAILED  # 安全终态，非 RUNNING
    assert task.error_code == "EXECUTOR_ERROR"
    assert task.safe_error_message is not None
    assert "boom" in task.safe_error_message

    # 平台与任务两份预留都被释放（未计费），无悬挂预留。
    rows = conn.execute(
        "SELECT scope, status FROM budget_reservations ORDER BY scope"
    ).fetchall()
    assert len(rows) == 2
    by_scope = {r["scope"]: r["status"] for r in rows}
    assert by_scope["platform"] == "released"
    assert by_scope[f"task:{task_id}"] == "released"
