"""任务状态机与持久化任务表访问层（S2-01 / S2-02 / S2-03）。

实现 reference.md §3 精简状态集与转换语义、F6 的幂等创建 / 有限重试 /
恢复（含 ``UNKNOWN`` 人工处理）与 §12 规则 2/3/4。所有状态转换都在
``BEGIN IMMEDIATE`` 事务内完成，以 SQLite 事务 + UNIQUE 约束保证单机一致性
（§0.3 / §3），不引入分布式租约或发件箱。

- **幂等创建：** ``tasks.idempotency_key`` 唯一约束；重复提交返回既有任务，
  并发竞态经 ``IntegrityError`` 回退到按键查询。
- **有限重试：** ``schedule_retry`` 递增 ``attempt_no``，超过 ``max_attempts``
  或突破 ``max_cost`` 时转为 ``FAILED``（受次数 / 成本上限约束，§12 规则 4）。
- **恢复：** ``recover`` 把超期 ``RUNNING`` 转为 ``UNKNOWN``（不盲目重试），
  把超过 ``deadline_at`` 的非终态任务转为 ``EXPIRED``。
- **错误信息：** 业务数据库只保存 ``error_code`` / ``error_category`` /
  ``safe_error_message``，消息经 :func:`nexusai_lite.logging_setup.redact_text`
  脱敏后写入（§4.3）。
"""

from __future__ import annotations

import enum
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexusai_lite.db import utcnow_iso
from nexusai_lite.logging_setup import redact_text

if TYPE_CHECKING:
    from collections.abc import Iterator


class TaskStatus(enum.StrEnum):
    """任务状态集（reference §3 精简状态集）。"""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


#: 显式允许转换表（reference §3）；SUCCEEDED/FAILED/CANCELLED/EXPIRED 为终态。
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.RETRY_WAIT: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.UNKNOWN: frozenset(
        {TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.SUCCEEDED}
    ),  # 仅经 resolve_unknown 人工处理
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.EXPIRED: frozenset(),
}

#: 终态：不可再转换。
TERMINAL: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}
)

#: 超过 deadline_at 允许转为 EXPIRED 的非终态（PENDING 仅在入队前被取消）。
_NON_TERMINAL_EXPIRABLE: frozenset[TaskStatus] = frozenset(
    {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY_WAIT, TaskStatus.PAUSED}
)

#: 取消时写入错误语义（§4.3 / F6：取消要记录残余外部执行风险）。
_CANCELLED_CODE = "CANCELLED"
_CANCELLED_CATEGORY = "cancelled"

#: mark_unknown / 预算突破使用的错误分类。
_UNKNOWN_CATEGORY = "unknown_error"


@dataclass(frozen=True, slots=True)
class Task:
    """逻辑任务对象（对应 ``tasks`` 表的一行，含 S2-01 新增限制字段）。"""

    task_id: str
    task_type: str | None
    priority: str
    status: TaskStatus
    attempt_no: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    deadline_at: str | None
    estimated_cost: float | None
    actual_cost: float | None
    cost_status: str | None
    error_code: str | None
    error_category: str | None
    safe_error_message: str | None
    raw_error_reference: str | None
    policy_version: str | None
    trace_id: str | None
    idempotency_key: str | None
    max_attempts: int
    max_execution_seconds: int
    max_cost: float | None


class TaskError(Exception):
    """任务服务的基础异常。"""


class TaskNotFoundError(TaskError):
    """任务不存在。"""

    def __init__(self, task_id: str) -> None:  # noqa: D107 - 异常 __init__ 无需 docstring
        self.task_id = task_id
        super().__init__(f"任务不存在: {task_id}")


class InvalidTransitionError(TaskError):
    """非法状态转换（reference §3：状态转换有合法性定义）。"""

    def __init__(  # noqa: D107 - 异常 __init__ 无需 docstring
        self,
        task_id: str,
        from_status: TaskStatus,
        to_status: TaskStatus,
    ) -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"非法状态转换: {from_status.value} -> {to_status.value} "
            f"(task_id={task_id})"
        )


def _as_dt(value: str) -> datetime:
    """把 UTC ISO 时间戳解析为时区感知 datetime（比较用，非存储）。"""
    return datetime.fromisoformat(value)


def _older_than(iso: str, seconds: int) -> bool:
    """判断 ``iso`` 时间距今是否超过 ``seconds`` 秒。"""
    return (datetime.now(UTC) - _as_dt(iso)).total_seconds() > seconds


