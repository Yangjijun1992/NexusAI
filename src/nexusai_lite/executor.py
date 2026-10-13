"""开发任务执行器（S3-02 / S3-05 / S3-06）。

在授权窗口内执行已授权的夜间开发任务，落实 reference.md F7、§4.1、§4.4、
§5.1、§12 规则 5/8/11 与任务清单 S3-02/S3-05/S3-06 的验收标准：

- **守卫管线（S3-02）：** 预算预留 → 命令/白名单校验 → 沙箱执行 → 密钥扫描 →
  输出补丁草案。每一步的失败都是安全停止，**绝不**合并/发布/部署。
- **运行时长/资源/成本限制（S3-05）：** 墙钟超时、CPU、内存等资源限额由沙箱的
  rlimits 强制执行（S3-03）；超时后按预算/Attempt 上限进入 ``RETRY_WAIT`` /
  ``FAILED``；成本受预算硬上限约束（§12 规则 9），绝不会被自动突破。
- **分支/补丁草案输出 + 人工检查入口（S3-06）：** 只生成补丁草案到
  ``drafts_dir`` 供人工检查，不创建生产分支、不自动合并、不自动部署
  （§12 规则 8）。外部副作用不确定（如补丁创建客户端未确认）时进入 ``UNKNOWN``
  交由人工处理（§3 / §12 规则 3），绝不盲目重试。任何「自动化成功」都必须有
  可验证的补丁草案（§12 规则 11）。
- **危险操作由外部策略阻止，非仅靠模型：** 命令白名单与目录白名单在进沙箱之前
  强制执行（§12 规则 5/7）；密钥泄露在输出环节被 Secret Scan 阻断并告警
  （F7 / §4.3）。

本模块只依赖标准库与现有 ``nexusai_lite`` 模块，不引入自动合并/部署路径。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexusai_lite.alerts import Severity as AlertSeverity
from nexusai_lite.budget import BudgetExceededError, BudgetGuard, Reservation
from nexusai_lite.config import get_active_config
from nexusai_lite.logging_setup import redact_text
from nexusai_lite.sandbox import ResourceLimits, SandboxResult
from nexusai_lite.secretscan import Finding, SecretLeakError, SecretScanner
from nexusai_lite.tasks import Task, TaskNotFoundError, TaskService, TaskStatus
from nexusai_lite.whitelist import (
    CommandWhitelist,
    DangerousCommandError,
    PathNotAllowedError,
    validate_tool_call,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from nexusai_lite.alerts import AlertManager
    from nexusai_lite.sandbox import Sandbox


@dataclass(frozen=True, slots=True)
class DraftOutput:
    """一次成功执行的补丁草案输出（供人工检查，§12 规则 8/11）。

    Args:
        task_id: 关联任务 ID。
        patch_path: 补丁草案落盘路径；沙箱未产出补丁时为 ``None``。
        branch: 分支名（仅草案/非保护名）；本实现只写补丁，故恒为 ``None``。
        files_changed: 补丁涉及的文件变更数。
        findings_count: 产出中发现的密钥扫描发现数（低/中危，高危会阻断任务）。
        summary: 已脱敏的一段摘要。
    """

    task_id: str
    patch_path: Path | None
    branch: str | None
    files_changed: int
    findings_count: int
    summary: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """一次 ``run_task`` 的受控结果。

    Args:
        task_id: 关联任务 ID。
        status: 执行后任务的最终状态字符串（``SUCCEEDED``/``FAILED``/``UNKNOWN``/
            ``RETRY_WAIT`` 等，对应 :class:`TaskStatus`）。
        sandbox_result: 沙箱执行结果；未进入沙箱的失败为 ``None``。
        findings: 产出中检测到的密钥扫描发现（高危会阻断任务）。
        draft: 成功时生成的补丁草案；失败/未产出时为 ``None``。
        reason: 已脱敏的原因说明。
    """

    task_id: str
    status: str
    sandbox_result: SandboxResult | None
    findings: tuple[Finding, ...]
    draft: DraftOutput | None
    reason: str


@dataclass(frozen=True, slots=True)
class _Settlement:
    """一次执行的成本结算上下文（平台 + 任务预留、是否计费、实际成本）。

    Args:
        platform_reservation: ``platform``（每日硬上限）scoped 预留；超限拒绝或
            前提失败时为 ``None``。
        task_reservation: ``task:<task_id>`` scoped 预留；per-task reserve 拒绝时
            为 ``None``。
        may_be_billed: 是否可能已产生计费执行。
        actual_cost: 结算时的实际成本。
    """

    platform_reservation: Reservation | None
    task_reservation: Reservation | None
    may_be_billed: bool
    actual_cost: float


class DevExecutor:
    """开发任务执行器：把任务按守卫管线推进到安全终态。

    Args:
        conn: 已初始化的 SQLite 连接。
        sandbox: 沙箱后端（实现 :class:`Sandbox`）。
        workspace_root: 经目录白名单授权的工作区根目录。
        commands: 命令白名单（进沙箱前强制校验，§12 规则 5）。
        drafts_dir: 补丁草案落盘目录（人工检查入口）。
        default_limits: 默认资源限额；任务 ``max_execution_seconds`` 会覆盖其
            墙钟超时与 CPU 上限。
        alert_manager: 可选告警管理器；提供时对预算超限/密钥泄露写入告警。
        now_fn: 可注入时钟（返回 aware UTC datetime）；测试用，默认
            ``datetime.now(UTC)``。
        argv_for: 由任务派生命令 argv 的可调用；默认按 ``task_type`` 派生。
        actual_cost_for: 由任务与沙箱结果计算实际成本；默认用任务估算成本。
        confirm_draft: 确认补丁草案已持久化的可调用（返回 ``False`` 时视为
            外部副作用不确定而进入 ``UNKNOWN``）；``None`` 视为已确认。
    """

    def __init__(  # noqa: PLR0913 - 注入参数即守卫管线所需依赖集合，属构造契约
        self,
        conn: sqlite3.Connection,
        *,
        sandbox: Sandbox,
        workspace_root: Path,
        commands: CommandWhitelist,
        drafts_dir: Path,
        default_limits: ResourceLimits | None = None,
        alert_manager: AlertManager | None = None,
        now_fn: Callable[[], datetime] | None = None,
        argv_for: Callable[[Task], Sequence[str]] | None = None,
        actual_cost_for: Callable[[Task, SandboxResult], float] | None = None,
        confirm_draft: Callable[[DraftOutput], bool] | None = None,
    ) -> None:
        """构造执行器；各注入依赖承载守卫管线的能力边界。"""
        self._conn = conn
        self._sandbox = sandbox
        self._workspace_root = workspace_root
        self._commands = commands
        self._drafts_dir = drafts_dir
        self._default_limits = default_limits
        self._alert_manager = alert_manager
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._argv_for = argv_for or _default_argv_for
        self._actual_cost_for = actual_cost_for or _default_actual_cost
        self._confirm_draft = confirm_draft
        self._tasks = TaskService(conn)

    # ------------------------------------------------------------------ 主入口

    def run_task(self, task_id: str) -> ExecutionResult:
        """按守卫管线执行任务，返回受控结果。

        步骤依次为：开始执行 → **平台预算硬上限预留（§12 规则 9，防并发合计超限）**
        → per-task 预算预留 → 命令/白名单校验 → 沙箱执行 → 尝试记录 →
        输出处理（密钥扫描 + 补丁草案）→ 状态收尾 → 成本结算。

        每一步的失败都是安全停止，**绝不**合并/发布/部署；补丁草案是最终产物。
        任何未预期异常都会结算预留并把任务移出 ``RUNNING`` 到安全状态，绝不留
        悬挂预留或卡在 ``RUNNING``。
        """
        try:
            execution_id = self._tasks.start_run(task_id)
        except Exception as exc:  # noqa: BLE001 - 异常均已脱敏后透出为受控结果
            reason = redact_text(str(exc))
            return ExecutionResult(
                task_id=task_id,
                status=_task_status_or_failed(self._tasks, task_id),
                sandbox_result=None,
                findings=(),
                draft=None,
                reason=reason,
            )

        task = self._require_task(task_id)
        settlement = _Settlement(
            platform_reservation=None, task_reservation=None, may_be_billed=False, actual_cost=0.0
        )
        result: SandboxResult | None = None

        try:
            # 2a) 平台（每日）预算硬上限预留：防止并发任务合计突破日上限（§12 规则 9）。
            estimated = task.estimated_cost or 0.0
            guard = BudgetGuard(self._conn)
            guard.from_config(get_active_config(self._conn))  # 从配置播种 platform 上限
            platform_reservation = guard.reserve("platform", estimated, ttl_seconds=None)
            settlement = _Settlement(
                platform_reservation=platform_reservation,
                task_reservation=None,
                may_be_billed=False,
                actual_cost=0.0,
            )

            # 2b) per-task 预算预留（上限来自任务 max_cost）。
            reservation = guard.reserve_for_task(task_id, estimated, task.max_cost)
            settlement = _Settlement(
                platform_reservation=platform_reservation,
                task_reservation=reservation,
                may_be_billed=False,
                actual_cost=0.0,
            )

            # 3) 命令构建 + 白名单校验（危险操作由外部策略阻止，进沙箱前）。
            argv = self._argv_for(task)
            validated = validate_tool_call(
                argv,
                cwd=self._workspace_root,
                roots=[self._workspace_root],
                commands=self._commands,
            )

            # 4) 沙箱执行。
            limits = self._resolve_limits(task)
            result = self._sandbox.run(validated.argv, cwd=validated.cwd, limits=limits)

            # 5) 记录尝试；实际成本在结算时确定。
            settlement = _Settlement(
                platform_reservation=settlement.platform_reservation,
                task_reservation=settlement.task_reservation,
                may_be_billed=_may_be_billed(result),
                actual_cost=self._actual_cost_for(task, result),
            )
            self._record_attempt(task, execution_id, result, settlement)

            # 超时：按预算/Attempt 上限安排重试或失败（外部副作用不确定，不盲目重试）。
            if result.timed_out:
                return self._finalize_timeout(task_id, execution_id, settlement, result)

            # 6) 输出处理：密钥扫描 + 补丁草案。
            return self._finalize_output(task_id, execution_id, settlement, result)
        except (DangerousCommandError, PathNotAllowedError) as exc:
            return self._fail(
                task_id,
                execution_id,
                settlement,
                code="COMMAND_NOT_ALLOWED",
                category="command_not_allowed",
                reason=redact_text(str(exc)),
            )
        except BudgetExceededError as exc:
            return self._fail_budget(task_id, execution_id, settlement, exc)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何未预期异常都安全收尾
            return self._fail_unexpected(task_id, execution_id, settlement, exc)
        finally:
            # 安全网：任何路径若仍有活跃预留（未结算），释放归还容量，绝不悬挂预留。
            self._release_if_active(settlement.platform_reservation)
            self._release_if_active(settlement.task_reservation)

    # ------------------------------------------------------------------ 收尾路径

    def _finalize_timeout(
        self,
        task_id: str,
        execution_id: str,
        settlement: _Settlement,
        result: SandboxResult,
    ) -> ExecutionResult:
        """墙钟超时：结算成本后按 caps 安排重试或失败（S3-05）。"""
        self._finalize_cost(settlement)
        reason = "执行超时（wall timeout），外部副作用不确定"
        self._tasks.finish_run(
            execution_id, status="retry_wait", error_code="TIMEOUT", safe_error_message=reason
        )
        self._tasks.schedule_retry(task_id, reason)  # 受次数/成本 caps 约束
        return self._build_result(task_id, result, (), None, reason)

    def _finalize_output(
        self,
        task_id: str,
        execution_id: str,
        settlement: _Settlement,
        result: SandboxResult,
    ) -> ExecutionResult:
        """输出处理：扫描密钥 → 写补丁草案 → 成功或进入 UNKNOWN。"""
        patch_text = result.stdout
        findings = self._scan_patch(patch_text)
        scope = f"task:{task_id}"
        try:
            SecretScanner().assert_clean(findings, alert_manager=self._alert_manager, scope=scope)
        except SecretLeakError as exc:
            # 密钥泄露：停止、隔离工作区（沙箱已清理）、告警（assert_clean 已发）、
            # 不持久化任何“干净”补丁。
            reason = redact_text(str(exc))
            self._finalize_cost(settlement)
            self._tasks.finish_run(
                execution_id, status="failed", error_code="SECRET_LEAK", safe_error_message=reason
            )
            self._tasks.fail(
                task_id,
                error_code="SECRET_LEAK",
                error_category="secret_leak",
                safe_error_message=reason,
                actual_cost=settlement.actual_cost,
                cost_status="actual",
            )
            return self._build_result(task_id, result, findings, None, reason)

        draft = self._write_draft(task_id, patch_text, len(findings))
        self._finalize_cost(settlement)

        if self._confirm_draft is not None and not self._confirm_draft(draft):
            # 补丁草案创建结果未收到确认：外部副作用不确定 → UNKNOWN（§3 / §12 规则 3）。
            reason = "补丁草案创建结果未确认，外部副作用不确定，进入人工检查"
            self._tasks.finish_run(
                execution_id, status="unknown", error_code=None, safe_error_message=reason
            )
            self._tasks.mark_unknown(task_id, reason)
            return self._build_result(task_id, result, findings, draft, reason)

        # 成功：持久化结果并附草稿供人工检查（§12 规则 11）。
        self._tasks.finish_run(execution_id, status="succeeded")
        self._tasks.succeed(
            task_id, actual_cost=settlement.actual_cost, cost_status="actual"
        )
        reason = "任务成功，补丁草案已生成供人工检查"
        return self._build_result(task_id, result, findings, draft, reason)

    def _fail(  # noqa: PLR0913 - 失败路径错误/成本/执行记录字段即收尾契约
        self,
        task_id: str,
        execution_id: str,
        settlement: _Settlement,
        *,
        code: str,
        category: str,
        reason: str,
    ) -> ExecutionResult:
        """确认失败：结算成本（未计费则释放全部预留）并关闭执行/任务记录。"""
        no_bill = _Settlement(
            platform_reservation=settlement.platform_reservation,
            task_reservation=settlement.task_reservation,
            may_be_billed=False,
            actual_cost=0.0,
        )
        self._finalize_cost(no_bill)
        self._tasks.finish_run(
            execution_id, status="failed", error_code=code, safe_error_message=reason
        )
        self._tasks.fail(
            task_id, error_code=code, error_category=category, safe_error_message=reason
        )
        return self._build_result(task_id, None, (), None, reason)

    def _fail_budget(
        self,
        task_id: str,
        execution_id: str,
        settlement: _Settlement,
        exc: BudgetExceededError,
    ) -> ExecutionResult:
        """预算超限：释放已预留（若平台预留已建而任务预留被拒）并告警。

        平台预留拒绝时无任何预留；任务预留被拒时平台预留已建立，须释放归还容量。
        """
        reason = redact_text(f"预算硬上限已满，任务未执行: {exc}")
        if self._alert_manager is not None:
            self._alert_manager.raise_alert(
                severity=AlertSeverity.CRITICAL,
                code="BUDGET_EXCEEDED",
                scope=f"task:{task_id}",
                title="预算硬上限已满，任务未执行",
                message=reason,
                event_id=task_id,
            )
        # 结算已建立（但未计费）的预留——通常是平台预留，任务预留被拒则无。
        no_bill = _Settlement(
            platform_reservation=settlement.platform_reservation,
            task_reservation=settlement.task_reservation,
            may_be_billed=False,
            actual_cost=0.0,
        )
        self._finalize_cost(no_bill)
        self._tasks.finish_run(
            execution_id, status="failed", error_code="BUDGET_EXCEEDED", safe_error_message=reason
        )
        self._tasks.fail(
            task_id,
            error_code="BUDGET_EXCEEDED",
            error_category="budget_exceeded",
            safe_error_message=reason,
        )
        return self._build_result(task_id, None, (), None, reason)

    def _fail_unexpected(
        self,
        task_id: str,
        execution_id: str,
        settlement: _Settlement,
        exc: Exception,
    ) -> ExecutionResult:
        """兜底：任何未预期异常都结算预留并把任务移到安全终态，绝不遗留 RUNNING。

        外部副作用无法确认时将任务标为 ``FAILED``（脱敏原因），释放/结算预留，并
        关闭执行记录；仅在连任务收尾都失败时保留原始异常。
        """
        reason = redact_text(f"执行器未预期异常，任务已安全停止: {exc}")
        with contextlib.suppress(Exception):  # 结算失败不阻断安全收尾，交给 finally 兜底
            self._finalize_cost(settlement)
        with contextlib.suppress(Exception):  # 执行记录收尾失败不阻断任务安全收尾
            self._tasks.finish_run(
                execution_id,
                status="failed",
                error_code="EXECUTOR_ERROR",
                safe_error_message=reason,
            )
        with contextlib.suppress(Exception):  # 任务若已终态则无需再标 FAILED；绝不留 RUNNING
            self._tasks.fail(
                task_id,
                error_code="EXECUTOR_ERROR",
                error_category="executor_error",
                safe_error_message=reason,
            )
        return self._build_result(task_id, None, (), None, reason)

    def _release_if_active(self, reservation: Reservation | None) -> None:
        """若预留仍为活跃（未结算）则释放归还容量，避免悬挂预留（§12 规则 9）。"""
        if reservation is None:
            return
        try:
            row = self._conn.execute(
                "SELECT status FROM budget_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()
        except Exception:  # noqa: BLE001 - DB 异常时保守跳过，不掩盖已发生的收尾
            return
        if row is not None and row["status"] == "active":
            BudgetGuard(self._conn).release(reservation.reservation_id)

    # ------------------------------------------------------------------ 内部工具

    def _require_task(self, task_id: str) -> Task:
        task = self._tasks.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def _resolve_limits(self, task: Task) -> ResourceLimits:
        """由任务限制与默认限额派生资源限额（墙钟超时/CPU 取任务值，§12 规则 4）。"""
        base = self._default_limits or ResourceLimits()
        wall = float(task.max_execution_seconds)
        return ResourceLimits(
            cpu_seconds=max(1, task.max_execution_seconds),
            memory_mb=base.memory_mb,
            max_processes=base.max_processes,
            max_file_size_mb=base.max_file_size_mb,
            max_open_files=base.max_open_files,
            wall_timeout_seconds=wall,
        )

    def _record_attempt(
        self,
        task: Task,
        execution_id: str,
        result: SandboxResult,
        settlement: _Settlement,
    ) -> None:
        """写入一次尝试记录（request_id=None；成本估算为实际成本）。"""
        now_iso = self._now_fn().isoformat()
        self._tasks.record_attempt(
            execution_id=execution_id,
            task_id=task.task_id,
            attempt_no=task.attempt_no + 1,
            started_at=task.started_at or now_iso,
            finished_at=now_iso,
            error_category=result.killed_reason,
            retry_reason="timed_out" if result.timed_out else None,
            may_be_billed=settlement.may_be_billed,
            cost_estimate=settlement.actual_cost,
            request_id=None,
        )

    def _scan_patch(self, patch_text: str) -> tuple[Finding, ...]:
        """对补丁新增行执行 Secret Scan，返回发现（高危会随后阻断）。"""
        return tuple(SecretScanner().scan_diff(patch_text))

    def _write_draft(self, task_id: str, patch_text: str, findings_count: int) -> DraftOutput:
        """写补丁草案到 ``drafts_dir/<task_id>.patch``；仅草案，不创建生产分支。"""
        self._drafts_dir.mkdir(parents=True, exist_ok=True)
        files_changed = _count_files(patch_text)
        patch_path: Path | None = None
        if patch_text:
            # 落盘前再次脱敏（§4.3），高危密钥已在写前被 Secret Scan 阻断。
            patch_path = self._drafts_dir / f"{task_id}.patch"
            patch_path.write_text(redact_text(patch_text), encoding="utf-8")
        summary = redact_text(
            f"任务 {task_id} 生成了含 {files_changed} 个文件变更的补丁草案供人工检查"
        )
        return DraftOutput(
            task_id=task_id,
            patch_path=patch_path,
            branch=None,  # 仅补丁草案，不创建生产分支（§12 规则 8）
            files_changed=files_changed,
            findings_count=findings_count,
            summary=summary,
        )

    def _finalize_cost(self, settlement: _Settlement) -> None:
        """结算预留：有计费则 ``commit`` 实际成本，否则 ``release`` 归还容量。

        同时结算平台（每日）与任务两个 scope 的预留（§12 规则 9）。
        """
        guard = BudgetGuard(self._conn)
        for reservation in (settlement.platform_reservation, settlement.task_reservation):
            if reservation is None:
                continue
            if settlement.may_be_billed:
                guard.commit(reservation.reservation_id, settlement.actual_cost)
            else:
                guard.release(reservation.reservation_id)

    def _build_result(
        self,
        task_id: str,
        sandbox_result: SandboxResult | None,
        findings: Sequence[Finding],
        draft: DraftOutput | None,
        reason: str,
    ) -> ExecutionResult:
        """构造结果，状态取自数据库中的任务当前状态。"""
        return ExecutionResult(
            task_id=task_id,
            status=_task_status_or_failed(self._tasks, task_id),
            sandbox_result=sandbox_result,
            findings=tuple(findings),
            draft=draft,
            reason=reason,
        )


# ------------------------------------------------------------------ 模块级工具


def _default_argv_for(task: Task) -> Sequence[str]:
    """默认命令派生：以 ``task_type`` 作为命令名（须在白名单内）。

    这是最小、保守的默认；生产调用方应显式注入 ``argv_for``。空任务类型导致空
    命令，进白名单校验时被安全拒绝（非授权即拒绝，§12 规则 7）。
    """
    return (task.task_type or "",)


def _default_actual_cost(task: Task, result: SandboxResult) -> float:
    """默认实际成本：任务已有实际成本则用之，否则用估算成本。"""
    del result  # 默认实现不依赖沙箱结果
    if task.actual_cost is not None:
        return task.actual_cost
    return task.estimated_cost or 0.0


def _may_be_billed(result: SandboxResult) -> bool:
    """命令是否可能已产生计费执行。

    超时（外部副作用不确定）或正常结束都视为可能计费；仅 ``launch_failed``
    （进程未启动）视为未计费。
    """
    return result.killed_reason != "launch_failed"


def _count_files(patch: str) -> int:
    """统计 unified diff 中的文件数（按 ``+++ `` 头计）。"""
    return sum(1 for line in patch.splitlines() if line.startswith("+++ "))


def _task_status_or_failed(tasks: TaskService, task_id: str) -> str:
    """返回任务当前状态字符串；任务不存在时视为失败。"""
    task = tasks.get_task(task_id)
    return task.status.value if task is not None else TaskStatus.FAILED.value
