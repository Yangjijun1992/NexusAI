"""基础告警/通知（S2-08）。

实现对 429 限流比率、5xx/过载、熔断打开、预算软/硬阈值、任务 ``UNKNOWN`` 与
密钥认证失败等高风险情况的简化告警（reference §8 阶段 2 退出条件、§9 精简）。
告警经 :func:`redact_text` 脱敏后持久化到 ``alerts`` 表并派发给各
:class:`AlertSink`；通知失败进行有限重试并落库为可观测的 ``dispatch_failed``
状态（§12 规则 4）。告警载荷绝不包含密钥、完整 Prompt 或敏感代码
（§4.3、§12 规则 6）。对无法自动恢复的高风险告警（如任务 ``UNKNOWN``）提供
人工接管提示（§4.4）。
"""

from __future__ import annotations

import enum
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from nexusai_lite.budget import BudgetGuard
from nexusai_lite.logging_setup import PACKAGE_LOGGER_NAME, redact_text
from nexusai_lite.records import ErrorCategory

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence


#: 触发告警所需的最少请求样本量（样本不足不触发，符合保守策略 §0.3 原则 4）。
_MIN_SAMPLES = 5
#: 429 比率告警软/硬阈值。
_RATE_LIMIT_WARNING_RATIO = 0.2
_RATE_LIMIT_CRITICAL_RATIO = 0.5
#: 5xx（含 503/过载）比率告警阈值。
_SERVER_ERROR_RATIO = 0.2

#: 语义化状态码（避免魔法数值）。
_STATUS_RATE_LIMITED = 429
_STATUS_SERVER_ERROR_MIN = 500

#: 文本截断上限（title/message/detail 内长文本）。
_TITLE_MAX = 200
_MESSAGE_MAX = 2000

#: 单条目通知派发与重试次数上限（§12 规则 4 有限重试）。
_SINK_MAX_ATTEMPTS = 3


class Severity(str, enum.Enum):  # noqa: UP042 - 持久化要求 str+Enum 便于存取
    """告警严重级别。"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Alert:
    """一条已构造（且已脱敏）的告警。

    Args:
        alert_id: 告警唯一 ID（UUID4）。
        severity: 严重级别。
        code: 触发条件代码（如 ``RATE_LIMIT_RATIO_HIGH``）。
        scope: 影响范围（如 ``provider:deepseek`` / ``task:<id>``）。
        title: 简短标题（已脱敏）。
        message: 详细说明（已脱敏）。
        event_id: 关联事件/记录 ID（如 request_id、task_id）。
        occurred_at: 告警发生时间（UTC ISO-8601）。
        detail: 结构化细节（已脱敏），可为 ``None``。
    """

    alert_id: str
    severity: Severity
    code: str
    scope: str | None
    title: str
    message: str
    event_id: str | None
    occurred_at: str
    detail: dict[str, object] | None


class AlertSink(Protocol):
    """告警派发目标。实现 ``notify(alert)`` 即视为通告通道。"""

    def notify(self, alert: Alert) -> None:
        """派发一条告警。

        Args:
            alert: 待派发的告警。
        """
        ...


class LogSink:
    """默认告警通道：通过 ``nexusai`` 包级 logger 记录告警。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """以指定 logger 构造（默认 ``nexusai`` 包级 logger）。"""
        self._logger = logger or logging.getLogger(PACKAGE_LOGGER_NAME)

    def notify(self, alert: Alert) -> None:
        """按严重级别映射到日志级别后写出告警。"""
        self._logger.log(
            _severity_log_level(alert.severity),
            "告警 %s [%s] scope=%s event=%s: %s — %s",
            alert.code,
            alert.severity.value,
            alert.scope,
            alert.event_id,
            alert.title,
            alert.message,
        )


