"""简化熔断 CLOSED/OPEN/HALF_OPEN（S2-05）。

实现 reference F5、§2、§12：对连续 429 / 持续 5xx / 过载进入退避并暂停非紧急任务
（OPEN），冷却期后用有限探测请求恢复（HALF_OPEN），探测成功后关闭熔断（CLOSED），
探测失败则再次打开。状态按 dimension 隔离维度持久化到 ``circuit_breakers`` 表，
使进程重启后不会把 OPEN 静默重置为 CLOSED（reference §4.4）。每次状态转换都会写入
``audit_events``（§4.4 可解释、可追踪）。所有写操作都在 ``BEGIN IMMEDIATE`` 事务内
完成。

隔离维度是字符串键（例如 ``provider:deepseek``、``model:deepseek-chat``、
``project:<id>``），状态按维度各自独立。
"""

from __future__ import annotations

import enum
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable


class BreakerState(str, enum.Enum):  # noqa: UP042 - 持久化要求 str+Enum 便于存取
    """简化熔断状态（reference F5：CLOSED/OPEN/HALF_OPEN）。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """按维度隔离的简化熔断器（持久化到 SQLite）。

    Args:
        conn: 已初始化的 SQLite 连接（须含 004 迁移的 ``circuit_breakers`` 表）。
        failure_threshold: CLOSED 状态下连续失败多少次后打开熔断（OPEN）。
        open_seconds: 熔断打开后等待的冷却秒数，之后进入 HALF_OPEN。
        half_open_max_probes: HALF_OPEN 状态下允许同时进行的探测请求数。
        success_threshold: HALF_OPEN 下连续成功多少次后恢复为 CLOSED。
        now_fn: 可注入的时钟（返回 aware UTC datetime），测试用；默认 ``datetime.now(UTC)``。
    """

    def __init__(  # noqa: PLR0913 - 6 参数为规定构造签名，字段与熔断参数一一对应
        self,
        conn: sqlite3.Connection,
        *,
        failure_threshold: int = 5,
        open_seconds: float = 60.0,
        half_open_max_probes: int = 1,
        success_threshold: int = 2,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """构造熔断器，细节见类 docstring。"""
        self._conn = conn
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._half_open_max_probes = half_open_max_probes
        self._success_threshold = success_threshold
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------- 公共读取

    def state(self, dimension: str) -> BreakerState:
        """返回维度当前状态（自动执行 OPEN→HALF_OPEN 冷却转换并持久化/审计）。"""
        return self._load_resolved(dimension).state

    def allow(self, dimension: str) -> bool:
        """判断维度当前是否允许发起请求。

        - CLOSED：允许。
        - OPEN：拒绝（冷却期后自动进入 HALF_OPEN）。
        - HALF_OPEN：仅当在途探测数 ``< half_open_max_probes`` 时允许该探测，
          并递增在途探测数（不允许大量任务同时试探）。
        """
        row = self._load_resolved(dimension)
        if row.state is BreakerState.HALF_OPEN:
            if row.in_flight_probes >= self._half_open_max_probes:
                return False
            row.in_flight_probes += 1
            row.last_change_at = self._now_iso()
            self._save(dimension, row)
            return True
        return row.state is BreakerState.CLOSED

    # ------------------------------------------------------------- 结果上报

    def record_success(self, dimension: str) -> None:
        """记录维度一次成功。

        在 HALF_OPEN 下视为完成一次探测：释放在途探测；连续成功达到
        ``success_threshold`` 后关闭为 CLOSED（重置计数）并审计。
        """
        row = self._load_resolved(dimension)
        if row.state is BreakerState.HALF_OPEN:
            self._release_probe(row)
            row.success_count += 1
            if row.success_count >= self._success_threshold:
                self._transition(dimension, row, BreakerState.CLOSED, "CircuitBreakerClosed")
                return
            row.last_change_at = self._now_iso()
            self._save(dimension, row)

    def record_failure(self, dimension: str) -> None:
        """记录维度一次失败。

        - CLOSED：递增连续失败数，达到 ``failure_threshold`` 后打开为 OPEN 并审计。
        - HALF_OPEN：视为探测失败，立即重新打开为 OPEN（重置 ``opened_at``）并审计。
        - OPEN：保持打开，不做改动。
        """
        row = self._load_resolved(dimension)
        if row.state is BreakerState.OPEN:
            return
        if row.state is BreakerState.CLOSED:
            row.failure_count += 1
            if row.failure_count >= self._failure_threshold:
                self._open_from(dimension, row)
                return
            row.last_change_at = self._now_iso()
            self._save(dimension, row)
            return
        # HALF_OPEN：探测失败，重新打开。
        self._release_probe(row)
        self._open_from(dimension, row)

    def release_probe(self, dimension: str, *, success: bool) -> None:
        """显式释放一次 HALF_OPEN 探测并按结果上报。

        等价于对该探测调用 :meth:`record_success` 或 :meth:`record_failure`。
        注意：正常流程应在 ``allow`` 取得探测后用本方法（或 record_*）释放一次，
        不要对同一次探测重复释放。
        """
        if success:
            self.record_success(dimension)
        else:
            self.record_failure(dimension)

    def reset(self, dimension: str) -> None:
        """把维度重置为 CLOSED（清空计数与在途探测），并审计。"""
        self._begin()
        try:
            self._conn.execute(
                "DELETE FROM circuit_breakers WHERE dimension=?",
                (dimension,),
            )
            self._audit(
                dimension,
                "CircuitBreakerClosed",
                detail={"state": BreakerState.CLOSED.value, "reason": "manual_reset"},
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------- 内部实现

    def _now(self) -> datetime:
        return self._now_fn()

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _load_resolved(self, dimension: str) -> _BreakerRow:
        """读取维度行并应用可能的 OPEN→HALF_OPEN 自动转换（需要时开事务）。"""
        row = self._load(dimension)
        if row is None:
            return _BreakerRow(BreakerState.CLOSED)
        if row.state is not BreakerState.OPEN or not self._cooldown_elapsed(row.opened_at):
            return row
        # 冷却期已过：OPEN→HALF_OPEN 自动转换（本事务内完成，避免嵌套事务）。
        now_iso = self._now_iso()
        self._begin()
        try:
            # 事务内重新读取，避免并发下的陈旧数据。
            fresh = self._load(dimension)
            if fresh is not None and fresh.state is BreakerState.OPEN and self._cooldown_elapsed(
                fresh.opened_at
            ):
                fresh.state = BreakerState.HALF_OPEN
                fresh.failure_count = 0
                fresh.success_count = 0
                fresh.in_flight_probes = 0
                fresh.opened_at = None
                fresh.last_change_at = now_iso
                self._upsert(dimension, fresh)
                self._audit(
                    dimension,
                    "CircuitBreakerHalfOpened",
                    detail={"state": BreakerState.HALF_OPEN.value, "at": now_iso},
                )
                self._conn.execute("COMMIT")
                return fresh
            self._conn.execute("ROLLBACK")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        # 并发下状态已不再是可转换的 OPEN：重新解析。
        return self._load_resolved(dimension)

    def _cooldown_elapsed(self, opened_at: str | None) -> bool:
        if opened_at is None:
            return False
        opened = datetime.fromisoformat(opened_at)
        return (self._now() - opened).total_seconds() >= self._open_seconds

    def _load(self, dimension: str) -> _BreakerRow | None:
        row = self._conn.execute(
            "SELECT state, failure_count, success_count, in_flight_probes, "
            "       opened_at, last_change_at "
            "FROM circuit_breakers WHERE dimension=?",
            (dimension,),
        ).fetchone()
        if row is None:
            return None
        return _BreakerRow(
            BreakerState(row["state"]),
            failure_count=int(row["failure_count"]),
            success_count=int(row["success_count"]),
            in_flight_probes=int(row["in_flight_probes"]),
            opened_at=row["opened_at"],
            last_change_at=row["last_change_at"],
        )

    def _save(self, dimension: str, row: _BreakerRow) -> None:
        """把维度行写入（upsert），在自身事务内提交。"""
        self._begin()
        try:
            self._upsert(dimension, row)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _upsert(self, dimension: str, row: _BreakerRow) -> None:
        """把维度行写入（upsert）。调用方负责已开启事务。"""
        self._conn.execute(
            "INSERT INTO circuit_breakers "
            "(dimension, state, failure_count, success_count, in_flight_probes, "
            " opened_at, last_change_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dimension) DO UPDATE SET "
            " state=excluded.state, failure_count=excluded.failure_count, "
            " success_count=excluded.success_count, "
            " in_flight_probes=excluded.in_flight_probes, "
            " opened_at=excluded.opened_at, last_change_at=excluded.last_change_at",
            (
                dimension,
                row.state.value,
                row.failure_count,
                row.success_count,
                row.in_flight_probes,
                row.opened_at,
                row.last_change_at,
            ),
        )

    def _open_from(self, dimension: str, row: _BreakerRow) -> None:
        """从 CLOSED/HALF_OPEN 转换到 OPEN（设置 opened_at、重置计数）并审计。"""
        now_iso = self._now_iso()
        self._begin()
        try:
            row.state = BreakerState.OPEN
            row.failure_count = 0
            row.success_count = 0
            row.in_flight_probes = 0
            row.opened_at = now_iso
            row.last_change_at = now_iso
            self._upsert(dimension, row)
            self._audit(
                dimension,
                "CircuitBreakerOpened",
                detail={"state": BreakerState.OPEN.value, "opened_at": now_iso},
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _transition(
        self,
        dimension: str,
        row: _BreakerRow,
        new_state: BreakerState,
        event_type: str,
    ) -> None:
        """通用状态转换（重置计数、清空 opened_at，用于 HALF_OPEN/CLOSED）并审计。"""
        now_iso = self._now_iso()
        self._begin()
        try:
            row.state = new_state
            row.failure_count = 0
            row.success_count = 0
            row.in_flight_probes = 0
            row.opened_at = None
            row.last_change_at = now_iso
            self._upsert(dimension, row)
            self._audit(
                dimension,
                event_type,
                detail={"state": new_state.value, "at": now_iso},
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _release_probe(row: _BreakerRow) -> None:
        """递减在途探测数（保证不小于 0）。"""
        if row.in_flight_probes > 0:
            row.in_flight_probes -= 1

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _audit(
        self,
        dimension: str,
        event_type: str,
        *,
        detail: dict[str, object],
    ) -> None:
        """写入一条熔断审计事件（须在事务内调用）。"""
        self._conn.execute(
            "INSERT INTO audit_events "
            "(event_type, subject, object_type, object_id, at, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_type,
                dimension,
                "circuit_breakers",
                dimension,
                self._now_iso(),
                json.dumps(detail, sort_keys=True),
            ),
        )


class _BreakerRow:
    """熔断维度行的内存表示。"""

    __slots__ = (
        "failure_count",
        "in_flight_probes",
        "last_change_at",
        "opened_at",
        "state",
        "success_count",
    )

    def __init__(  # noqa: PLR0913 - 6 参数为内部状态行的全字段构造
        self,
        state: BreakerState,
        *,
        failure_count: int = 0,
        success_count: int = 0,
        in_flight_probes: int = 0,
        opened_at: str | None = None,
        last_change_at: str | None = None,
    ) -> None:
        """构造一行熔断状态。"""
        self.state = state
        self.failure_count = failure_count
        self.success_count = success_count
        self.in_flight_probes = in_flight_probes
        self.opened_at = opened_at
        self.last_change_at = last_change_at
