"""配置管理最小实现测试（S1-09）。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.config import (
    ProtectedConfigError,
    activate_config,
    default_config,
    get_active_config,
    list_versions,
    propose_config,
    rollback_config,
    validate_config,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的内存/临时 SQLite 连接。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    c = db.connect(db_path_)
    yield c
    c.close()


def _audit_rows(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT event_type FROM audit_events ORDER BY id").fetchall()
    return [r["event_type"] for r in rows]


def test_default_validate() -> None:
    """默认配置应通过校验。"""
    cfg = default_config()
    assert cfg.windows.peak_windows == [["09:00", "12:00"], ["14:00", "18:00"]]
    assert cfg.provider.default_model == "deepseek-chat"
    assert cfg.pricing.table == {}
    assert cfg.budget.currency is None
    assert cfg.retention.log_retention_days == 90


def test_invalid_hh_mm_rejected() -> None:
    """非法 HH:MM 应被拒绝。"""
    payload = default_config().model_dump()
    payload["analysis"]["night_start"] = "25:00"
    with pytest.raises(ValueError, match="超出范围"):
        validate_config(payload)


def test_workdays_out_of_range_rejected() -> None:
    """workdays 超出 1..7 应被拒绝。"""
    payload = default_config().model_dump()
    payload["windows"]["workdays"] = [1, 8]
    with pytest.raises(ValueError, match="超出范围"):
        validate_config(payload)


def test_peak_window_start_ge_end_rejected() -> None:
    """peak 窗口 start>=end 应被拒绝。"""
    payload = default_config().model_dump()
    payload["windows"]["peak_windows"] = [["10:00", "09:00"]]
    with pytest.raises(ValueError, match="start>=end"):
        validate_config(payload)


def test_lifecycle_propose_activate_rollback(conn: sqlite3.Connection) -> None:
    """propose → activate → rollback 生命周期：3 版本状态与审计正确。"""
    # v1 已由 seed 创建（active）。提交两个新版本。
    base = default_config().model_dump()

    payload2 = json.loads(json.dumps(base))
    payload2["windows"]["peak_windows"] = [["08:00", "11:00"]]  # 与 v1 不同
    version2 = propose_config(conn, payload2, "调整高峰时段", created_by="alice")

    payload3 = json.loads(json.dumps(base))
    payload3["retention"]["log_retention_days"] = 30  # 与 v1 不同
    version3 = propose_config(conn, payload3, "缩短保留期", created_by="bob")

    assert version2 == 2
    assert version3 == 3

    # 尚未激活任何新版本，active 仍是 v1。
    active = get_active_config(conn)
    assert active.meta.version is None  # seed payload 不含 version
    assert active.retention.log_retention_days == 90

    # 激活 v2。
    activate_config(conn, version2)
    statuses = {
        r["version"]: r["status"]
        for r in conn.execute("SELECT version, status FROM config_versions").fetchall()
    }
    assert statuses[1] == "superseded"
    assert statuses[2] == "active"
    assert statuses[3] == "proposed"
    active2 = get_active_config(conn)
    assert active2.windows.peak_windows == [["08:00", "11:00"]]

    # 回滚到 v1。
    rollback_config(conn, 1)
    statuses = {
        r["version"]: r["status"]
        for r in conn.execute("SELECT version, status FROM config_versions").fetchall()
    }
    assert statuses[1] == "active"
    assert statuses[2] == "superseded"
    assert statuses[3] == "proposed"
    active_rolled = get_active_config(conn)
    assert active_rolled.windows.peak_windows == [["09:00", "12:00"], ["14:00", "18:00"]]

    # 审计事件：seed(ConfigActivated) + 2x ConfigProposed + ConfigActivated + rollback
    events = _audit_rows(conn)
    assert events.count("ConfigProposed") == 2
    assert events.count("ConfigActivated") >= 1


def test_diff_json_nonempty_on_change(conn: sqlite3.Connection) -> None:
    """配置变更时应产生非空 diff_json。"""
    base = default_config().model_dump()
    payload = json.loads(json.dumps(base))
    payload["retention"]["log_retention_days"] = 45
    version = propose_config(conn, payload, "改保留期")
    row = conn.execute(
        "SELECT diff_json FROM config_versions WHERE version=?", (version,)
    ).fetchone()
    assert row is not None
    diff = json.loads(row["diff_json"])
    assert diff != {}
    assert "retention.log_retention_days" in diff


def test_get_active_config_round_trips(conn: sqlite3.Connection) -> None:
    """get_active_config 应能 round-trip 已激活的 payload。"""
    base = default_config().model_dump()
    payload = json.loads(json.dumps(base))
    payload["provider"]["default_model"] = "deepseek-reasoner"
    version = propose_config(conn, payload, "换默认模型")
    activate_config(conn, version)
    active = get_active_config(conn)
    assert active.provider.default_model == "deepseek-reasoner"


def test_list_versions_shape(conn: sqlite3.Connection) -> None:
    """list_versions 应返回版本信息。"""
    versions = list_versions(conn)
    assert len(versions) == 1
    row = versions[0]
    assert set(row) >= {"id", "version", "status", "created_at", "effective_at", "change_reason"}
    assert row["version"] == 1


def test_protected_budget_change_requires_human_approval(conn: sqlite3.Connection) -> None:
    """未经人工批准的预算变更应被拒绝（受保护配置项）。"""
    payload = default_config().model_dump()
    payload["budget"]["daily_hard_limit"] = 100.0
    with pytest.raises(ProtectedConfigError, match="受保护"):
        propose_config(conn, payload, "改预算")


def test_protected_budget_change_with_approval_allowed(conn: sqlite3.Connection) -> None:
    """人工批准后允许修改受保护配置项。"""
    payload = default_config().model_dump()
    payload["budget"]["daily_hard_limit"] = 100.0
    version = propose_config(conn, payload, "改预算", human_approval=True)
    assert version == 2


def test_protected_sandbox_whitelist_concurrency_change_blocked_without_approval(
    conn: sqlite3.Connection,
) -> None:
    """未经人工审批修改沙箱/白名单/并发上限等安全策略应被拒绝。"""
    cases: list[tuple[str, dict[str, object]]] = [
        ("sandbox", {"allowed_commands": ["python", "bash"]}),
        ("whitelist", {"commands": ["python", "bash"]}),
        ("concurrency", {"max_inflight": 16}),
    ]
    for section, change in cases:
        payload = default_config().model_dump()
        payload[section] = change
        with pytest.raises(ProtectedConfigError, match="受保护"):
            propose_config(conn, payload, f"agent 企图修改 {section}")


def test_protected_sandbox_change_with_approval_allowed(conn: sqlite3.Connection) -> None:
    """人工批准后允许修改沙箱策略。"""
    payload = default_config().model_dump()
    payload["sandbox"] = {"allow_network": True}
    version = propose_config(conn, payload, "人工调整沙箱网络", human_approval=True)
    assert version >= 2


def test_default_config_has_behavior_sections() -> None:
    """默认配置应覆盖所有影响生产行为的 section，并使用安全默认值。"""
    cfg = default_config()

    assert cfg.concurrency.max_inflight == 4
    assert cfg.concurrency.conservative_inflight == 2
    assert cfg.concurrency.conservative_inflight <= cfg.concurrency.max_inflight

    # 沙箱默认最小权限：关闭出站网络、无工作区根目录、命令白名单非空
    assert cfg.sandbox.allow_network is False
    assert cfg.sandbox.workspace_roots == []
    assert cfg.sandbox.allowed_commands == ["python", "pytest", "ruff", "git"]

    assert cfg.whitelist.commands == ["python", "pytest", "ruff", "git"]
    assert cfg.whitelist.directories == []


def test_concurrency_non_positive_rejected() -> None:
    """并发数为非正数应被拒绝。"""
    payload = default_config().model_dump()
    payload["concurrency"]["max_inflight"] = 0
    with pytest.raises(ValueError, match="必须 > 0"):
        validate_config(payload)


def test_conservative_exceeds_max_rejected() -> None:
    """conservative_inflight 大于 max_inflight 应被拒绝。"""
    payload = default_config().model_dump()
    payload["concurrency"] = {"max_inflight": 2, "conservative_inflight": 5}
    with pytest.raises(ValueError, match="不能大于"):
        validate_config(payload)


def test_empty_command_rejected() -> None:
    """sandbox/whitelist 中空命令名应被拒绝。"""
    payload = default_config().model_dump()
    payload["sandbox"]["allowed_commands"] = ["python", ""]
    with pytest.raises(ValueError, match="空命令名"):
        validate_config(payload)

    payload2 = default_config().model_dump()
    payload2["whitelist"]["commands"] = ["   "]
    with pytest.raises(ValueError, match="空命令名"):
        validate_config(payload2)


def test_empty_whitelist_directory_rejected() -> None:
    """whitelist 中空目录路径应被拒绝。"""
    payload = default_config().model_dump()
    payload["whitelist"]["directories"] = [""]
    with pytest.raises(ValueError, match="空目录"):
        validate_config(payload)


def test_policy_changed_event_on_activate(conn: sqlite3.Connection) -> None:
    """activate 时应发布 PolicyChanged 事件并携带版本与原因。"""
    payload = default_config().model_dump()
    payload["concurrency"]["max_inflight"] = 8
    version = propose_config(conn, payload, "提高并发上限", human_approval=True)
    activate_config(conn, version)

    rows = conn.execute(
        "SELECT event_type, object_id, detail_json FROM audit_events "
        "WHERE event_type='PolicyChanged'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["object_id"] == str(version)
    detail = json.loads(rows[0]["detail_json"])
    assert detail["version"] == version
    assert detail["change_reason"] == "提高并发上限"


def test_policy_changed_event_on_rollback(conn: sqlite3.Connection) -> None:
    """rollback 时应发布 PolicyChanged 事件并携带版本与原因。"""
    payload = default_config().model_dump()
    payload["concurrency"]["max_inflight"] = 8
    version = propose_config(conn, payload, "提高并发上限", human_approval=True)
    activate_config(conn, version)
    rollback_config(conn, 1)

    rows = conn.execute(
        "SELECT event_type, object_id FROM audit_events WHERE event_type='PolicyChanged'"
    ).fetchall()
    assert len(rows) == 2  # activate + rollback 各一次
    assert {r["object_id"] for r in rows} == {"1", str(version)}


def test_rollback_invalid_target_no_side_effect(conn: sqlite3.Connection) -> None:
    """回滚到不存在的版本应抛错且不产生任何状态/审计副作用。"""
    before = list(conn.execute("SELECT status FROM config_versions").fetchall())
    before_events = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]

    with pytest.raises(ValueError, match="不存在"):
        rollback_config(conn, 999)

    after = list(conn.execute("SELECT status FROM config_versions").fetchall())
    after_events = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
    assert after == before
    assert after_events == before_events


def test_new_sections_round_trip(conn: sqlite3.Connection) -> None:
    """携带新 section 的配置应能 propose → activate → get_active_config round-trip。"""
    payload = default_config().model_dump()
    payload["concurrency"] = {"max_inflight": 6, "conservative_inflight": 3}
    payload["sandbox"] = {
        "allow_network": True,
        "workspace_roots": ["/home/user/dev"],
        "allowed_commands": ["python", "git"],
    }
    payload["whitelist"] = {"commands": ["python"], "directories": ["/home/user/dev/repo"]}
    version = propose_config(conn, payload, "启用受限网络与工作区", human_approval=True)
    activate_config(conn, version)

    active = get_active_config(conn)
    assert active.concurrency.max_inflight == 6
    assert active.sandbox.allow_network is True
    assert active.sandbox.workspace_roots == ["/home/user/dev"]
    assert active.whitelist.commands == ["python"]
    assert active.whitelist.directories == ["/home/user/dev/repo"]