class AlertManager:
    """基于 SQLite 的告警管理器：脱敏、去重合并、持久化与多通道派发。

    支持注入时钟（``now_fn``）与多告警通道（``sinks``）。告警先按
    ``f"{code}|{scope}"`` 去重：``dedup_window_seconds`` 窗口内已存在同键告警则
    跳过（返回 ``None``）。通知失败进行有限重试并按需将状态落为
    ``dispatch_failed``。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        sinks: Sequence[AlertSink] = (),
        dedup_window_seconds: int = 300,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """构造告警管理器（连接由调用方管理与关闭）。"""
        self._conn = conn
        self._sinks = tuple(sinks)
        self._dedup_window_seconds = dedup_window_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._logger = logging.getLogger(PACKAGE_LOGGER_NAME)

    # ------------------------------------------------------------- 手动告警

    def raise_alert(  # noqa: PLR0913 - 规定的告警构造签名（脱敏/去重/持久化/派发）
        self,
        *,
        severity: Severity,
        code: str,
        scope: str | None = None,
        title: str,
        message: str,
        event_id: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> Alert | None:
        """构造并派发一条告警；触发去重时返回 ``None``。

        title/message/detail 会经 :func:`redact_text` 脱敏并截断后再存储与派发。
        通知失败有限重试后将状态落为 ``dispatch_failed``（仍返回该告警）。
        """
        title = _truncate(redact_text(title), _TITLE_MAX)
        message = _truncate(redact_text(message), _MESSAGE_MAX)
        detail = _sanitize_detail(detail)
        dedup_key = f"{code}|{scope}"
        occurred_at = self._now_iso()
        if self._recent_alert(dedup_key, occurred_at):
            return None
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            severity=severity,
            code=code,
            scope=scope,
            title=title,
            message=message,
            event_id=event_id,
            occurred_at=occurred_at,
            detail=detail,
        )
        self._persist(alert, dedup_key, "raised")
        if self._dispatch(alert):
            self._update_status(alert.alert_id, "dispatch_failed")
        else:
            self._update_status(alert.alert_id, "dispatched")
        return alert

    # ------------------------------------------------------------- 检测器

    def detect_rate_limits(self, conn: sqlite3.Connection, *, minutes: int = 15) -> Alert | None:
        """扫描 ``api_request_logs`` 的 429 比率；超阈值发 ``RATE_LIMIT_RATIO_HIGH``。"""
        start = (self._now() - timedelta(minutes=minutes)).isoformat()
        total = _count(conn, "COUNT(*) AS n", "started_at >= ?", (start,))
        if total < _MIN_SAMPLES:
            return None
        rate_limited = _count(
            conn,
            "COUNT(*) AS n",
            "started_at >= ? AND status_code = ?",
            (start, _STATUS_RATE_LIMITED),
        )
        ratio = rate_limited / total
        severity = (
            Severity.CRITICAL
            if ratio >= _RATE_LIMIT_CRITICAL_RATIO
            else Severity.WARNING
            if ratio >= _RATE_LIMIT_WARNING_RATIO
            else None
        )
        if severity is None:
            return None
        return self.raise_alert(
            severity=severity,
            code="RATE_LIMIT_RATIO_HIGH",
            title="429 限流比率过高",
            message=(
                f"近 {minutes} 分钟窗口内 429 比率 {ratio:.1%}（样本 {total}，"
                f"阈值 告警 {_RATE_LIMIT_WARNING_RATIO:.0%}/严重 {_RATE_LIMIT_CRITICAL_RATIO:.0%}）"
            ),
        )

    def detect_server_errors(self, conn: sqlite3.Connection, *, minutes: int = 15) -> Alert | None:
        """扫描 ``api_request_logs`` 的 5xx（含 503/过载）比率；超阈值发告警。"""
        start = (self._now() - timedelta(minutes=minutes)).isoformat()
        total = _count(conn, "COUNT(*) AS n", "started_at >= ?", (start,))
        if total < _MIN_SAMPLES:
            return None
        server_errors = _count(
            conn,
            "COUNT(*) AS n",
            "started_at >= ? AND status_code >= ?",
            (start, _STATUS_SERVER_ERROR_MIN),
        )
        ratio = server_errors / total
        if ratio < _SERVER_ERROR_RATIO:
            return None
        return self.raise_alert(
            severity=Severity.WARNING,
            code="SERVER_ERROR_RATE_HIGH",
            title="5xx/过载错误率偏高",
            message=(
                f"近 {minutes} 分钟窗口内 5xx/过载比率 {ratio:.1%}（样本 {total}，"
                f"阈值 {_SERVER_ERROR_RATIO:.0%}）"
            ),
        )

    def detect_open_breakers(self, conn: sqlite3.Connection) -> Alert | None:
        """扫描 ``circuit_breakers`` 中处于 OPEN 的维度；各发 ``CIRCUIT_BREAKER_OPEN``。"""
        raised: list[Alert] = []
        rows = conn.execute(
            "SELECT dimension FROM circuit_breakers WHERE state=?",
            ("open",),
        ).fetchall()
        for row in rows:
            dimension = str(row["dimension"])
            alert = self.raise_alert(
                severity=Severity.CRITICAL,
                code="CIRCUIT_BREAKER_OPEN",
                scope=dimension,
                title="熔断打开，已暂停非紧急任务",
                message=f"熔断维度 {dimension} 处于 OPEN，非紧急任务已暂停以保护重要任务。",
            )
            if alert is not None:
                raised.append(alert)
        return raised[0] if raised else None

    def detect_budget_thresholds(self, conn: sqlite3.Connection) -> Alert | None:
        """扫描账户用量；越过软/硬阈值分别发 ``BUDGET_SOFT_THRESHOLD`` / ``BUDGET_HARD_LIMIT``。"""
        guard = BudgetGuard(conn)
        accounts = conn.execute(
            "SELECT scope FROM budget_accounts "
            "WHERE soft_limit IS NOT NULL OR hard_limit IS NOT NULL"
        ).fetchall()
        raised: list[Alert] = []
        for account in accounts:
            scope = str(account["scope"])
            usage = guard.usage(scope)
            soft_obj = usage["soft_limit"]
            hard_obj = usage["hard_limit"]
            soft = _usage_float(soft_obj)
            hard = _usage_float(hard_obj)
            used = _usage_float(usage["committed"]) + _usage_float(usage["reserved"])
            if hard_obj is not None and used > hard:
                alert = self.raise_alert(
                    severity=Severity.CRITICAL,
                    code="BUDGET_HARD_LIMIT",
                    scope=scope,
                    title="预算硬上限已超限",
                    message=f"scope {scope} 已用 {used:.4f} 超过硬上限 {hard:.4f}，须人工关注。",
                )
            elif soft_obj is not None and used > soft:
                alert = self.raise_alert(
                    severity=Severity.WARNING,
                    code="BUDGET_SOFT_THRESHOLD",
                    scope=scope,
                    title="预算软阈值已越过",
                    message=f"scope {scope} 已用 {used:.4f} 越过软阈值 {soft:.4f}。",
                )
            else:
                alert = None
            if alert is not None:
                raised.append(alert)
        return raised[0] if raised else None

    def detect_unknown_tasks(self, conn: sqlite3.Connection) -> Alert | None:
        """扫描 ``tasks`` 中处于 UNKNOWN 的任务；各发 ``TASK_UNKNOWN`` 并提示人工接管。"""
        raised: list[Alert] = []
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE status=?",
            ("UNKNOWN",),
        ).fetchall()
        for row in rows:
            task_id = str(row["task_id"])
            alert = self.raise_alert(
                severity=Severity.CRITICAL,
                code="TASK_UNKNOWN",
                scope=f"task:{task_id}",
                title="任务进入 UNKNOWN，需人工接管",
                message=(
                    f"任务 {task_id} 处于 UNKNOWN，无法自动恢复；"
                    "请人工核查上次执行的外部副作用后接管处理（对无法自动恢复的"
                    "高风险告警提供人工接管入口）。"
                ),
                event_id=task_id,
            )
            if alert is not None:
                raised.append(alert)
        return raised[0] if raised else None

    def detect_auth_failures(self, conn: sqlite3.Connection, *, minutes: int = 15) -> Alert | None:
        """扫描 ``api_request_logs`` 的认证失败；存在则发 ``AUTHENTICATION_FAILURE``。"""
        start = (self._now() - timedelta(minutes=minutes)).isoformat()
        rows = conn.execute(
            "SELECT request_id, provider FROM api_request_logs "
            "WHERE started_at >= ? AND error_category=? ORDER BY started_at DESC",
            (start, ErrorCategory.AUTHENTICATION_ERROR.value),
        ).fetchall()
        if not rows:
            return None
        latest = rows[0]
        return self.raise_alert(
            severity=Severity.CRITICAL,
            code="AUTHENTICATION_FAILURE",
            scope=str(latest["provider"]) if latest["provider"] else None,
            title="密钥认证失败",
            message=(
                f"近 {minutes} 分钟窗口内检测到 {len(rows)} 次认证失败，"
                "请人工核查并轮换失效凭据。"
            ),
            event_id=str(latest["request_id"]),
        )

    # ------------------------------------------------------------- 聚合扫描

    def scan_and_alert(self, conn: sqlite3.Connection, *, minutes: int = 15) -> list[Alert]:
        """运行全部检测器，返回本次新触发的告警列表（已被去重者不计入）。"""
        return [
            alert
            for alert in (
                self.detect_rate_limits(conn, minutes=minutes),
                self.detect_server_errors(conn, minutes=minutes),
                self.detect_open_breakers(conn),
                self.detect_budget_thresholds(conn),
                self.detect_unknown_tasks(conn),
                self.detect_auth_failures(conn, minutes=minutes),
            )
            if alert is not None
        ]

    # ------------------------------------------------------------- 内部实现

    def _now(self) -> datetime:
        return self._now_fn()

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _recent_alert(self, dedup_key: str, now_iso: str) -> bool:
        """去重窗口内是否存在同键告警（occurred_at 在 ``now_iso - dedup_window`` 内）。"""
        start = (
            datetime.fromisoformat(now_iso) - timedelta(seconds=self._dedup_window_seconds)
        ).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM alerts WHERE dedup_key=? AND occurred_at>=? LIMIT 1",
            (dedup_key, start),
        ).fetchone()
        return row is not None

    def _persist(self, alert: Alert, dedup_key: str, status: str) -> None:
        """写入告警行（事务保护）。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO alerts "
                "(alert_id, severity, code, scope, title, message, event_id, "
                " occurred_at, status, dedup_key, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    alert.alert_id,
                    alert.severity.value,
                    alert.code,
                    alert.scope,
                    alert.title,
                    alert.message,
                    alert.event_id,
                    alert.occurred_at,
                    status,
                    dedup_key,
                    json.dumps(alert.detail, sort_keys=True) if alert.detail else None,
                ),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _update_status(self, alert_id: str, status: str) -> None:
        """更新告警派发状态（事务保护）。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE alerts SET status=? WHERE alert_id=?",
                (status, alert_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _dispatch(self, alert: Alert) -> bool:
        """把告警派发到各通道；任一通道最终失败返回 ``True``（有限重试）。"""
        failed = False
        for sink in self._sinks:
            last_exc: BaseException | None = None
            for _ in range(_SINK_MAX_ATTEMPTS):
                try:
                    sink.notify(alert)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 - sink 为不可信边界，有限重试
                    last_exc = exc
            if last_exc is not None:
                failed = True
                self._logger.error(
                    "告警 %s 派发失败（已重试 %d 次）: %s",
                    alert.alert_id,
                    _SINK_MAX_ATTEMPTS,
                    last_exc,
                )
        return failed


def _truncate(text: str, limit: int) -> str:
    """把文本截断到 ``limit`` 字符（超限加省略号）。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _sanitize_detail(detail: dict[str, object] | None) -> dict[str, object] | None:
    """递归清洗 detail：对每个字符串值做脱敏与截断。"""
    if detail is None:
        return None
    result: dict[str, object] = {}
    for key, value in detail.items():
        if isinstance(value, str):
            result[key] = _truncate(redact_text(value), _MESSAGE_MAX)
        elif isinstance(value, dict):
            result[key] = _sanitize_detail(value)
        else:
            result[key] = value
    return result


def _severity_log_level(severity: Severity) -> int:
    """把告警级别映射为日志级别。"""
    return {
        Severity.INFO: logging.INFO,
        Severity.WARNING: logging.WARNING,
        Severity.CRITICAL: logging.ERROR,
    }[severity]


def _count(
    conn: sqlite3.Connection, expression: str, where: str, params: tuple[object, ...]
) -> int:
    """执行 ``SELECT <expression> FROM api_request_logs WHERE <where>`` 并返回计数。"""
    row = conn.execute(
        f"SELECT {expression} FROM api_request_logs WHERE {where}",  # noqa: S608 - 仅模块常量拼接，where 占位符参数化
        params,
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def _usage_float(value: object) -> float:
    """把 ``BudgetGuard.usage`` 的数值字段转为 float（非数值视为 0）。"""
    return float(value) if isinstance(value, (int, float)) else 0.0