def _past(iso: str, now_iso: str) -> bool:
    """判断 ``iso`` 是否早于 ``now_iso``（均为 UTC ISO 字符串）。"""
    return _as_dt(iso) < _as_dt(now_iso)


def _row_to_task(row: sqlite3.Row) -> Task:
    """把 ``tasks`` 行转换为 :class:`Task`。"""
    return Task(
        task_id=row["task_id"],
        task_type=row["task_type"],
        priority=row["priority"],
        status=TaskStatus(row["status"]),
        attempt_no=row["attempt_no"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        deadline_at=row["deadline_at"],
        estimated_cost=row["estimated_cost"],
        actual_cost=row["actual_cost"],
        cost_status=row["cost_status"],
        error_code=row["error_code"],
        error_category=row["error_category"],
        safe_error_message=row["safe_error_message"],
        raw_error_reference=row["raw_error_reference"],
        policy_version=row["policy_version"],
        trace_id=row["trace_id"],
        idempotency_key=row["idempotency_key"],
        max_attempts=row["max_attempts"],
        max_execution_seconds=row["max_execution_seconds"],
        max_cost=row["max_cost"],
    )


class TaskService:
    """任务状态机与持久化访问层（S2-01 / S2-02 / S2-03）。

    所有状态转换在 ``BEGIN IMMEDIATE`` 事务内执行；幂等创建依赖
    ``tasks.idempotency_key`` 的 UNIQUE 约束处理并发竞态。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:  # noqa: D107 - 注入连接
        self.conn = conn

    # ------------------------------------------------------------------ 事务

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """开启 ``BEGIN IMMEDIATE`` 事务；异常时回滚，否则提交。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ 读取

    def _select_task(self, task_id: str) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def _require_task(self, task_id: str) -> Task:
        task = self._select_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def _find_by_key(self, idempotency_key: str) -> Task | None:
        """按幂等键查找任务（幂等键非空时才调用）。"""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_task(self, task_id: str) -> Task | None:
        """按 ``task_id`` 返回任务；不存在返回 ``None``。"""
        return self._select_task(task_id)

    def list_tasks(self, status: TaskStatus | str | None = None, limit: int = 100) -> list[Task]:
        """按可选状态过滤列出任务（按创建时间升序）。"""
        sql = "SELECT * FROM tasks"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            status_value = status.value if isinstance(status, TaskStatus) else status
            params.append(status_value)
        sql += " ORDER BY created_at LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_task(row) for row in rows]

    # ------------------------------------------------------------------ 创建

    def create_task(  # noqa: PLR0913 - 创建参数即表字段集合，属领域创建契约
        self,
        *,
        task_type: str,
        idempotency_key: str | None = None,
        priority: str = "normal",
        deadline_at: str | None = None,
        estimated_cost: float | None = None,
        max_attempts: int = 3,
        max_execution_seconds: int = 3600,
        max_cost: float | None = None,
        policy_version: str | None = None,
        trace_id: str | None = None,
    ) -> Task:
        """创建任务（状态 ``PENDING``，``attempt_no=0``）。

        非空 ``idempotency_key`` 重复提交返回既有任务，不创建重复行；并发竞态
        由 UNIQUE 约束捕获（``IntegrityError`` → 按键回查既有任务）。
        """
        now = utcnow_iso()
        if idempotency_key is not None:
            existing = self._find_by_key(idempotency_key)
            if existing is not None:
                return existing
        task_id = uuid.uuid4().hex
        try:
            with self._tx():
                self.conn.execute(
                    "INSERT INTO tasks (task_id, idempotency_key, task_type, priority, status, "
                    "attempt_no, created_at, deadline_at, estimated_cost, max_attempts, "
                    "max_execution_seconds, max_cost, policy_version, trace_id) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        idempotency_key,
                        task_type,
                        priority,
                        TaskStatus.PENDING.value,
                        now,
                        deadline_at,
                        estimated_cost,
                        max_attempts,
                        max_execution_seconds,
                        max_cost,
                        policy_version,
                        trace_id,
                    ),
                )
        except sqlite3.IntegrityError:
            # 并发竞态：另一连接在预检后提交了同一幂等键。
            if idempotency_key is not None:
                existing = self._find_by_key(idempotency_key)
                if existing is not None:
                    return existing
            raise
        return Task(
            task_id=task_id,
            task_type=task_type,
            priority=priority,
            status=TaskStatus.PENDING,
            attempt_no=0,
            created_at=now,
            started_at=None,
            finished_at=None,
            deadline_at=deadline_at,
            estimated_cost=estimated_cost,
            actual_cost=None,
            cost_status=None,
            error_code=None,
            error_category=None,
            safe_error_message=None,
            raw_error_reference=None,
            policy_version=policy_version,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            max_execution_seconds=max_execution_seconds,
            max_cost=max_cost,
        )

    # ------------------------------------------------------------------ 状态转换

    def _validate_transition(
        self, from_status: TaskStatus, to_status: TaskStatus, task_id: str
    ) -> None:
        allowed = TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise InvalidTransitionError(task_id, from_status, to_status)

    def queue(self, task_id: str) -> Task:
        """进入可执行队列（PENDING / RETRY_WAIT / PAUSED -> QUEUED）。"""
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.QUEUED, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (TaskStatus.QUEUED.value, task_id),
            )
            return replace(current, status=TaskStatus.QUEUED)

    def unpause(self, task_id: str) -> Task:
        """从暂停恢复（PAUSED -> QUEUED）。"""
        return self.queue(task_id)

    def start(self, task_id: str) -> Task:
        """开始执行（QUEUED -> RUNNING），记录 ``started_at``。"""
        now = utcnow_iso()
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.RUNNING, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, started_at = ? WHERE task_id = ?",
                (TaskStatus.RUNNING.value, now, task_id),
            )
            return replace(current, status=TaskStatus.RUNNING, started_at=now)

    def succeed(
        self,
        task_id: str,
        *,
        actual_cost: float | None = None,
        cost_status: str | None = None,
    ) -> Task:
        """任务成功（RUNNING -> SUCCEEDED），记录 ``finished_at`` 与成本。"""
        now = utcnow_iso()
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.SUCCEEDED, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ?, actual_cost = ?, cost_status = ? "
                "WHERE task_id = ?",
                (TaskStatus.SUCCEEDED.value, now, actual_cost, cost_status, task_id),
            )
            return replace(
                current,
                status=TaskStatus.SUCCEEDED,
                finished_at=now,
                actual_cost=actual_cost,
                cost_status=cost_status,
            )

    def fail(  # noqa: PLR0913 - 错误与成本记录字段即表字段集合，属领域契约
        self,
        task_id: str,
        *,
        error_code: str | None = None,
        error_category: str | None = None,
        safe_error_message: str | None = None,
        actual_cost: float | None = None,
        cost_status: str | None = None,
    ) -> Task:
        """确认失败（RUNNING -> FAILED），记录错误字段与成本。"""
        now = utcnow_iso()
        safe = redact_text(safe_error_message) if safe_error_message is not None else None
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.FAILED, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ?, error_code = ?, "
                "error_category = ?, safe_error_message = ?, actual_cost = ?, cost_status = ? "
                "WHERE task_id = ?",
                (
                    TaskStatus.FAILED.value,
                    now,
                    error_code,
                    error_category,
                    safe,
                    actual_cost,
                    cost_status,
                    task_id,
                ),
            )
            return replace(
                current,
                status=TaskStatus.FAILED,
                finished_at=now,
                error_code=error_code,
                error_category=error_category,
                safe_error_message=safe,
                actual_cost=actual_cost,
                cost_status=cost_status,
            )

    def _retry_blocked(self, task: Task) -> bool:
        """判断是否应停止重试：达到 ``max_attempts`` 或突破 ``max_cost``。"""
        if task.attempt_no + 1 >= task.max_attempts:
            return True
        if task.max_cost is None:
            return False
        return (task.actual_cost is not None and task.actual_cost > task.max_cost) or (
            task.estimated_cost is not None and task.estimated_cost > task.max_cost
        )

    def schedule_retry(self, task_id: str, retry_reason: str) -> Task:
        """安排有限重试（RUNNING -> RETRY_WAIT），递增 ``attempt_no``。

        若下次尝试超过 ``max_attempts`` 或累计成本突破 ``max_cost``，则转为
        ``FAILED``（次数 / 成本上限约束，§12 规则 4）。重试原因经脱敏写入
        ``safe_error_message`` 以保持可追踪，并应在创建新尝试时经
        :meth:`record_attempt` 落库。
        """
        safe = redact_text(retry_reason)
        now = utcnow_iso()
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.RETRY_WAIT, task_id)
            if self._retry_blocked(current):
                self.conn.execute(
                    "UPDATE tasks SET status = ?, finished_at = ?, error_code = ?, "
                    "error_category = ?, safe_error_message = ? WHERE task_id = ?",
                    (
                        TaskStatus.FAILED.value,
                        now,
                        "MAX_ATTEMPTS_OR_BUDGET",
                        _UNKNOWN_CATEGORY,
                        safe,
                        task_id,
                    ),
                )
                return replace(
                    current,
                    status=TaskStatus.FAILED,
                    finished_at=now,
                    error_code="MAX_ATTEMPTS_OR_BUDGET",
                    error_category=_UNKNOWN_CATEGORY,
                    safe_error_message=safe,
                )
            next_attempt = current.attempt_no + 1
            self.conn.execute(
                "UPDATE tasks SET status = ?, attempt_no = ?, safe_error_message = ? "
                "WHERE task_id = ?",
                (TaskStatus.RETRY_WAIT.value, next_attempt, safe, task_id),
            )
            return replace(
                current,
                status=TaskStatus.RETRY_WAIT,
                attempt_no=next_attempt,
                safe_error_message=safe,
            )

    def pause(self, task_id: str) -> Task:
        """暂停（QUEUED / RUNNING -> PAUSED）。"""
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.PAUSED, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (TaskStatus.PAUSED.value, task_id),
            )
            return replace(current, status=TaskStatus.PAUSED)

    def cancel(self, task_id: str, reason: str | None = None) -> Task:
        """取消任务（非终态 -> CANCELLED），记录取消时间并标记残余外部执行风险。

        取消原因与残余外部执行提示经脱敏写入 ``safe_error_message``，
        ``error_code``/``error_category`` 分别记为 ``CANCELLED``/``cancelled``
        （F6：任务取消记录取消时间与可能的残余外部执行）。
        """
        now = utcnow_iso()
        note = f"任务已取消；外部副作用可能已发生。原因: {reason or ''}".strip()
        safe = redact_text(note)
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.CANCELLED, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ?, error_code = ?, "
                "error_category = ?, safe_error_message = ? WHERE task_id = ?",
                (
                    TaskStatus.CANCELLED.value,
                    now,
                    _CANCELLED_CODE,
                    _CANCELLED_CATEGORY,
                    safe,
                    task_id,
                ),
            )
            return replace(
                current,
                status=TaskStatus.CANCELLED,
                finished_at=now,
                error_code=_CANCELLED_CODE,
                error_category=_CANCELLED_CATEGORY,
                safe_error_message=safe,
            )

    def expire(self, task_id: str) -> Task:
        """任务超过有效期 / 截止时间（非终态 -> EXPIRED）。"""
        now = utcnow_iso()
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.EXPIRED, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ? WHERE task_id = ?",
                (TaskStatus.EXPIRED.value, now, task_id),
            )
            return replace(current, status=TaskStatus.EXPIRED, finished_at=now)

    def mark_unknown(self, task_id: str, reason: str | None = None) -> Task:
        """执行结果不确定（RUNNING -> UNKNOWN），交由人工核查（§12 规则 3）。"""
        safe = redact_text(reason) if reason else None
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.UNKNOWN, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, error_category = ?, safe_error_message = ? "
                "WHERE task_id = ?",
                (TaskStatus.UNKNOWN.value, _UNKNOWN_CATEGORY, safe, task_id),
            )
            return replace(
                current,
                status=TaskStatus.UNKNOWN,
                error_category=_UNKNOWN_CATEGORY,
                safe_error_message=safe,
            )

    def resolve_unknown(
        self, task_id: str, outcome: TaskStatus | str
    ) -> Task:
        """人工处置 ``UNKNOWN`` 任务（仅可转为 QUEUED / FAILED / SUCCEEDED）。

        ``outcome`` 可传 :class:`TaskStatus` 或对应的字符串值；非法目标抛出
        :class:`InvalidTransitionError`。
        """
        target = outcome if isinstance(outcome, TaskStatus) else TaskStatus(outcome)
        now = utcnow_iso()
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, target, task_id)
            if target == TaskStatus.SUCCEEDED:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, finished_at = ? WHERE task_id = ?",
                    (target.value, now, task_id),
                )
                return replace(current, status=target, finished_at=now)
            self.conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (target.value, task_id),
            )
            return replace(current, status=target)

    # ------------------------------------------------------------------ 执行记录

    def start_run(self, task_id: str) -> str:
        """开始一次执行（QUEUED -> RUNNING），写入 ``task_runs`` 行，返回 execution_id。"""
        execution_id = uuid.uuid4().hex
        now = utcnow_iso()
        with self._tx():
            current = self._require_task(task_id)
            self._validate_transition(current.status, TaskStatus.RUNNING, task_id)
            self.conn.execute(
                "UPDATE tasks SET status = ?, started_at = ? WHERE task_id = ?",
                (TaskStatus.RUNNING.value, now, task_id),
            )
            self.conn.execute(
                "INSERT INTO task_runs (execution_id, task_id, started_at, status) "
                "VALUES (?, ?, ?, ?)",
                (execution_id, task_id, now, "running"),
            )
        return execution_id

    def finish_run(
        self,
        execution_id: str,
        *,
        status: str,
        error_code: str | None = None,
        safe_error_message: str | None = None,
    ) -> None:
        """关闭一次执行记录（写入 ``finished_at`` 与结果字段）。"""
        safe = redact_text(safe_error_message) if safe_error_message is not None else None
        with self._tx():
            self.conn.execute(
                "UPDATE task_runs SET finished_at = ?, status = ?, error_code = ?, "
                "safe_error_message = ? WHERE execution_id = ?",
                (utcnow_iso(), status, error_code, safe, execution_id),
            )

    def record_attempt(  # noqa: PLR0913 - 尝试记录字段即表字段集合，属领域契约
        self,
        *,
        execution_id: str,
        task_id: str,
        attempt_no: int,
        started_at: str,
        finished_at: str | None = None,
        error_category: str | None = None,
        retry_reason: str | None = None,
        may_be_billed: bool = False,
        cost_estimate: float | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        """记录一次尝试（写入 ``task_attempts`` 行），返回 ``attempt_id``。"""
        attempt_id = uuid.uuid4().hex
        safe_reason = redact_text(retry_reason) if retry_reason is not None else None
        with self._tx():
            self.conn.execute(
                "INSERT INTO task_attempts (attempt_id, execution_id, task_id, attempt_no, "
                "started_at, finished_at, error_category, retry_reason, may_be_billed, "
                "cost_estimate, request_id, trace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    execution_id,
                    task_id,
                    attempt_no,
                    started_at,
                    finished_at,
                    error_category,
                    safe_reason,
                    1 if may_be_billed else 0,
                    cost_estimate,
                    request_id,
                    trace_id,
                ),
            )
        return attempt_id

    # ------------------------------------------------------------------ 恢复

    def recover(self, stale_after_seconds: int | None = None) -> list[str]:
        """保守崩溃恢复（§3 / §12 规则 3），返回受影响任务 ID。

        - ``RUNNING`` 超过执行窗口（未显式给定时取 ``max_execution_seconds``）
          → 转 ``UNKNOWN``（不确定是否已产生副作用，不盲目重试）。
        - 其余非终态超期（超过 ``deadline_at``）→ 转 ``EXPIRED``。
        """
        affected: list[str] = []
        now = utcnow_iso()
        for task in self.list_tasks(limit=100000):
            if task.status == TaskStatus.RUNNING:
                window = (
                    stale_after_seconds
                    if stale_after_seconds is not None
                    else task.max_execution_seconds
                )
                if task.started_at is not None and _older_than(task.started_at, window):
                    self.mark_unknown(
                        task.task_id, "crash recovery: running 超过执行窗口且未确认结果"
                    )
                    affected.append(task.task_id)
                elif task.deadline_at is not None and _past(task.deadline_at, now):
                    self.expire(task.task_id)
                    affected.append(task.task_id)
            elif (
                task.status in _NON_TERMINAL_EXPIRABLE
                and task.deadline_at is not None
                and _past(task.deadline_at, now)
            ):
                self.expire(task.task_id)
                affected.append(task.task_id)
        return affected

    def expire_overdue(self) -> list[str]:
        """把超过 ``deadline_at`` 的非终态任务转为 ``EXPIRED``，返回受影响 ID。"""
        affected: list[str] = []
        now = utcnow_iso()
        for task in self.list_tasks(limit=100000):
            if (
                task.status in _NON_TERMINAL_EXPIRABLE
                and task.deadline_at is not None
                and _past(task.deadline_at, now)
            ):
                self.expire(task.task_id)
                affected.append(task.task_id)
        return affected
