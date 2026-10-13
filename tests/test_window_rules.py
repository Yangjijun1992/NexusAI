"""官方时段解析与 ``window_rules`` 版本化管理测试（S1-02）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexusai_lite import db
from nexusai_lite.collector import window_rules
from nexusai_lite.collector.window_rules import (
    WindowRuleData,
    get_active_window_rule,
    is_peak,
    is_workday,
    parse_peak_windows,
    upsert_window_rule,
)

if TYPE_CHECKING:
    import sqlite3

FIXTURES = Path(__file__).parent / "fixtures"

# 默认规则：北京时间周一至周五 09:00-12:00、14:00-18:00 为高峰。
_DEFAULT_RULE = WindowRuleData()


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    return db.connect(db_path)


def _utc(*, year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """构造带 UTC 时区的给定时刻（测试中统一用 naive 时刻的时区做换算）。"""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ------------------------------------------------------------- parse_peak_windows
def test_parse_from_real_pricing_fixture() -> None:
    """真实价格页 fixture 应解析出两个计价时段（官方页面现为 UTC 表达）。"""
    html = (FIXTURES / "pricing_page.html").read_text(encoding="utf-8")
    windows = parse_peak_windows(html)
    assert windows == [("01:00", "04:00"), ("06:00", "10:00")]


def test_parse_peak_windows_returns_none_for_garbage() -> None:
    """无可解析时段时返回 None，绝不伪造默认值。"""
    assert parse_peak_windows("<html>no numbers here</html>") is None
    assert parse_peak_windows("") is None
    # 只有时段但没有高峰上下文标记 → 不抽取。
    assert parse_peak_windows("价格 09:00 - 12:00") is None


def test_parse_supports_endash_and_hyphen() -> None:
    """应同时容忍 en-dash 与连字符两种写法。"""
    text = (
        "工作日 09:00–12:00、14:00 - 18:00 为高峰，其余为空闲。"
        "（peak hours are 09:00–12:00 and 14:00 - 18:00）"
    )
    assert parse_peak_windows(text) == [("09:00", "12:00"), ("14:00", "18:00")]


def test_parse_skips_invalid_ranges() -> None:
    """start>=end 或非法 HH 的窗口应被丢弃。"""
    text = "peak hours: 18:00 - 09:00 and 25:00 - 26:00 and 09:00-12:00"
    assert parse_peak_windows(text) == [("09:00", "12:00")]


def test_parse_deduplicates_windows() -> None:
    """重复出现的同一窗口只保留一个。"""
    text = "peak 09:00-12:00 and again 09:00–12:00"
    assert parse_peak_windows(text) == [("09:00", "12:00")]


# ------------------------------------------------------- detect_window_timezone
def test_detect_timezone_utc_marker() -> None:
    """含 UTC 标记的官方文本 → timezone=UTC。"""
    text = "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday."
    assert window_rules.detect_window_timezone(text) == "UTC"


def test_detect_timezone_beijing_marker() -> None:
    """含北京时间标记 → Asia/Shanghai。"""
    text = "工作日（北京时间）09:00–12:00、14:00 - 18:00 为高峰计价时段。"
    assert window_rules.detect_window_timezone(text) == "Asia/Shanghai"


def test_detect_timezone_default_fallback() -> None:
    """无显式时区标记时按官方中文页常规表述默认 Asia/Shanghai。"""
    assert window_rules.detect_window_timezone("高峰时段 09:00-12:00") == "Asia/Shanghai"


def test_detect_timezone_utc8_is_beijing() -> None:
    """UTC+8 偏移写法（北京时间）应识别为 Asia/Shanghai，而非 UTC。"""
    text = "Peak hours are in Beijing Time (UTC+8): 09:00-12:00 and 14:00-18:00."
    assert window_rules.detect_window_timezone(text) == "Asia/Shanghai"


# ------------------------------------------------------------- upsert_window_rule
def test_upsert_versions_and_source_snapshot(tmp_path: Path) -> None:
    """upsert 应递增版本、降级旧 active、记录 source 与 source_snapshot_id。"""
    conn = _conn(tmp_path)
    try:
        # 预置一条官方快照，供 source_snapshot_id 引用（满足外键约束）。
        cursor = conn.execute(
            "INSERT INTO official_snapshots "
            "(snapshot_type, source_url, fetched_at, status) "
            "VALUES ('pricing', 'https://example/pricing', '2026-10-12T00:00:00+00:00', 'ok')"
        )
        assert cursor.lastrowid is not None
        snapshot_id = int(cursor.lastrowid)

        v1 = upsert_window_rule(
            conn,
            _DEFAULT_RULE,
            source="official-pricing-page",
            source_snapshot_id=snapshot_id,
        )
        assert v1 == 1

        v2 = upsert_window_rule(
            conn,
            WindowRuleData(source="manual"),
            source="manual",
            source_snapshot_id=None,
        )
        assert v2 == 2

        rows = conn.execute(
            "SELECT version, active, source, source_snapshot_id, rule_json "
            "FROM window_rules ORDER BY version"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["version"] == 1
        assert rows[0]["active"] == 0
        assert rows[0]["source"] == "official-pricing-page"
        assert rows[0]["source_snapshot_id"] == snapshot_id
        assert rows[1]["version"] == 2
        assert rows[1]["active"] == 1
        assert rows[1]["source"] == "manual"
        assert rows[1]["source_snapshot_id"] is None
    finally:
        conn.close()


def test_get_active_returns_latest_active(tmp_path: Path) -> None:
    """get_active_window_rule 应返回最新 active 版本并正确反序列化。"""
    conn = _conn(tmp_path)
    try:
        upsert_window_rule(conn, _DEFAULT_RULE, source="official-pricing-page")
        upsert_window_rule(
            conn,
            WindowRuleData(timezone="UTC", peak_windows=(("13:00", "16:00"),), source="manual"),
            source="manual",
        )
        active = get_active_window_rule(conn)
        assert active is not None
        assert active.timezone == "UTC"
        assert active.peak_windows == (("13:00", "16:00"),)
        assert active.source == "manual"
    finally:
        conn.close()


def test_get_active_returns_none_on_empty(tmp_path: Path) -> None:
    """空表时应返回 None。"""
    conn = _conn(tmp_path)
    try:
        assert get_active_window_rule(conn) is None
    finally:
        conn.close()


# ----------------------------------------------------------------------- is_peak
def test_is_peak_beijing_morning_monday() -> None:
    """北京周一 09:30（UTC 01:30）应处于高峰。"""
    dt = _utc(year=2026, month=10, day=12, hour=1, minute=30)
    assert is_peak(dt, _DEFAULT_RULE) is True


def test_is_peak_beijing_noon_not_peak() -> None:
    """北京 12:30 不在任何高峰窗口 → False。"""
    dt = _utc(year=2026, month=10, day=12, hour=4, minute=30)  # 北京 12:30
    assert is_peak(dt, _DEFAULT_RULE) is False


def test_is_peak_saturday_false() -> None:
    """周六（UTC 2026-10-17）非工作日 → False。"""
    dt = _utc(year=2026, month=10, day=17, hour=1, minute=30)  # 北京周六 09:30
    assert is_peak(dt, _DEFAULT_RULE) is False


def test_is_peak_rule_none_returns_none() -> None:
    """规则为 None 时应返回 None（调用方采用保守默认）。"""
    dt = _utc(year=2026, month=10, day=12, hour=1, minute=30)
    assert is_peak(dt, None) is None


def test_is_peak_utc_input_converted_to_beijing() -> None:
    """给定 UTC 时刻，应在换算到北京时区后判断。

    北京 14:30（高峰窗口 14:00-18:00）= UTC 06:30。
    """
    dt = _utc(year=2026, month=10, day=12, hour=6, minute=30)  # 北京周一 14:30
    assert is_peak(dt, _DEFAULT_RULE) is True


def test_is_peak_afternoon_window() -> None:
    """北京周一 17:00（UTC 09:00）应在 14:00-18:00 窗口内。"""
    dt = _utc(year=2026, month=10, day=12, hour=9, minute=0)
    assert is_peak(dt, _DEFAULT_RULE) is True


# --------------------------------------------------------------------- is_workday
def test_is_workday_monday_true() -> None:
    dt = _utc(year=2026, month=10, day=12, hour=1, minute=0)  # 北京周一 09:00
    assert is_workday(dt, _DEFAULT_RULE) is True


def test_is_workday_saturday_false() -> None:
    dt = _utc(year=2026, month=10, day=17, hour=1, minute=0)  # 北京周六
    assert is_workday(dt, _DEFAULT_RULE) is False


def test_is_workday_utc_conversion() -> None:
    """UTC 周日深夜换算到北京已是周一 → 应视为工作日。"""
    # 2026-10-12 00:30 UTC = 北京 08:30 周一
    dt = _utc(year=2026, month=10, day=12, hour=0, minute=30)
    assert is_workday(dt, _DEFAULT_RULE) is True


def test_is_workday_rule_none_returns_none() -> None:
    assert is_workday(datetime(2026, 10, 12, 1, 0, tzinfo=UTC), None) is None


def test_window_rule_roundtrip_json() -> None:
    """WindowRuleData 的 JSON 序列化应可往返恢复一致。"""
    rule = WindowRuleData(
        timezone="UTC",
        workdays=(1, 2, 3),
        peak_windows=(("13:00", "16:00"),),
        source="manual",
    )
    restored = WindowRuleData.from_json(rule.to_json())
    assert restored == rule


def test_import_via_package() -> None:
    """collector 包应导出关键符号。"""
    assert window_rules.WindowRuleData is WindowRuleData
