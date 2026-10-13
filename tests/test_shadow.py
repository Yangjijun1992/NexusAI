"""阶段 4 影子模式（S4-03/S4-04）测试。

使用临时数据库与可注入时钟（无网络）确定性验证：
- 迁移 008 创建 ``shadow_recommendations`` / ``forecast_runs`` 表。
- ``recommend`` 只写 ``shadow_recommendations``，**绝不改写** ``tasks`` 或真实
  ``schedule_decisions``（影子模式的只读/建议性质）。
- 高峰对普通任务建议 DELAY，对重要任务（高优先级）建议 ALLOW。
- ``record`` / ``list_recommendations`` 往返。
- ``evaluate`` 生成效果报告（命中率、样本、判定）。
- ``propose_policy_update`` 仅在证据通过评估门限时生成提案；且**绝不自动激活**。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from nexusai_lite import db
from nexusai_lite.collector.window_rules import WindowRuleData, upsert_window_rule
from nexusai_lite.config import get_active_config, list_versions
from nexusai_lite.shadow import (
    ShadowEvaluation,
    ShadowMode,
    ShadowRecommendation,
    propose_policy_update,
)
from nexusai_lite.tasks import TaskService

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

TZ = ZoneInfo("Asia/Shanghai")
# 2026-10-12 为周一；北京 10:00 = UTC 02:00（高峰内）。
_PEAK_UTC = datetime(2026, 10, 12, 2, 0, 0, tzinfo=UTC)
# 北京 13:00 = UTC 05:00（两高峰窗口之间，空闲）。
_OFF_PEAK_UTC = datetime(2026, 10, 12, 5, 0, 0, tzinfo=UTC)

_DEFAULT_RULE = WindowRuleData(
    timezone="Asia/Shanghai",
    workdays=(1, 2, 3, 4, 5),
    peak_windows=(("09:00", "12:00"), ("14:00", "18:00")),
    source="official-pricing-page",
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（含 008 迁移）。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    c = db.connect(db_path_)
    yield c
    c.close()


def _insert(conn: sqlite3.Connection, started_at: str, count: int = 1) -> None:
    """在指定 UTC 时刻写入 ``count`` 条自身请求记录。"""
    for _ in range(count):
        conn.execute(
            "INSERT INTO api_request_logs (request_id, provider, started_at, status_code) "
            "VALUES (?, 'deepseek', ?, 200)",
            (uuid.uuid4().hex, started_at),
        )


def _make_rec(action: str, created_at: str) -> ShadowRecommendation:
    """构造一条建议记录（用于直接注入评估数据）。"""
    return ShadowRecommendation(
        recommendation_id=uuid.uuid4().hex,
        task_id="task-x",
        recommended_action=action,
        reason_codes=("shadow-rule",),
        predicted_metric=None,
        model="shadow-window-rule",
        created_at=created_at,
    )


# ---------------------------------------------------------------- 迁移

def test_migration_008_creates_tables(conn: sqlite3.Connection) -> None:
    """迁移 008 应创建影子建议与预测审计表。"""
    for table in ("shadow_recommendations", "forecast_runs"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None, f"缺少表 {table}"


# ---------------------------------------------------------------- recommend

def test_recommend_writes_only_shadow(conn: sqlite3.Connection) -> None:
    """recommend 只写影子表，绝不改写 tasks 或 schedule_decisions。"""
    upsert_window_rule(conn, _DEFAULT_RULE, source="official-pricing-page")
    svc = TaskService(conn)
    for priority in ("normal", "high"):
        task = svc.create_task(task_type="llm", priority=priority)
        svc.queue(task.task_id)

    before_tasks = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    before_decisions = conn.execute("SELECT COUNT(*) AS c FROM schedule_decisions").fetchone()["c"]
    before_shadow = conn.execute("SELECT COUNT(*) AS c FROM shadow_recommendations").fetchone()["c"]

    shadow = ShadowMode(conn, now_fn=lambda: _PEAK_UTC)
    recs = shadow.recommend(now=_PEAK_UTC)

    assert len(recs) == 2
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == before_tasks
    assert (
        conn.execute("SELECT COUNT(*) AS c FROM schedule_decisions").fetchone()["c"]
        == before_decisions
    )
    assert (
        conn.execute("SELECT COUNT(*) AS c FROM shadow_recommendations").fetchone()["c"]
        == before_shadow + 2
    )


def test_recommend_peak_defer_and_important_allowed(conn: sqlite3.Connection) -> None:
    """高峰对普通任务建议 DELAY，对重要任务（高优先级）建议 ALLOW。"""
    upsert_window_rule(conn, _DEFAULT_RULE, source="official-pricing-page")
    svc = TaskService(conn)
    normal = svc.create_task(task_type="llm", priority="normal")
    svc.queue(normal.task_id)
    high = svc.create_task(task_type="llm", priority="high")
    svc.queue(high.task_id)

    shadow = ShadowMode(conn, now_fn=lambda: _PEAK_UTC)
    recs = shadow.recommend(now=_PEAK_UTC)
    by_task = {r.task_id: r for r in recs}
    assert by_task[normal.task_id].recommended_action == "DELAY"
    assert by_task[high.task_id].recommended_action == "ALLOW"
    assert "important_task" in by_task[high.task_id].reason_codes


def test_recommend_off_peak_allows(conn: sqlite3.Connection) -> None:
    """空闲时段普通任务应建议 ALLOW。"""
    upsert_window_rule(conn, _DEFAULT_RULE, source="official-pricing-page")
    svc = TaskService(conn)
    normal = svc.create_task(task_type="llm", priority="normal")
    svc.queue(normal.task_id)

    shadow = ShadowMode(conn, now_fn=lambda: _OFF_PEAK_UTC)
    recs = shadow.recommend(now=_OFF_PEAK_UTC)
    assert recs[0].recommended_action == "ALLOW"


# ---------------------------------------------------------------- record / list

def test_record_and_list_round_trip(conn: sqlite3.Connection) -> None:
    """record 后可经 list_recommendations 读回完整字段。"""
    shadow = ShadowMode(conn, now_fn=lambda: _PEAK_UTC)
    rec = ShadowRecommendation(
        recommendation_id="abc123",
        task_id="t1",
        recommended_action="DELAY",
        reason_codes=("shadow_peak_defer",),
        predicted_metric=12.5,
        model="shadow-window-rule",
        created_at=_PEAK_UTC.isoformat(),
    )
    shadow.record(rec)

    listed = shadow.list_recommendations(since=_PEAK_UTC.isoformat())
    assert len(listed) == 1
    got = listed[0]
    assert got.recommendation_id == "abc123"
    assert got.task_id == "t1"
    assert got.recommended_action == "DELAY"
    assert got.reason_codes == ("shadow_peak_defer",)
    assert got.predicted_metric == 12.5


# ---------------------------------------------------------------- evaluate

def test_evaluate_produces_report(conn: sqlite3.Connection) -> None:
    """evaluate 应对比建议与实际观测输出命中率与判定。"""
    now = datetime(2026, 10, 12, 6, 0, 0, tzinfo=UTC)  # 北京 14:00 周一
    # (Mon,10) 北京=02:00 UTC：三低一高 → 中位数 3；10-05 为 9 → 高峰（命中 DELAY）。
    for day in (
        "2026-09-14T02:00:00+00:00",
        "2026-09-21T02:00:00+00:00",
        "2026-09-28T02:00:00+00:00",
    ):
        _insert(conn, day, count=3)
    _insert(conn, "2026-10-05T02:00:00+00:00", count=9)
    # (Mon,11) 北京=03:00 UTC：全为 5 → 中位数 5；10-05 为 5 → 未达高峰（DELAY 未命中）。
    for day in (
        "2026-09-14T03:00:00+00:00",
        "2026-09-21T03:00:00+00:00",
        "2026-09-28T03:00:00+00:00",
        "2026-10-05T03:00:00+00:00",
    ):
        _insert(conn, day, count=5)

    shadow = ShadowMode(conn, now_fn=lambda: now)
    shadow.record(_make_rec("DELAY", "2026-10-05T02:00:00+00:00"))
    shadow.record(_make_rec("DELAY", "2026-10-05T03:00:00+00:00"))

    report = shadow.evaluate(days=30)
    assert report.total == 2
    assert report.delay_recommended == 2
    assert report.allow_recommended == 0
    assert report.sample_count == 2
    assert report.hit_rate == pytest.approx(0.5)
    assert report.verdict == "insufficient_data"
    assert report.data_quality == "INSUFFICIENT_SAMPLE"


# ---------------------------------------------------------------- propose_policy_update

def _gate_evidence() -> ShadowEvaluation:
    """构造通过评估门限的证据（effective 且样本充足）。"""
    return ShadowEvaluation(
        window_days=30,
        total=20,
        delay_recommended=15,
        allow_recommended=5,
        sample_count=20,
        hit_rate=0.9,
        data_quality="ok",
        verdict="effective",
        peak_hours=("09:00",),
    )


def test_propose_policy_update_returns_proposal_when_gate_passes(
    conn: sqlite3.Connection,
) -> None:
    """证据通过门限时应生成提案并返回版本号，且不自动激活。"""
    active_before = get_active_config(conn).meta.version
    version = propose_policy_update(
        conn,
        evidence=_gate_evidence(),
        change_reason="影子评估 peak_hours=09:00",
    )
    assert version is not None
    assert version == 2  # v1 已由 seed 创建，提案为新版本。
    # 提案 status=proposed 存在。
    statuses = {
        r["version"]: r["status"]
        for r in conn.execute("SELECT version, status FROM config_versions").fetchall()
    }
    assert statuses[2] == "proposed"
    # 未自动激活：active 版本不变。
    active_after = get_active_config(conn).meta.version
    assert active_after == active_before


def test_propose_policy_update_no_auto_activation(conn: sqlite3.Connection) -> None:
    """propose 只产生 proposed 版本，绝不调用激活（active 版本保持 v1）。"""
    propose_policy_update(conn, evidence=_gate_evidence(), change_reason="影子评估")
    active_rows = [r for r in list_versions(conn) if r["status"] == "active"]
    assert len(active_rows) == 1
    assert active_rows[0]["version"] == 1


def test_propose_policy_update_none_when_ineffective(conn: sqlite3.Connection) -> None:
    """证据 verdict 非 effective 时不应生成提案。"""
    evidence = ShadowEvaluation(
        window_days=30,
        total=20,
        delay_recommended=10,
        allow_recommended=10,
        sample_count=20,
        hit_rate=0.3,
        data_quality="ok",
        verdict="ineffective",
        peak_hours=(),
    )
    version = propose_policy_update(conn, evidence=evidence, change_reason="低命中率")
    assert version is None
    # 未产生新配置版本。
    assert len(list_versions(conn)) == 1


def test_propose_policy_update_none_when_insufficient_samples(conn: sqlite3.Connection) -> None:
    """证据样本不足时不应生成提案。"""
    evidence = ShadowEvaluation(
        window_days=30,
        total=5,
        delay_recommended=3,
        allow_recommended=2,
        sample_count=5,
        hit_rate=1.0,
        data_quality="INSUFFICIENT_SAMPLE",
        verdict="insufficient_data",
        peak_hours=(),
    )
    version = propose_policy_update(conn, evidence=evidence, change_reason="样本不足")
    assert version is None
    assert len(list_versions(conn)) == 1
