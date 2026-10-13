"""预算硬上限简化版（S2-07）。

实现预算账户（``budget_accounts``）与预算预留（``budget_reservations``），
阻止自动突破硬上限（reference §12 规则 9）。核心思路：

- 用量 = 已提交成本（来自 ``cost_records`` 的可求和状态）+ 活跃（未过期）预留。
- ``reserve`` 在 ``BEGIN IMMEDIATE`` 事务内原子重算用量并校验，防止并发竞争
  同时通过检查而合计超限（reference §8 阶段 2 退出条件）。
- 账户按 scope 按需创建；限制取 ``None`` 表示未设置，不强制（``check`` 以
  ``limit_set=False`` 标记）。不臆造预算数值。

Scope 为字符串：``platform``、``provider:<name>``、``model:<name>``、
``project:<id>``、``task:<task_id>``。``cost_records`` 仅有 provider/model 列，
因此 committed 仅对 platform/provider/model 生效；project/task 的 committed 为 0，
由其 scope 上的预留账户承担。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from nexusai_lite.db import utcnow_iso

if TYPE_CHECKING:
    import sqlite3

    from nexusai_lite.config import AppConfig, BudgetConfig

#: 计入已提交成本（committed）的 cost_records 状态（与 stats/analysis 一致）。
_SUMMABLE_COST_STATUSES = ("estimated", "actual", "reconciled")

#: 预留状态。
_STATUS_ACTIVE = "active"
_STATUS_COMMITTED = "committed"
_STATUS_RELEASED = "released"
_STATUS_EXPIRED = "expired"

_AUDIT_LIMITS_CHANGED = "BudgetLimitsChanged"
_AUDIT_THRESHOLD_REACHED = "BudgetThresholdReached"

_PLATFORM_SCOPE = "platform"
_TASK_SCOPE_PREFIX = "task:"


class BudgetExceededError(Exception):
    """预留/校验超过硬上限，拒绝新的可计费执行。"""

    def __init__(
        self,
        *,
        scope: str,
        requested: float,
        committed: float,
        reserved: float,
        hard_limit: float,
    ) -> None:
        """记录超限细节与可用额度，供调用方告警。"""
        self.scope = scope
        self.requested = requested
        self.committed = committed
        self.reserved = reserved
        self.hard_limit = hard_limit
        self.available = max(hard_limit - committed - reserved, 0.0)
        super().__init__(
            f"scope {scope!r} 预算硬上限: 已用 {committed:.6f} + 预留 {reserved:.6f} "
            f"+ 请求 {requested:.6f} > 上限 {hard_limit:.6f}"
        )


@dataclass(frozen=True)
class BudgetCheck:
    """一次预算检查的结果（read-only，不写入审计）。"""

    allowed: bool
    remaining: float | None
    limit_set: bool
    would_exceed: bool
    soft_exceeded: bool


@dataclass(frozen=True)
class Reservation:
    """一条预算预留记录。"""

    reservation_id: str
    account_id: str
    scope: str
    amount: float
    status: str
    created_at: str
    expires_at: str | None
    committed_at: str | None
    actual_amount: float | None


def _scope_account_match(scope: str) -> tuple[str | None, tuple[str, ...]]:
    """返回 scope 对应的 ``cost_records`` 匹配子句与参数。

    返回 ``(where_clause, params)``；where_clause 为 ``None`` 表示不匹配任何
    committed 成本（project/task scope 无对应列）。
    """
    if scope == _PLATFORM_SCOPE:
        return "1=1", ()
    if scope.startswith("provider:"):
        return "provider = ?", (scope[len("provider:") :],)
    if scope.startswith("model:"):
        return "model = ?", (scope[len("model:") :],)
    return None, ()


def _committed_cost(conn: sqlite3.Connection, scope: str) -> float:
    """计算 scope 的已提交成本：cost_records 可求和状态下实际/估算成本之和。"""
    clause, params = _scope_account_match(scope)
    if clause is None:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM("
        " CASE WHEN cost_status='estimated' THEN COALESCE(estimated_cost, 0)"
        "      ELSE COALESCE(actual_cost, estimated_cost, 0) END"
        "), 0) AS committed "
        "FROM cost_records WHERE cost_status IN (?, ?, ?) AND "
        + clause,
        (*_SUMMABLE_COST_STATUSES, *params),
    ).fetchone()
    return float(row["committed"]) if row is not None else 0.0


def _reserved_cost(conn: sqlite3.Connection, scope: str, now: str) -> float:
    """计算 scope 的活跃（未过期）预留之和。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS reserved "
        "FROM budget_reservations "
        "WHERE scope=? AND status=? AND (expires_at IS NULL OR expires_at > ?)",
        (scope, _STATUS_ACTIVE, now),
    ).fetchone()
    return float(row["reserved"]) if row is not None else 0.0


