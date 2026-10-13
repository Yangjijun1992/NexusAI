"""数据库初始化/迁移/默认配置测试（S1-08）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexusai_lite import db, paths

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    import pytest

EXPECTED_TABLES = {
    "schema_migrations",
    "tasks",
    "task_runs",
    "task_attempts",
    "api_request_logs",
    "cost_records",
    "official_snapshots",
    "window_rules",
    "schedule_decisions",
    "config_versions",
    "audit_events",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_init_creates_all_tables(tmp_path: Path) -> None:
    """init_db 后应创建全部期望表。"""
    db_path_ = tmp_path / "sub" / "nexusai.db"
    db.init_db(db_path_)
    conn = db.connect(db_path_)
    try:
        names = _table_names(conn)
    finally:
        conn.close()
    assert EXPECTED_TABLES.issubset(names)


def test_migration_idempotent(tmp_path: Path) -> None:
    """迁移应幂等：init 两次不报错且迁移版本唯一。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    db.init_db(db_path_)
    conn = db.connect(db_path_)
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        versions = [row["version"] for row in rows]
        assert len(versions) == len(set(versions))
        assert "001_initial" in versions
    finally:
        conn.close()


def test_wal_enabled(tmp_path: Path) -> None:
    """连接应启用 WAL journal mode。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    conn = db.connect(db_path_)
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        mode = row[0]
    finally:
        conn.close()
    assert mode == "wal"


def test_seed_config_v1_active(tmp_path: Path) -> None:
    """初始 DB 应存在 active 的默认配置 v1。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    conn = db.connect(db_path_)
    try:
        row = conn.execute(
            "SELECT version, status FROM config_versions WHERE version=1"
        ).fetchone()
        assert row is not None
        assert row["version"] == 1
        assert row["status"] == "active"
    finally:
        conn.close()


def test_utcnow_iso_has_timezone() -> None:
    """utcnow_iso 应返回带时区信息的 ISO-8601 UTC 字符串。"""
    value = db.utcnow_iso()
    assert value.endswith(("+00:00", "Z"))


def test_data_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NEXUSAI_DATA_DIR 应覆盖默认数据目录。"""
    target = tmp_path / "custom"
    monkeypatch.setenv("NEXUSAI_DATA_DIR", str(target))
    assert paths.data_dir() == target.resolve()
    assert target.exists()


def test_db_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NEXUSAI_DB_PATH 应覆盖默认数据库路径。"""
    target = tmp_path / "custom.db"
    monkeypatch.setenv("NEXUSAI_DB_PATH", str(target))
    monkeypatch.setenv("NEXUSAI_DATA_DIR", str(tmp_path / "data"))
    assert paths.db_path() == target.resolve()
