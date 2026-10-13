"""时段规律分析（S1-05，reference F3 / §1.1）。

按 工作日高峰 / 工作日空闲 / 工作日夜 / 周末 / 节假日 分组统计本工具自身调用
数据，作为错峰调度依据。桶归属优先级（从高到低）：

    holiday > weekend > workday_peak > workday_night > workday_offpeak

节假日由可配置的节假日日历标记，与周末同处非工作日窗口。每天数据量不足时标记
``INSUFFICIENT_SAMPLE``；规则缺失时**不得**推断高峰归属（``workday_peak`` 桶
标记 ``NO_RULE`` 且计数为 0，所有工作日数据落到夜/空闲分类）。数据质量不足时
采用保守策略，不得默认判定为低压力（§0.3 原则 4）。

本模块为纯函数/无状态，不写数据库。
"""

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR2004
# C901/PLR0912/PLR0915: period_stats 需按任务规格（S1-05）完成完整的桶归属判定
# 与多桶聚合，复杂度为需求固有。
# PLR0913: period_stats 的 8 参数签名为任务规格冻结的接口。
# PLR2004: HTTP 状态码/星期数（200/300/429/500/599/6）为惯用常量，非魔法数字。

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nexusai_lite.collector.window_rules import WindowRuleData, is_peak

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

#: 输出桶的固定顺序。
_BUCKET_ORDER = ("workday_peak", "workday_offpeak", "workday_night", "weekend", "holiday")

_DEFAULT_TIMEZONE = "Asia/Shanghai"

#: 参与成本汇总的成本状态。
_SUMMABLE_COST_STATUSES = frozenset({"estimated", "actual", "reconciled"})


@dataclass
class _BucketAccumulator:
    """内部：单个时段桶的计数与聚合累加器。"""

    count: int = 0
    success: int = 0
    rate_429: int = 0
    rate_5xx: int = 0
    timeout: int = 0
    latency_sum: float = 0.0
    latency_n: int = 0
    tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class BucketStats:
    """单个时段桶的聚合统计。

    Attributes:
        bucket: 桶名（workday_peak / workday_offpeak / workday_night / weekend / holiday）。
        sample_count: 落入该桶的请求数。
        success_count: 成功数（200<=status<300）。
        success_rate: 成功率；无样本为 None。
        rate_429/rate_5xx/timeout_rate: 各异常比率；无样本为 None。
        avg_latency_ms: 非空延迟均值；无样本为 None。
        total_tokens: Token 总和（缺失按 0）。
        estimated_cost: 可求和成本状态下估算成本总和。
        quality: "ok" / "INSUFFICIENT_SAMPLE" / "NO_RULE"。
    """

    bucket: str
    sample_count: int
    success_count: int
    success_rate: float | None
    rate_429: float | None
    rate_5xx: float | None
    timeout_rate: float | None
    avg_latency_ms: float | None
    total_tokens: int
    estimated_cost: float
    quality: str

    def to_dict(self) -> dict[str, object]:
        """返回 JSON 可序列化的 snake_case 字典。"""
        return {
            "bucket": self.bucket,
            "sample_count": self.sample_count,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "rate_429": self.rate_429,
            "rate_5xx": self.rate_5xx,
            "timeout_rate": self.timeout_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "quality": self.quality,
        }


@dataclass(frozen=True)
class PeriodReport:
    """一次 ``period_stats()`` 的时段规律分析结果。

    Attributes:
        days: 分析窗口天数。
        timezone: 用于桶归属判定的 IANA 时区。
        since/until: 窗口下界/上界（ISO UTC 字符串）。
        sample_count: 窗口内总样本数。
        buckets: 固定顺序的各桶统计。
    """

    days: int
    timezone: str
    since: str
    until: str
    sample_count: int
    buckets: tuple[BucketStats, ...]

    def to_dict(self) -> dict[str, object]:
        """返回 JSON 可序列化的 snake_case 字典。"""
        return {
            "days": self.days,
            "timezone": self.timezone,
            "since": self.since,
            "until": self.until,
            "sample_count": self.sample_count,
            "buckets": [b.to_dict() for b in self.buckets],
        }


def _to_minutes(token: str) -> int:
    """把 ``HH:MM`` 转为自午夜的分钟数。"""
    hour_s, minute_s = token.split(":")
    return int(hour_s) * 60 + int(minute_s)


def _in_night_window(local_time: time, night_start: str, night_end: str) -> bool:
    """判断本地时刻是否落在夜间窗口（正确跨越午夜，如 22:00-07:00）。

    - 正常窗口（start < end）：``start <= cur < end``。
    - 跨午夜窗口（start > end）：``cur >= start or cur < end``。
    - start == end：视为空窗口（不在夜间）。
    """
    start_min = _to_minutes(night_start)
    end_min = _to_minutes(night_end)
    cur = local_time.hour * 60 + local_time.minute
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= cur < end_min
    return cur >= start_min or cur < end_min