def _validate_reserve(
    conn: sqlite3.Connection,
    scope: str,
    amount: float,
    now: str,
    hard_limit: float | None,
) -> tuple[float, float]:
    """在事务内重算 committed+reserved，超硬上限抛 :class:`BudgetExceededError`。

    committed 成本来自 ``cost_records``，reserved 来自 scope 内活跃预留。
    """
    committed = _committed_cost(conn, scope)
    reserved = _reserved_cost(conn, scope, now)
    if hard_limit is not None and committed + reserved + amount > hard_limit:
        raise BudgetExceededError(
            scope=scope,
            requested=amount,
            committed=committed,
            reserved=reserved,
            hard_limit=hard_limit,
        )
    return committed, reserved


class BudgetGuard:
    """预算防护：基于 SQLite 的检查、预留、提交、释放与过期。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """以指定 SQLite 连接构造预算防护（连接由调用方管理与关闭）。"""
        self._conn = conn

    # -- 内部工具 -----------------------------------------------------------

    def _ensure_account(
        self,
        scope: str,
        *,
        soft_limit: float | None = None,
        hard_limit: float | None = None,
        currency: str | None = None,
        now: str | None = None,
    ) -> tuple[str, float | None, float | None, str | None]:
        """创建或更新账户，返回 ``(account_id, soft, hard, currency)``。

        须在事务内调用。用于按需创建账户（reserve）与设置限额（set_limits）。
        """
        now = now or utcnow_iso()
        row = self._conn.execute(
            "SELECT account_id, soft_limit, hard_limit, currency "
            "FROM budget_accounts WHERE scope=?",
            (scope,),
        ).fetchone()
        if row is None:
            account_id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO budget_accounts "
                "(account_id, scope, currency, soft_limit, hard_limit, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (account_id, scope, currency, soft_limit, hard_limit, now, now),
            )
            return account_id, soft_limit, hard_limit, currency
        return (
            str(row["account_id"]),
            row["soft_limit"],
            row["hard_limit"],
            row["currency"],
        )

    def _read_account(
        self, scope: str
    ) -> tuple[str | None, float | None, float | None, str | None]:
        """读取账户（不存在返回全 None），不写入。"""
        row = self._conn.execute(
            "SELECT account_id, soft_limit, hard_limit, currency "
            "FROM budget_accounts WHERE scope=?",
            (scope,),
        ).fetchone()
        if row is None:
            return None, None, None, None
        return (
            str(row["account_id"]),
            row["soft_limit"],
            row["hard_limit"],
            row["currency"],
        )

    def _audit(
        self,
        event_type: str,
        object_id: str,
        *,
        detail: dict[str, object] | None = None,
        now: str | None = None,
    ) -> None:
        """写入一条审计事件（须在事务内或自动提交下调用）。"""
        detail_json = json.dumps(detail, sort_keys=True) if detail else None
        self._conn.execute(
            "INSERT INTO audit_events "
            "(event_type, subject, object_type, object_id, at, detail_json) "
            "VALUES (?, ?, 'budget', ?, ?, ?)",
            (event_type, "budget", object_id, now or utcnow_iso(), detail_json),
        )

    # -- 公开 API -----------------------------------------------------------

    def check(self, scope: str, amount: float) -> BudgetCheck:
        """检查 scope 是否允许新增 amount 用量（read-only）。

        未设置硬上限时 ``limit_set=False``、``allowed=True``、``would_exceed=False``。
        """
        _, soft_limit, hard_limit, _ = self._read_account(scope)
        now = utcnow_iso()
        committed = _committed_cost(self._conn, scope)
        reserved = _reserved_cost(self._conn, scope, now)
        total = committed + reserved + amount
        limit_set = hard_limit is not None
        if not limit_set:
            return BudgetCheck(
                allowed=True,
                remaining=None,
                limit_set=False,
                would_exceed=False,
                soft_exceeded=soft_limit is not None and total > soft_limit,
            )
        would_exceed = total > hard_limit
        return BudgetCheck(
            allowed=not would_exceed,
            remaining=hard_limit - committed - reserved,
            limit_set=True,
            would_exceed=would_exceed,
            soft_exceeded=soft_limit is not None and total > soft_limit,
        )

    def reserve(
        self, scope: str, amount: float, *, ttl_seconds: int | None = None
    ) -> Reservation:
        """原子预留 amount，超过硬上限抛 :class:`BudgetExceededError`。

        在 ``BEGIN IMMEDIATE`` 事务内重算用量并校验，防止并发竞争突破上限。
        一旦越过软阈值写一条 ``BudgetThresholdReached`` 审计。
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            account_id, soft_limit, hard_limit, _ = self._ensure_account(scope)
            now = utcnow_iso()
            committed, reserved = _validate_reserve(
                self._conn, scope, amount, now, hard_limit
            )
            total = committed + reserved + amount
            if soft_limit is not None and total > soft_limit:
                self._audit(
                    _AUDIT_THRESHOLD_REACHED,
                    account_id,
                    detail={"committed": committed, "reserved": reserved, "total": total},
                    now=now,
                )
            reservation_id = str(uuid.uuid4())
            expires_at: str | None = None
            if ttl_seconds is not None:
                expires_at = (
                    datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)
                ).isoformat()
            self._conn.execute(
                "INSERT INTO budget_reservations "
                "(reservation_id, account_id, scope, amount, status, created_at, "
                " expires_at, committed_at, actual_amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (reservation_id, account_id, scope, amount, _STATUS_ACTIVE, now, expires_at),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return Reservation(
            reservation_id=reservation_id,
            account_id=account_id,
            scope=scope,
            amount=amount,
            status=_STATUS_ACTIVE,
            created_at=now,
            expires_at=expires_at,
            committed_at=None,
            actual_amount=None,
        )

    def reserve_for_task(
        self,
        task_id: str,
        amount: float,
        per_task_limit: float | None,
        *,
        ttl_seconds: int = 3600,
    ) -> Reservation:
        """为 ``task:<task_id>`` scope 预留，应用 per-task 硬上限。

        per_task_limit 为调用方（Scheduler）从配置传入的 each-task 上限；为
        ``None`` 时不设置硬上限（仅建立活跃预留，不强制）。账户按需创建。
        预留默认带 TTL，避免执行方崩溃后预留永久占用预算容量。
        """
        scope = f"{_TASK_SCOPE_PREFIX}{task_id}"
        if per_task_limit is not None:
            self.set_limits(scope, hard_limit=per_task_limit)
        return self.reserve(scope, amount, ttl_seconds=ttl_seconds)

    def commit(self, reservation_id: str, actual_amount: float) -> None:
        """把预留标记为 committed，记录实际金额与时间。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow_iso()
            self._conn.execute(
                "UPDATE budget_reservations SET status=?, committed_at=?, actual_amount=? "
                "WHERE reservation_id=?",
                (_STATUS_COMMITTED, now, actual_amount, reservation_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def release(self, reservation_id: str) -> None:
        """释放预留（标记 released，归还容量）。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE budget_reservations SET status=? WHERE reservation_id=?",
                (_STATUS_RELEASED, reservation_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def expire_stale(self) -> int:
        """把已过 ``expires_at`` 且仍为 active 的预留标记为 expired，返回处理数。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow_iso()
            cur = self._conn.execute(
                "UPDATE budget_reservations SET status=? "
                "WHERE status=? AND expires_at IS NOT NULL AND expires_at <= ?",
                (_STATUS_EXPIRED, _STATUS_ACTIVE, now),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount

    def set_limits(
        self,
        scope: str,
        *,
        soft_limit: float | None = None,
        hard_limit: float | None = None,
        currency: str | None = None,
    ) -> None:
        """创建/更新账户限额；任何限额变化都写一条 ``BudgetLimitsChanged`` 审计。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow_iso()
            row = self._conn.execute(
                "SELECT account_id, soft_limit, hard_limit, currency "
                "FROM budget_accounts WHERE scope=?",
                (scope,),
            ).fetchone()
            changed: list[str] = []
            if row is None:
                account_id = str(uuid.uuid4())
                self._conn.execute(
                    "INSERT INTO budget_accounts "
                    "(account_id, scope, currency, soft_limit, hard_limit, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, scope, currency, soft_limit, hard_limit, now, now),
                )
                changed = ["create"]
            else:
                account_id = str(row["account_id"])
                old_soft = row["soft_limit"]
                old_hard = row["hard_limit"]
                old_currency = row["currency"]
                if soft_limit != old_soft or hard_limit != old_hard or currency != old_currency:
                    self._conn.execute(
                        "UPDATE budget_accounts "
                        "SET soft_limit=?, hard_limit=?, currency=?, updated_at=? "
                        "WHERE account_id=?",
                        (soft_limit, hard_limit, currency, now, account_id),
                    )
                    if soft_limit != old_soft:
                        changed.append("soft_limit")
                    if hard_limit != old_hard:
                        changed.append("hard_limit")
                    if currency != old_currency:
                        changed.append("currency")
            if changed:
                self._audit(
                    _AUDIT_LIMITS_CHANGED,
                    account_id,
                    detail={
                        "scope": scope,
                        "soft_limit": soft_limit,
                        "hard_limit": hard_limit,
                        "currency": currency,
                        "changed": changed,
                    },
                    now=now,
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def usage(self, scope: str) -> dict[str, object]:
        """返回 scope 的用量概览：committed/reserved/available/hard_limit/soft_limit/currency。"""
        _, soft_limit, hard_limit, currency = self._read_account(scope)
        now = utcnow_iso()
        committed = _committed_cost(self._conn, scope)
        reserved = _reserved_cost(self._conn, scope, now)
        used = committed + reserved
        available: float | None = None
        if hard_limit is not None:
            available = hard_limit - used
        return {
            "committed": committed,
            "reserved": reserved,
            "available": available,
            "hard_limit": hard_limit,
            "soft_limit": soft_limit,
            "currency": currency,
        }

    def from_config(self, config: AppConfig) -> None:
        """从 :class:`AppConfig` 播种账户：daily 限额 → ``platform`` scope。

        per_task_hard_limit 无固定 scope，由调用方在 reserve 时经
        :meth:`reserve_for_task` 传入。限额为 ``None`` 时表示未设置，不臆造数值。
        """
        budget: BudgetConfig = config.budget
        if (
            budget.daily_hard_limit is None
            and budget.daily_soft_limit is None
            and budget.currency is None
        ):
            return
        self.set_limits(
            _PLATFORM_SCOPE,
            soft_limit=budget.daily_soft_limit,
            hard_limit=budget.daily_hard_limit,
            currency=budget.currency,
        )
