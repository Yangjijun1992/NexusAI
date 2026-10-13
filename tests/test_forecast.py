"""阶段 4 预测（S4-01/S4-02）测试。

使用临时数据库与合成时间序列（无网络）确定性验证：
- :class:`Prediction` 字段与 ``INSUFFICIENT_SAMPLE`` 质量标记（样本不足）。
- ``baseline_predict`` 在合成序列上的正确性及其样本不足返回值。
- ``predict_peaks`` 的节假日处理、数据质量标记，以及**只读**（不写任何调度行）。
- ``backtest`` 的时间序列切分无未来泄漏，以及 better/worse/insufficient 判定。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from nexusai_lite import db
from nexusai_lite.collector.window_rules import get_active_window_rule
from nexusai_lite.config import activate_config, default_config, get_active_config, propose_config
from nexusai_lite.forecast import (
    ForecastModel,
    Prediction,
    backtest,
    baseline_predict,
    predict_peaks,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

TZ = ZoneInfo("Asia/Shanghai")
# 2026-10-12 为周一（追求稳定的工作日×小时分桶）。
NOW_UTC = "2026-10-12T01:00:00+00:00"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（含 008 迁移）。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    c = db.connect(db_path_)
    yield c
    c.close()


def _insert(conn: sqlite3.Connection, started_at: str, count: int = 1) -> None:
    """在 ``started_at`` 时刻写入 ``count`` 条自身请求记录（request_id 去重）。"""
    for _ in range(count):
        conn.execute(
            "INSERT INTO api_request_logs (request_id, provider, started_at, status_code) "
            "VALUES (?, 'deepseek', ?, 200)",
            (uuid.uuid4().hex, started_at),
        )


def _set_holidays(conn: sqlite3.Connection, holidays: list[str]) -> None:
    """配置 analysis.holidays 并激活（用于节假日处理测试）。"""
    payload = default_config().model_dump()
    payload["analysis"]["holidays"] = holidays
    version = propose_config(conn, payload, "设置节假日")
    activate_config(conn, version)


# ---------------------------------------------------------------- Prediction 字段

def test_prediction_fields() -> None:
    """Prediction 应暴露全部规定字段。"""
    pred = Prediction(
        bucket_start="2026-10-19T10:00:00+08:00",
        horizon_days=7,
        metric="request_rate",
        value=1.0,
        confidence=0.8,
        sample_count=20,
        data_quality="ok",
        model="seasonal-mean-robust",
        train_start="2026-07-01T00:00:00+00:00",
        train_end="2026-10-12T00:00:00+00:00",
    )
    assert pred.bucket_start == "2026-10-19T10:00:00+08:00"
    assert pred.horizon_days == 7
    assert pred.metric == "request_rate"
    assert pred.value == 1.0
    assert pred.confidence == 0.8
    assert pred.sample_count == 20
    assert pred.data_quality == "ok"
    assert pred.model == "seasonal-mean-robust"


def test_predictions_have_explicit_train_window(conn: sqlite3.Connection) -> None:
    """每条预测都应携带明确的训练窗口（S4-01 验收）。"""
    now = datetime.fromisoformat(NOW_UTC)
    train_start = now - timedelta(days=90)
    # 在过去的若干个周一 10:00（北京）各插入 1 条，凑足 min_sample。
    for weeks in range(1, 5):
        monday = now - timedelta(weeks=weeks)
        bucket_utc = monday.astimezone(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
        _insert(conn, bucket_utc.astimezone(UTC).isoformat())
    predictions = predict_peaks(conn, min_sample=3, now_utc=NOW_UTC)
    assert predictions
    for p in predictions:
        assert p.train_start == train_start.isoformat()
        assert p.train_end == now.isoformat()
        assert p.model == "seasonal-mean-robust"
    assert all(p.metric == "request_rate" for p in predictions)


# ---------------------------------------------------------------- baseline_predict

def test_baseline_predict_same_weekday_hour_mean() -> None:
    """基线应返回目标桶同「星期×小时」的历史算术均值。"""
    # 所有样本都在周一（isoweekday=1）10:00；target 为下一个周一 10:00。
    series = [
        ("2026-09-07T10:00:00+08:00", 5.0),
        ("2026-09-14T10:00:00+08:00", 7.0),
        ("2026-09-21T10:00:00+08:00", 9.0),
    ]
    target = "2026-10-05T10:00:00+08:00"
    assert baseline_predict(series, target) == pytest.approx(7.0)


def test_baseline_predict_insufficient_returns_none() -> None:
    """命中的历史「星期×小时」样本不足时应返回 None。"""
    series = [("2026-09-07T10:00:00+08:00", 5.0)]  # 仅 1 个命中样本
    target = "2026-10-05T10:00:00+08:00"
    assert baseline_predict(series, target) is None


# ---------------------------------------------------------------- predict_peaks

def test_predict_peaks_ok_when_sufficient(conn: sqlite3.Connection) -> None:
    """命中样本足够时预测应给出 value 且 quality=ok。"""
    now = datetime.fromisoformat(NOW_UTC)
    for weeks in range(1, 5):
        monday = now - timedelta(weeks=weeks)
        bucket_utc = monday.astimezone(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
        _insert(conn, bucket_utc.astimezone(UTC).isoformat())
    predictions = predict_peaks(conn, min_sample=3, horizon_days=8, now_utc=NOW_UTC)
    # 未来最近一个周一 10:00（北京）。
    target = "2026-10-19T10:00:00+08:00"
    match = [p for p in predictions if p.bucket_start == target]
    assert len(match) == 1
    assert match[0].data_quality == "ok"
    assert match[0].value == pytest.approx(1.0)
    assert match[0].sample_count == 4
    assert match[0].confidence is not None


def test_predict_peaks_insufficient_sample(conn: sqlite3.Connection) -> None:
    """命中样本不足 min_sample 时应标记 INSUFFICIENT_SAMPLE 且 value=None。"""
    now = datetime.fromisoformat(NOW_UTC)
    for weeks in range(1, 5):
        monday = now - timedelta(weeks=weeks)
        bucket_utc = monday.astimezone(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
        _insert(conn, bucket_utc.astimezone(UTC).isoformat())
    # 默认 min_sample=20 > 4：同「星期×小时」命中不足。
    predictions = predict_peaks(conn, horizon_days=8, now_utc=NOW_UTC)
    target = "2026-10-19T10:00:00+08:00"
    match = [p for p in predictions if p.bucket_start == target]
    assert len(match) == 1
    assert match[0].data_quality == "INSUFFICIENT_SAMPLE"
    assert match[0].value is None
    assert match[0].confidence is None
    assert match[0].sample_count == 4


def test_predict_peaks_handles_holidays(conn: sqlite3.Connection) -> None:
    """节假日桶不参与常规季节性预测（其样本应从命中计数中剔除）。"""
    # 2026-09-28 与 2026-10-05 均为周一；把一个设为节假日。
    _set_holidays(conn, ["2026-09-28"])
    # 非节假日周一 10:00（北京）1 条；节假日周一 10:00（北京）5 条。
    _insert(conn, "2026-10-05T02:00:00+00:00", count=1)  # 非节假日周一
    _insert(conn, "2026-09-28T02:00:00+00:00", count=5)  # 节假日周一
    predictions = predict_peaks(conn, min_sample=2, horizon_days=8, now_utc=NOW_UTC)
    target = "2026-10-19T10:00:00+08:00"  # 未来周一 10:00
    match = [p for p in predictions if p.bucket_start == target]
    assert len(match) == 1
    # 节假日桶被剔除 → 仅剩 1 个非节假日命中（< min_sample=2）→ 标记不足。
    assert match[0].data_quality == "INSUFFICIENT_SAMPLE"
    assert match[0].value is None
    assert match[0].sample_count == 1


def test_predict_peaks_read_only(conn: sqlite3.Connection) -> None:
    """预测必须只读：不得写入任何调度状态或任务行。"""
    _insert(conn, "2026-09-07T02:00:00+00:00")
    before_tasks = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    before_decisions = conn.execute("SELECT COUNT(*) AS c FROM schedule_decisions").fetchone()["c"]
    predict_peaks(conn, min_sample=1, now_utc=NOW_UTC)
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == before_tasks
    assert (
        conn.execute("SELECT COUNT(*) AS c FROM schedule_decisions").fetchone()["c"]
        == before_decisions
    )


# ---------------------------------------------------------------- backtest

def _seed_backtest(conn: sqlite3.Connection, *, test_value: int) -> None:
    """填充 60 天窗口内的周一 10:00（北京）序列。

    首个训练周一为离群值（20 条），其余训练周一为 1 条；测试端最后两个周一的
    实际值取 ``test_value``。非周一小时不插入（保持空桶），便于仅聚焦该季节组。
    """
    now = datetime.fromisoformat(NOW_UTC)
    start = now - timedelta(days=60)
    tz = TZ
    slots: list[datetime] = []
    slot = start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    end = now
    while slot < end:
        if slot.isoweekday() == 1 and slot.hour == 10:
            slots.append(slot)
        slot += timedelta(hours=1)

    assert len(slots) >= 5
    # 最早一个为训练离群值（20 条），最后两个为测试实际值，其余训练为 1 条。
    outlier = slots[0]
    test_slots = slots[-2:]
    for s in slots:
        if s == outlier:
            _insert(conn, s.astimezone(UTC).isoformat(), count=20)
        elif s in test_slots:
            _insert(conn, s.astimezone(UTC).isoformat(), count=test_value)
        else:
            _insert(conn, s.astimezone(UTC).isoformat(), count=1)


def test_backtest_no_future_leakage(conn: sqlite3.Connection) -> None:
    """时间序列切分必须训练严格早于测试（无未来泄漏）。"""
    _seed_backtest(conn, test_value=1)
    report = backtest(conn, min_sample=2, now_utc=NOW_UTC)
    assert datetime.fromisoformat(report.train_end) < datetime.fromisoformat(report.test_start)
    assert report.model == ForecastModel().name


def test_backtest_better_or_equal_with_robust_model(conn: sqlite3.Connection) -> None:
    """训练含离群值、测试为正常值时，稳健模型应优于或匹配基线。"""
    _seed_backtest(conn, test_value=1)
    report = backtest(conn, min_sample=2, now_utc=NOW_UTC)
    assert report.verdict == "better_or_equal"
    assert report.model_error is not None
    assert report.baseline_error is not None
    assert report.model_error <= report.baseline_error
    assert report.sample_count >= 2


def test_backtest_worse_with_model_missing_test_spike(conn: sqlite3.Connection) -> None:
    """测试端出现训练未见的高尖峰时，稳健模型（剔除离群）可能差于基线。"""
    _seed_backtest(conn, test_value=20)
    report = backtest(conn, min_sample=2, now_utc=NOW_UTC)
    assert report.verdict == "worse"
    assert report.model_error is not None
    assert report.baseline_error is not None
    assert report.model_error > report.baseline_error


def test_backtest_insufficient_data(conn: sqlite3.Connection) -> None:
    """无足够自身观测时应标记 insufficient_data。"""
    report = backtest(conn, now_utc=NOW_UTC)
    assert report.verdict == "insufficient_data"
    assert report.model_error is None
    assert report.baseline_error is None
    assert report.sample_count == 0


def test_backtest_never_writes_schedule(conn: sqlite3.Connection) -> None:
    """回测必须只读，不得写入调度状态。"""
    _seed_backtest(conn, test_value=1)
    before = {
        table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]  # noqa: S608 - 表名来自固定字面元组
        for table in ("tasks", "schedule_decisions", "shadow_recommendations", "forecast_runs")
    }
    backtest(conn, min_sample=2, now_utc=NOW_UTC)
    for table, count in before.items():
        assert (
            conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] == count  # noqa: S608 - 表名来自固定字面元组
        )


def test_config_and_rule_loaded(conn: sqlite3.Connection) -> None:
    """预测/回测应能读取生效配置与窗口规则。"""
    assert get_active_config(conn).windows.timezone == "Asia/Shanghai"
    rule = get_active_window_rule(conn)
    assert rule is None or rule.timezone == "Asia/Shanghai"
