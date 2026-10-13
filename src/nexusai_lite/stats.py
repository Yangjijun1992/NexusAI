"""自身 API 请求记录与统计（S1-04，reference F2 / §1.3 / §6）。

:class:`DbRequestLogger` 把 :class:`records.RequestRecord` 持久化到
``api_request_logs`` 与 ``cost_records``，并使用 ``request_id`` 去重键避免重复
累计。``summary()`` 对自身调用数据做聚合统计（成功/429/5xx/超时/重试/延迟分位数/
Token/成本）并携带数据质量标记。

数据边界（reference.md §1.3）：本模块只统计本工具自身发出的请求（自身观测），
绝不把它与 DeepSeek 全体用户的访问规律混为一谈。样本量不足时不输出误导性稳定
分位数，标记 ``INSUFFICIENT_SAMPLE``（§0.3 原则 4：失败时安全降级）。
"""

# ruff: noqa: PLR2004
# PLR2004: HTTP 状态码与成功区间（200/300/429/500/599）为惯用常量，非魔法数字。

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexusai_lite import db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from nexusai_lite.records import AttemptRecord, RequestRecord

#: 参与成本汇总的成本状态：已估算/已核算/已对账；其余（如 unknown）不计入求和。
_SUMMABLE_COST_STATUSES = frozenset({"estimated", "actual", "reconciled"})


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """按最近秩（nearest-rank）计算一个有序样本的分位数。

    对 ``sorted(values)``，第 ``p`` 百分位对应的秩为 ``ceil(p/100 * N)``，
    返回该位置的样本值。``percentile`` 取 ``(0, 100]``；样本非空才调用。

    Args:
        values: 样本（未排序亦可，内部会排序）。
        percentile: 百分位（0 到 100，含 100）。

    Returns:
        对应最近秩位置的值。
    """
    sorted_values = sorted(values)
    rank = math.ceil(percentile / 100.0 * len(sorted_values))
    return sorted_values[max(rank - 1, 0)]


