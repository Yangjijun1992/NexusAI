"""趋势分析与高峰预测（S4-01/S4-02，reference F8 / §8 阶段 4）。

基于**本工具自身历史数据**（``api_request_logs``）做按「工作日×小时」的季节性
高峰预测，供影子模式与人工审核使用。本模块为**纯/无状态**：只做只读聚合与
预测，**绝不写入任何调度状态**（不写 ``tasks`` / ``schedule_decisions``）。

数据边界与安全约束（reference §1.3、F8、§12 规则 10）：
- 预测只描述本工具自身的请求到达率，**不得**声称预测供应商内部负载或全局流量。
- 样本量不足时标记 ``INSUFFICIENT_SAMPLE`` 且 ``value=None``（保守，不默认低压力）。
- 预测是软性评分：**未通过离线回测与影子观察评估前不得控制真实调度**。
- 预测永远不能覆盖安全/熔断/预算硬上限（本模块仅做只读预测，不触碰这些门限）。

预测模型：同「本地星期数×小时」的历史均值；与简单基线（朴素均值）相比，模型
采用 IQR 稳健均值（剔除异常值），在存在异常尖峰时更稳健。两者都基于**训练窗口
内严格早于预测时点**的数据，避免未来数据泄漏。

时间戳统一为 UTC ISO-8601（存储层），聚合时按生效配置的本地时区切成小时桶。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nexusai_lite.config import get_active_config

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

#: 训练窗口天数：预测/回测默认只使用最近这段时间自身观测数据。
_TRAIN_DAYS = 90
#: 回测：全部窗口天数（在训练窗口内做时间序列切分）。
_BACKTEST_DAYS = 60
#: 时间序列切分：前 ``_SPLIT_RATIO`` 为训练，其余为测试（训练严格早于测试）。
_SPLIT_RATIO = 0.7
#: 模型 IQR 稳健均值的离群系数。
_OUTLIER_IQR_K = 1.5
#: 做 IQR 修剪所需的最小样本数；不足则以朴素均值替代。
_MIN_TRIM_SAMPLES = 4
#: 基线（朴素均值）至少需要命中的历史「星期×小时」样本数，否则视为不足。
_MIN_BASELINE_SAMPLES = 2
#: 判定「优于或合理匹配基线」的相对容差（5%）。
_VERDICT_TOLERANCE = 0.05
#: 回测得出的有效测试桶不足该值则标记 ``insufficient_data``。
_MIN_EVAL_BUCKETS = 4

_DATA_QUALITY_OK = "ok"
_DATA_QUALITY_INSUFFICIENT = "INSUFFICIENT_SAMPLE"
_SUPPORTED_METRICS = frozenset({"request_rate"})

_VERDICT_BETTER = "better_or_equal"
_VERDICT_WORSE = "worse"
_VERDICT_INSUFFICIENT = "insufficient_data"


@dataclass(frozen=True)
class ForecastModel:
    """预测模型描述。

    当前实现为「工作日×小时」季节性均值，其中模型对每个季节组做 IQR 稳健平均
    （剔除异常值），与朴素的简单基线（算术均值）区分开，便于在存在异常尖峰时
    获得更低的误差。

    Attributes:
        name: 模型名称（写入 ``Prediction.model`` / ``BacktestReport.model``）。
        outlier_iqr_k: IQR 离群系数（保守设为 1.5）。
    """

    name: str = "seasonal-mean-robust"
    outlier_iqr_k: float = _OUTLIER_IQR_K


@dataclass(frozen=True)
class Prediction:
    """单个小时桶的预测结果（reference S4-01 验收标准：明确窗口/训练范围/样本/置信度/质量）。

    Attributes:
        bucket_start: 预测桶的起始时刻（本地时区 ISO 字符串）。
        horizon_days: 预测跨度（天）。
        metric: 预测的指标（当前为 ``request_rate``）。
        value: 预测值；样本不足时为 None（保守）。
        confidence: 置信度（0..1）；样本不足时为 None。
        sample_count: 参与该桶预测的历史样本数。
        data_quality: ``"ok"`` 或 ``"INSUFFICIENT_SAMPLE"``。
        model: 使用的模型名。
        train_start/train_end: 训练窗口边界（UTC ISO 字符串）。
    """

    bucket_start: str
    horizon_days: int
    metric: str
    value: float | None
    confidence: float | None
    sample_count: int
    data_quality: str
    model: str
    train_start: str
    train_end: str


@dataclass(frozen=True)
class BacktestReport:
    """一次回测（S4-02）的评估报告。

    Attributes:
        model: 被评估的模型名。
        train_start/train_end/test_start/test_end: 时间序列切分的边界（ISO 字符串）。
        metric: 评估指标。
        model_error: 模型的平均绝对误差；不足时为 None。
        baseline_error: 简单基线的平均绝对误差；不足时为 None。
        verdict: ``"better_or_equal"`` / ``"worse"`` / ``"insufficient_data"``。
        sample_count: 参与评估的测试桶数。
    """

    model: str
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    metric: str
    model_error: float | None
    baseline_error: float | None
    verdict: str
    sample_count: int


@dataclass(frozen=True)
class _HourBucket:
    """一个本地小时桶（聚合的最小单元）。"""

    bucket_start: datetime  # aware，本地时区，整点
    value: float

    @property
    def iso(self) -> str:
        """本地时区 ISO 字符串。"""
        return self.bucket_start.isoformat()

    @property
    def date_iso(self) -> str:
        """本地日期 ``YYYY-MM-DD``。"""
        return self.bucket_start.date().isoformat()

    @property
    def weekday_hour(self) -> tuple[int, int]:
        """本地 ISO 星期数（Mon=1）与小时。"""
        return self.bucket_start.isoweekday(), self.bucket_start.hour


def _parse_aware(raw: str | datetime) -> datetime:
    """把（可能 naive 的）时间解析为 aware UTC datetime。"""
    dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _local_bucket(dt: datetime, tz: ZoneInfo) -> datetime:
    """把某时刻截断为其所在小时的开始（本地时区、整点）。"""
    return dt.astimezone(tz).replace(minute=0, second=0, microsecond=0)


def _hourly_series(
    conn: sqlite3.Connection,
    *,
    timezone: str,
    since: datetime,
    until: datetime,
    metric: str,
) -> list[_HourBucket]:
    """把 ``api_request_logs`` 聚合为本地小时桶序列。

    - 只统计本工具自身请求（自身观测）；``metric="request_rate"`` 为每小时请求数。
    - **只收录实际记录过请求的小时桶**：无记录的小时视作「缺失/未知」而非 0
      （保守，不得默认判定为低压力，reference §0.3 原则 4 / §1.3）。
    - 结果按时间升序排列。
    """
    if metric not in _SUPPORTED_METRICS:
        msg = f"不支持的预测指标: {metric!r}（支持 {sorted(_SUPPORTED_METRICS)}）"
        raise ValueError(msg)

    tz = ZoneInfo(timezone)
    since_iso = since.isoformat()
    until_iso = until.isoformat()
    rows = conn.execute(
        "SELECT started_at FROM api_request_logs WHERE started_at >= ? AND started_at < ?",
        (since_iso, until_iso),
    ).fetchall()

    counts: dict[datetime, int] = {}
    for row in rows:
        raw = row["started_at"]
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            # 无法解析的时间戳不参与统计（保守，不做推断）。
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        bucket = _local_bucket(dt, tz)
        counts[bucket] = counts.get(bucket, 0) + 1

    return [
        _HourBucket(bucket_start=bucket, value=float(count))
        for bucket, count in sorted(counts.items())
    ]


def _seasonal_groups(
    series: Sequence[_HourBucket], holidays: set[str]
) -> dict[tuple[int, int], list[float]]:
    """按「星期×小时」分组训练值；节假日桶不参与常规季节性模式（避免污染）。"""
    groups: dict[tuple[int, int], list[float]] = {}
    for bucket in series:
        if bucket.date_iso in holidays:
            # 节假日流量不遵循常规工作日/周末星期模式，剔除以免污染季节组。
            continue
        key = bucket.weekday_hour
        groups.setdefault(key, []).append(bucket.value)
    return groups


def baseline_predict(series: Sequence[tuple[str, float]], target_bucket: str) -> float | None:
    """简单基线：目标桶「星期×小时」的历史算术均值。

    Args:
        series: 历史观测 ``[(bucket_start_iso, value), ...]``（bucket_start 为本地
            时区 ISO 字符串）。
        target_bucket: 要预测的目标桶起始时刻（本地时区 ISO 字符串）。

    Returns:
        命中样本的历史均值；命中的历史「星期×小时」样本不足
        （< :data:`_MIN_BASELINE_SAMPLES`）时返回 None。
    """
    target = _parse_aware(target_bucket)
    key = (target.isoweekday(), target.hour)
    values = [value for iso, value in series if _key_of_iso(iso) == key]
    if len(values) < _MIN_BASELINE_SAMPLES:
        return None
    return sum(values) / len(values)


def _key_of_iso(iso: str) -> tuple[int, int]:
    """从本地时区 ISO 字符串解析「星期×小时」键。"""
    dt = _parse_aware(iso)
    return dt.isoweekday(), dt.hour


def _percentile(sorted_values: list[float], p: float) -> float:
    """最近秩分位数（样本非空）。"""
    rank = max(int(p / 100.0 * len(sorted_values)) - 1, 0)
    return sorted_values[rank]


def _robust_mean(values: list[float], iqr_k: float) -> float | None:
    """IQR 稳健均值：剔除 [Q1-k*IQR, Q3+k*IQR] 之外的值后取算术均值。"""
    if not values:
        return None
    if len(values) < _MIN_TRIM_SAMPLES:
        return sum(values) / len(values)
    ordered = sorted(values)
    q1 = _percentile(ordered, 25)
    q3 = _percentile(ordered, 75)
    iqr = q3 - q1
    low = q1 - iqr_k * iqr
    high = q3 + iqr_k * iqr
    kept = [v for v in values if low <= v <= high]
    if not kept:
        kept = values
    return sum(kept) / len(kept)


def _confidence(count: int, min_sample: int) -> float:
    """样本数驱动的单调置信度（0..1）。"""
    if count <= 0:
        return 0.0
    return count / (count + min_sample)


def predict_peaks(
    conn: sqlite3.Connection,
    *,
    metric: str = "request_rate",
    horizon_days: int = 7,
    min_sample: int = 20,
    now_utc: str | None = None,
) -> list[Prediction]:
    """预测未来 ``horizon_days`` 内每个小时桶的自身请求到达率。

    只做只读预测，**绝不写入任何调度状态**。训练窗口取最近
    :data:`_TRAIN_DAYS` 天（严格早于 ``now``），按生效配置的本地时区与节假日
    分组。每个目标桶命中样本不足 ``min_sample`` 时标记 ``INSUFFICIENT_SAMPLE``
    且 ``value=None``（保守，不默认低压力）。

    Args:
        conn: 数据库连接。
        metric: 预测指标（当前仅 ``request_rate``）。
        horizon_days: 预测跨度（天）。
        min_sample: 数据质量充足的最小样本数。
        now_utc: 参考当前时刻（UTC ISO 字符串）；省略取系统当前 UTC。

    Returns:
        按时间升序的每小时桶 :class:`Prediction` 列表。
    """
    now = _parse_aware(now_utc) if now_utc else datetime.now(UTC)
    config = get_active_config(conn)
    tz_str = config.windows.timezone
    tz = ZoneInfo(tz_str)
    holidays = set(config.analysis.holidays)

    train_start = now - timedelta(days=_TRAIN_DAYS)
    train = _hourly_series(conn, timezone=tz_str, since=train_start, until=now, metric=metric)
    groups = _seasonal_groups(train, holidays)

    model = ForecastModel()
    predictions: list[Prediction] = []
    current = _local_bucket(now, tz)
    horizon_end = current + timedelta(days=horizon_days)
    slot = current
    while slot < horizon_end:
        key = (slot.isoweekday(), slot.hour)
        values = groups.get(key, [])
        count = len(values)
        bucket_iso = slot.isoformat()
        if count < min_sample:
            predictions.append(
                Prediction(
                    bucket_start=bucket_iso,
                    horizon_days=horizon_days,
                    metric=metric,
                    value=None,
                    confidence=None,
                    sample_count=count,
                    data_quality=_DATA_QUALITY_INSUFFICIENT,
                    model=model.name,
                    train_start=train_start.isoformat(),
                    train_end=now.isoformat(),
                )
            )
        else:
            predictions.append(
                Prediction(
                    bucket_start=bucket_iso,
                    horizon_days=horizon_days,
                    metric=metric,
                    value=_robust_mean(values, model.outlier_iqr_k),
                    confidence=_confidence(count, min_sample),
                    sample_count=count,
                    data_quality=_DATA_QUALITY_OK,
                    model=model.name,
                    train_start=train_start.isoformat(),
                    train_end=now.isoformat(),
                )
            )
        slot += timedelta(hours=1)
    return predictions


def _mae(pairs: Sequence[tuple[float, float]]) -> float:
    """平均绝对误差（``expected`` 为实际值）。"""
    return sum(abs(pred - actual) for pred, actual in pairs) / len(pairs)


def backtest(
    conn: sqlite3.Connection,
    *,
    model: ForecastModel | None = None,
    horizon_days: int = 1,
    min_sample: int = 20,
    now_utc: str | None = None,
) -> BacktestReport:
    """对预测模型做时间序列切分回测，与简单基线对比（S4-02）。

    把最近 :data:`_BACKTEST_DAYS` 天的自身观测按时间升序切分为训练（前
    ``_SPLIT_RATIO``）与测试（其余），**训练严格早于测试**（断言无未来泄漏）。
    对每个测试桶，仅用训练窗口内同「星期×小时」的历史分别以模型（IQR 稳健均值）
    与基线（朴素均值）预测，比较二者 MAE。

    Args:
        conn: 数据库连接。
        model: 要评估的模型；省略使用 :class:`ForecastModel`。
        horizon_days: 本参数为保证接口与 ``predict_peaks`` 一致保留，不改变切分。
        min_sample: 数据质量充足的最小样本数（评估的有效测试桶下限据此判定）。
        now_utc: 参考当前时刻（UTC ISO 字符串）。

    Returns:
        回测评估报告 :class:`BacktestReport`。
    """
    del horizon_days  # 接口一致性占位：时间序列切分与跨期预测共用同一切分方式
    model = model or ForecastModel()
    now = _parse_aware(now_utc) if now_utc else datetime.now(UTC)
    config = get_active_config(conn)
    tz_str = config.windows.timezone
    holidays = set(config.analysis.holidays)

    start = now - timedelta(days=_BACKTEST_DAYS)
    full = _hourly_series(conn, timezone=tz_str, since=start, until=now, metric="request_rate")

    report = BacktestReport(
        model=model.name,
        train_start=start.isoformat(),
        train_end=now.isoformat(),
        test_start=start.isoformat(),
        test_end=now.isoformat(),
        metric="request_rate",
        model_error=None,
        baseline_error=None,
        verdict=_VERDICT_INSUFFICIENT,
        sample_count=0,
    )

    if len(full) < _MIN_EVAL_BUCKETS * 2:
        return report

    split = max(int(len(full) * _SPLIT_RATIO), 1)
    train_series = full[:split]
    test_series = full[split:]
    if not train_series or not test_series:
        return report

    # 无未来泄漏：训练最后一个桶必须严格早于测试第一个桶（时间序列切分不变量）。
    if not (train_series[-1].bucket_start < test_series[0].bucket_start):
        msg = "回测时间序列切分存在未来数据泄漏"
        raise RuntimeError(msg)

    groups = _seasonal_groups(train_series, holidays)
    model_pairs: list[tuple[float, float]] = []
    baseline_pairs: list[tuple[float, float]] = []
    for test_bucket in test_series:
        key = test_bucket.weekday_hour
        values = groups.get(key, [])
        if len(values) < _MIN_BASELINE_SAMPLES:
            continue  # 基线都无法计算，跳过该桶，避免不公平比较
        actual = test_bucket.value
        model_pred = _robust_mean(values, model.outlier_iqr_k)
        base_pred = sum(values) / len(values)
        if model_pred is not None:
            model_pairs.append((model_pred, actual))
        baseline_pairs.append((base_pred, actual))

    sample_count = len(model_pairs)
    if sample_count < min_sample or len(baseline_pairs) < min_sample:
        return report

    model_error = _mae(model_pairs)
    baseline_error = _mae(baseline_pairs)
    if baseline_error is None:
        return report

    verdict = (
        _VERDICT_BETTER
        if model_error <= baseline_error * (1.0 + _VERDICT_TOLERANCE)
        else _VERDICT_WORSE
    )
    return BacktestReport(
        model=model.name,
        train_start=train_series[0].iso,
        train_end=train_series[-1].iso,
        test_start=test_series[0].iso,
        test_end=test_series[-1].iso,
        metric="request_rate",
        model_error=model_error,
        baseline_error=baseline_error,
        verdict=verdict,
        sample_count=sample_count,
    )