def _quality_for(count: int, min_sample: int, *, rule_present: bool) -> str:
    """计算数据质量标记；规则缺失时 peak 桶恒为 NO_RULE。"""
    if not rule_present:
        return "NO_RULE"
    return "ok" if count >= min_sample else "INSUFFICIENT_SAMPLE"


def period_stats(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    rule: WindowRuleData | None = None,
    holidays: Sequence[str] = (),
    night_start: str = "22:00",
    night_end: str = "07:00",
    min_sample: int = 20,
    now_utc: str | None = None,
) -> PeriodReport:
    """按时段桶分组统计自身调用数据。

    Args:
        conn: 数据库连接。
        days: 分析窗口天数（``started_at >= now - days``）。
        rule: 生效的时段规则（含时区与高峰窗口）；为 None 时不推断高峰归属。
        holidays: 节假日日期（``"YYYY-MM-DD"``）。
        night_start: 夜间窗口开始时间（``HH:MM``）。
        night_end: 夜间窗口结束时间（``HH:MM``，可早于开始时间表示跨午夜）。
        min_sample: 数据质量充足的最小样本数。
        now_utc: 参考当前时刻（ISO UTC 字符串）；省略则取系统当前 UTC 时间。

    Returns:
        时段规律分析结果 :class:`PeriodReport`。
    """
    now = datetime.fromisoformat(now_utc) if now_utc else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    since = now - timedelta(days=days)
    since_iso = since.isoformat()
    until_iso = now.isoformat()

    timezone = rule.timezone if rule else _DEFAULT_TIMEZONE
    tz = ZoneInfo(timezone)
    holiday_set = set(holidays)

    rows = conn.execute(
        "SELECT started_at, status_code, error_category, retry_count, "
        " latency_ms, input_tokens, output_tokens, estimated_cost, cost_status "
        "FROM api_request_logs WHERE started_at >= ?",
        (since_iso,),
    ).fetchall()

    # 每个桶的计数与聚合累加器。
    buckets: dict[str, _BucketAccumulator] = {
        name: _BucketAccumulator() for name in _BUCKET_ORDER
    }

    for row in rows:
        started = row["started_at"]
        try:
            dt = datetime.fromisoformat(str(started))
        except ValueError:
            # 无法解析的时间戳不参与桶统计（保守：不作推断）。
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local = dt.astimezone(tz)

        local_date = local.date().isoformat()
        if local_date in holiday_set:
            bucket_name = "holiday"
        elif local.isoweekday() >= 6:
            bucket_name = "weekend"
        elif rule is not None and is_peak(dt, rule) is True:
            bucket_name = "workday_peak"
        elif _in_night_window(local.time(), night_start, night_end):
            bucket_name = "workday_night"
        else:
            bucket_name = "workday_offpeak"

        acc = buckets[bucket_name]
        acc.count += 1
        status_code = row["status_code"]
        if status_code is not None and 200 <= status_code < 300:
            acc.success += 1
        if status_code == 429:
            acc.rate_429 += 1
        if status_code is not None and 500 <= status_code <= 599:
            acc.rate_5xx += 1
        if row["error_category"] == "timeout_unknown":
            acc.timeout += 1
        if row["latency_ms"] is not None:
            acc.latency_sum += float(row["latency_ms"])
            acc.latency_n += 1
        tokens = (int(row["input_tokens"]) if row["input_tokens"] is not None else 0) + (
            int(row["output_tokens"]) if row["output_tokens"] is not None else 0
        )
        acc.tokens += tokens
        if row["estimated_cost"] is not None and row["cost_status"] in _SUMMABLE_COST_STATUSES:
            acc.cost += float(row["estimated_cost"])

    bucket_stats: list[BucketStats] = []
    for name in _BUCKET_ORDER:
        acc = buckets[name]
        count = acc.count
        success_count = acc.success
        rule_present = rule is not None
        # 规则缺失时 peak 桶恒为 NO_RULE 且计数为 0（不得推断高峰归属）。
        if name == "workday_peak" and not rule_present:
            quality = "NO_RULE"
            count = 0
            success_count = 0
        else:
            quality = _quality_for(count, min_sample, rule_present=rule_present)
        bucket_stats.append(
            BucketStats(
                bucket=name,
                sample_count=count,
                success_count=success_count,
                success_rate=(success_count / count) if count else None,
                rate_429=(acc.rate_429 / count) if count else None,
                rate_5xx=(acc.rate_5xx / count) if count else None,
                timeout_rate=(acc.timeout / count) if count else None,
                avg_latency_ms=(acc.latency_sum / acc.latency_n) if acc.latency_n else None,
                total_tokens=acc.tokens,
                estimated_cost=acc.cost,
                quality=quality,
            )
        )

    total_samples = sum(b.sample_count for b in bucket_stats)
    return PeriodReport(
        days=days,
        timezone=timezone,
        since=since_iso,
        until=until_iso,
        sample_count=total_samples,
        buckets=tuple(bucket_stats),
    )