@dataclass(frozen=True)
class SummaryReport:
    """一次 ``summary()`` 的聚合结果，可序列化为 JSON。

    Attributes:
        total: 时间窗口内请求总数。
        success/failure: 成功（200<=status<300）与失败数量。
        success_rate: 成功率；无样本时为 None（保守，不默认零压力）。
        rate_429/rate_5xx/timeout_rate/retry_rate: 各异常比率，无样本为 None。
        latency_quality: "ok" 或 "INSUFFICIENT_SAMPLE"。
        latency_p50/p95/p99: 延迟分位数（毫秒）；样本不足时为 None。
        input_tokens/output_tokens: Token 求和（缺失按 0）。
        estimated_cost: 可求和成本状态下的估算成本总和。
        cost_unknown_count: 成本状态为 unknown 的请求数。
        error_counts: 按 error_category 字符串计数的分布。
        since/until: 时间窗口下界/上界（ISO UTC 字符串或 None）。
        groups: ``group_by`` 分组统计（空 dict 表示未分组）。
    """

    total: int
    success: int
    failure: int
    success_rate: float | None
    rate_429: float | None
    rate_5xx: float | None
    timeout_rate: float | None
    retry_rate: float | None
    latency_quality: str
    latency_p50: float | None
    latency_p95: float | None
    latency_p99: float | None
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    cost_unknown_count: int
    error_counts: dict[str, int]
    groups: dict[str, dict[str, object]]
    since: str | None
    until: str | None

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化的 snake_case 字典。"""
        return {
            "total": self.total,
            "window": {"since": self.since, "until": self.until},
            "quality": {"latency": self.latency_quality},
            "success": self.success,
            "failure": self.failure,
            "success_rate": self.success_rate,
            "rate_429": self.rate_429,
            "rate_5xx": self.rate_5xx,
            "timeout_rate": self.timeout_rate,
            "retry_rate": self.retry_rate,
            "latency_ms": {
                "quality": self.latency_quality,
                "p50": self.latency_p50,
                "p95": self.latency_p95,
                "p99": self.latency_p99,
            },
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
            "cost_unknown_count": self.cost_unknown_count,
            "error_counts": dict(self.error_counts),
            "groups": {key: dict(value) for key, value in self.groups.items()},
        }


class DbRequestLogger:
    """把 :class:`RequestRecord` 持久化到 SQLite 数据库。

    每次调用 ``log_request`` 打开一个短生命周期连接、写入后关闭（在 ``finally``
    中保证关闭）。使用 ``request_id`` 作为 ``api_request_logs`` 的去重键
    （``INSERT OR IGNORE``），重复写入同一记录不产生重复统计。
    """

    def __init__(self, db_file: Path) -> None:
        """构造记录器。

        Args:
            db_file: 数据库文件路径（须已初始化）。
        """
        self._db_file = db_file

    def log_request(self, record: RequestRecord) -> None:
        """持久化一条请求记录与对应的成本记录（幂等）。

        - ``api_request_logs`` 使用 ``INSERT OR IGNORE``（request_id 唯一）。
        - ``cost_records`` 无针对单条请求的显式唯一键，先按 request_id 查询，
          不存在才插入，避免重复累计。

        Args:
            record: 要持久化的请求记录。
        """
        attempts_json = json.dumps(
            {"attempts": [_attempt_to_dict(a) for a in record.attempts]},
            ensure_ascii=False,
        )
        category = record.error_category.value if record.error_category is not None else None
        conn = db.connect(self._db_file)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO api_request_logs "
                "(request_id, task_id, provider, model, started_at, ended_at, "
                " status_code, latency_ms, input_tokens, output_tokens, retry_count, "
                " estimated_cost, cost_status, error_code, error_category, "
                " safe_error_message, raw_error_reference, streaming, trace_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    record.request_id,
                    record.task_id,
                    record.provider,
                    record.model,
                    record.started_at,
                    record.ended_at,
                    record.status_code,
                    record.latency_ms,
                    record.input_tokens,
                    record.output_tokens,
                    record.retry_count,
                    record.estimated_cost,
                    record.cost_status,
                    category,
                    category,
                    record.safe_error_message,
                    record.raw_error_reference,
                    1 if record.streaming else 0,
                    attempts_json,
                ),
            )
            self._insert_cost_record(conn, record)
        finally:
            conn.close()

    def _insert_cost_record(self, conn: sqlite3.Connection, record: RequestRecord) -> None:
        """幂等地写入一条成本记录（`request_id` 唯一索引去重，原子且无竞态）。"""
        conn.execute(
            "INSERT OR IGNORE INTO cost_records "
            "(request_id, attempt_id, provider, model, pricing_version, "
            " input_tokens, output_tokens, estimated_cost, actual_cost, cost_status, "
            " basis, effective_at, source, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                record.request_id,
                None,
                record.provider,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.estimated_cost,
                record.cost_status,
                "api_response_usage",
                record.started_at,
                "platform_estimate",
                db.utcnow_iso(),
            ),
        )


def _attempt_to_dict(attempt: AttemptRecord) -> dict[str, object]:
    """把 AttemptRecord 转为可 JSON 序列化的 dict。"""
    return {
        "attempt_no": attempt.attempt_no,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "status_code": attempt.status_code,
        "error_category": (
            attempt.error_category.value if attempt.error_category is not None else None
        ),
        "retry_reason": attempt.retry_reason,
        "may_be_billed": attempt.may_be_billed,
        "latency_ms": attempt.latency_ms,
    }


def _ratio(count: int, total: int) -> float | None:
    """计算比率；无样本时返回 None（保守）。"""
    if total <= 0:
        return None
    return count / total


def summary(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    group_by: str | None = None,
    min_sample: int = 20,
) -> SummaryReport:
    """汇总 ``api_request_logs`` 的自身调用指标。

    Args:
        conn: 数据库连接。
        since: 下界（ISO UTC 字符串，``started_at >= since``）；省略则不限。
        until: 上界（ISO UTC 字符串，``started_at < until``）；省略则不限。
        group_by: ``"provider"`` / ``"model"`` 时输出分组统计；否则忽略。
        min_sample: 输出延迟分位数的最小样本数；低于则标记质量不足。

    Returns:
        聚合统计结果 :class:`SummaryReport`。
    """
    where: list[str] = []
    params: list[object] = []
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    if until is not None:
        where.append("started_at < ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT provider, model, status_code, error_category, retry_count, "
        " latency_ms, input_tokens, output_tokens, estimated_cost, cost_status "
        "FROM api_request_logs" + clause
    )
    rows = conn.execute(sql, params).fetchall()

    total = len(rows)
    success = sum(1 for r in rows if r["status_code"] is not None and 200 <= r["status_code"] < 300)
    failure = total - success
    rate_429 = _ratio(sum(1 for r in rows if r["status_code"] == 429), total)
    rate_5xx = _ratio(
        sum(1 for r in rows if r["status_code"] is not None and 500 <= r["status_code"] <= 599),
        total,
    )
    timeout_rate = _ratio(
        sum(1 for r in rows if r["error_category"] == "timeout_unknown"), total
    )
    retry_rate = _ratio(sum(1 for r in rows if (r["retry_count"] or 0) > 0), total)

    latencies = [float(r["latency_ms"]) for r in rows if r["latency_ms"] is not None]
    if len(latencies) >= min_sample:
        latency_quality = "ok"
        p50 = nearest_rank_percentile(latencies, 50)
        p95 = nearest_rank_percentile(latencies, 95)
        p99 = nearest_rank_percentile(latencies, 99)
    else:
        latency_quality = "INSUFFICIENT_SAMPLE"
        p50 = p95 = p99 = None

    input_tokens = sum(int(r["input_tokens"]) for r in rows if r["input_tokens"] is not None)
    output_tokens = sum(int(r["output_tokens"]) for r in rows if r["output_tokens"] is not None)

    estimated_cost = sum(
        float(r["estimated_cost"])
        for r in rows
        if r["estimated_cost"] is not None and r["cost_status"] in _SUMMABLE_COST_STATUSES
    )
    cost_unknown_count = sum(1 for r in rows if r["cost_status"] == "unknown")

    error_counts: dict[str, int] = {}
    for r in rows:
        category = r["error_category"]
        if category is not None:
            error_counts[str(category)] = error_counts.get(str(category), 0) + 1

    groups: dict[str, dict[str, object]] = {}
    if group_by in ("provider", "model"):
        for key in sorted({str(r[group_by]) for r in rows if r[group_by] is not None}):
            subset = [r for r in rows if r[group_by] == key]
            g_success = sum(
                1 for r in subset if r["status_code"] is not None and 200 <= r["status_code"] < 300
            )
            g_latencies = [float(r["latency_ms"]) for r in subset if r["latency_ms"] is not None]
            g_errors: dict[str, int] = {}
            for r in subset:
                cat = r["error_category"]
                if cat is not None:
                    g_errors[str(cat)] = g_errors.get(str(cat), 0) + 1
            groups[key] = {
                "count": len(subset),
                "success_rate": _ratio(g_success, len(subset)),
                "rate_429": _ratio(
                    sum(1 for r in subset if r["status_code"] == 429), len(subset)
                ),
                "avg_latency_ms": (
                    (sum(g_latencies) / len(g_latencies)) if g_latencies else None
                ),
                "error_counts": g_errors,
            }

    return SummaryReport(
        total=total,
        success=success,
        failure=failure,
        success_rate=_ratio(success, total),
        rate_429=rate_429,
        rate_5xx=rate_5xx,
        timeout_rate=timeout_rate,
        retry_rate=retry_rate,
        latency_quality=latency_quality,
        latency_p50=p50,
        latency_p95=p95,
        latency_p99=p99,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=estimated_cost,
        cost_unknown_count=cost_unknown_count,
        error_counts=error_counts,
        groups=groups,
        since=since,
        until=until,
    )
