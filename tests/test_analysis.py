"""时段规律分析测试（S1-05）。

在临时数据库用 :class:`DbRequestLogger` 写入构造于已知时刻的记录，验证桶归属
优先级（holiday > weekend > workday_peak > workday_night > workday_offpeak）、
数据质量标记与规则缺失时的保守行为。无网络。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nexusai_lite import db
from nexusai_lite.analysis import BucketStats, PeriodReport, period_stats
from nexusai_lite.collector.window_rules import WindowRuleData
from nexusai_lite.records import RequestRecord
from nexusai_lite.stats import DbRequestLogger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

# 固定参考时刻：分析窗口为 2026-09-18 .. 2026-10-18（UTC）。
NOW_UTC = "2026-10-18T00:00:00+00:00"
DAYS = 30

# 默认规则：北京时间周一至周五 09:00-12:00、14:00-18:00 为高峰。
_DEFAULT_RULE = WindowRuleData()


def _db_file(tmp_path: Path) -> Path:
    path = tmp_path / "nexusai.db"
    db.init_db(path)
    return path


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(_db_file(tmp_path))


def _record(request_id: str, started_at: str) -> RequestRecord:
    """构造一条成功请求记录。"""
    return RequestRecord(
        request_id=request_id,
        provider="deepseek",
        model="deepseek-chat",
        started_at=started_at,
        ended_at=started_at,
        latency_ms=120,
        status_code=200,
        retry_count=0,
        input_tokens=3,
        output_tokens=2,
        estimated_cost=0.001,
        cost_status="estimated",
        error_category=None,
        safe_error_message=None,
        raw_error_reference=None,
        streaming=False,
        task_id="task-1",
        attempts=(),
    )


def _seed(tmp_path: Path, records: list[RequestRecord]) -> None:
    logger = DbRequestLogger(_db_file(tmp_path))
    for record in records:
        logger.log_request(record)


def _bucket(report: PeriodReport, name: str) -> BucketStats:
    for bucket in report.buckets:
        if bucket.bucket == name:
            return bucket
    msg = f"未找到桶 {name}"
    raise AssertionError(msg)


def _run(
    tmp_path: Path,
    *,
    rule: WindowRuleData | None = _DEFAULT_RULE,
    holidays: Sequence[str] = (),
) -> PeriodReport:
    conn = _conn(tmp_path)
    try:
        return period_stats(
            conn,
            days=DAYS,
            rule=rule,
            holidays=holidays,
            night_start="22:00",
            night_end="07:00",
            min_sample=20,
            now_utc=NOW_UTC,
        )
    finally:
        conn.close()


# ------------------------------------------------------------------ 桶归属
class TestBucketClassification:
    def test_four_buckets_and_holiday(self, tmp_path: Path) -> None:
        """北京周一高峰/空闲/夜间、周末与节假日各归入正确桶。"""
        records = [
            # 北京周一 09:30 = UTC 2026-10-12 01:30 → workday_peak
            _record("peak", "2026-10-12T01:30:00+00:00"),
            # 北京周一 13:30 = UTC 05:30 → workday_offpeak
            _record("offpeak", "2026-10-12T05:30:00+00:00"),
            # 北京周一 23:00 = UTC 15:00 → workday_night
            _record("night", "2026-10-12T15:00:00+00:00"),
            # 北京周六 09:30 = UTC 2026-10-17 01:30 → weekend
            _record("weekend", "2026-10-17T01:30:00+00:00"),
            # 节假日 2026-10-01（北京 09:00 = UTC 10-01 01:00）→ holiday
            _record("holiday", "2026-10-01T01:00:00+00:00"),
        ]
        _seed(tmp_path, records)
        report = _run(tmp_path, holidays=("2026-10-01",))

        assert _bucket(report, "workday_peak").sample_count == 1
        assert _bucket(report, "workday_offpeak").sample_count == 1
        assert _bucket(report, "workday_night").sample_count == 1
        assert _bucket(report, "weekend").sample_count == 1
        assert _bucket(report, "holiday").sample_count == 1
        assert report.sample_count == 5
        assert report.timezone == "Asia/Shanghai"

    def test_holiday_wins_over_weekend(self, tmp_path: Path) -> None:
        """同一日期既在节假日也在周末时，优先级 holiday > weekend。"""
        _seed(tmp_path, [_record("satholiday", "2026-10-17T01:30:00+00:00")])
        report = _run(tmp_path, holidays=("2026-10-17",))
        assert _bucket(report, "holiday").sample_count == 1
        assert _bucket(report, "weekend").sample_count == 0

    def test_night_window_wraps_midnight(self, tmp_path: Path) -> None:
        """夜间窗口跨午夜（22:00-07:00）：凌晨北京 03:00 归夜间。"""
        # 北京 2026-10-13 周二 03:00 = UTC 2026-10-12 19:00。
        _seed(tmp_path, [_record("wraponly", "2026-10-12T19:00:00+00:00")])
        report = _run(tmp_path)
        assert _bucket(report, "workday_night").sample_count == 1
        assert _bucket(report, "workday_offpeak").sample_count == 0
        assert _bucket(report, "workday_peak").sample_count == 0


# ------------------------------------------------------------------ 数据质量
class TestQuality:
    def test_insufficient_sample_flag(self, tmp_path: Path) -> None:
        """桶内样本不足 → INSUFFICIENT_SAMPLE。"""
        _seed(tmp_path, [_record("only", "2026-10-12T01:30:00+00:00")])
        report = _run(tmp_path, rule=_DEFAULT_RULE, holidays=())
        bucket = _bucket(report, "workday_peak")
        assert bucket.sample_count == 1
        assert bucket.quality == "INSUFFICIENT_SAMPLE"

    def test_rule_none_no_peak_classification(self, tmp_path: Path) -> None:
        """规则缺失：workday_peak 桶计数 0、质量 NO_RULE，不得推断高峰归属。"""
        # 北京周一高峰时刻（09:30 与 14:30）的记录，规则缺失时必须落到夜/空闲。
        records = [
            _record("p1", "2026-10-12T01:30:00+00:00"),  # 北京 09:30
            _record("p2", "2026-10-12T06:30:00+00:00"),  # 北京 14:30
        ]
        _seed(tmp_path, records)
        report = _run(tmp_path, rule=None)

        peak = _bucket(report, "workday_peak")
        assert peak.sample_count == 0
        assert peak.quality == "NO_RULE"
        # 两条高峰时刻记录不得误入 peak，应落入空闲。
        assert _bucket(report, "workday_offpeak").sample_count == 2
        assert _bucket(report, "workday_peak").sample_count == 0


# ------------------------------------------------------------------ 序列化与顺序
class TestReportShape:
    def test_fixed_bucket_order_and_json(self, tmp_path: Path) -> None:
        """桶按固定顺序输出且 to_dict 可 JSON 序列化。"""
        _seed(
            tmp_path,
            [
                _record("a", "2026-10-12T01:30:00+00:00"),
                _record("b", "2026-10-17T01:30:00+00:00"),
            ],
        )
        report = _run(tmp_path, rule=_DEFAULT_RULE)
        names = [b.bucket for b in report.buckets]
        assert names == ["workday_peak", "workday_offpeak", "workday_night", "weekend", "holiday"]
        payload = json.loads(json.dumps(report.to_dict()))
        assert payload["days"] == DAYS
        assert payload["sample_count"] == 2
        assert payload["timezone"] == "Asia/Shanghai"
