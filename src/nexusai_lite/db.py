"""SQLite 连接、迁移与初始化。

时间戳统一存储为 UTC ISO-8601 TEXT；主键为主 UUID4 字符串（除非标注 INTEGER
AUTOINCREMENT）。单机场景以 SQLite 事务保护状态一致性与配置变更。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO-8601 字符串，带 ``+00:00`` 时区后缀。"""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """打开一个配置好的 SQLite 连接。

    - ``journal_mode=WAL``：并发读写
    - ``foreign_keys=ON``：外键约束
    - ``busy_timeout=5000``：锁等待
    - ``row_factory=Row``：按列名访问
    - ``isolation_level=None``：显式事务（需要处使用 ``BEGIN IMMEDIATE``）

    调用方负责调用 :meth:`sqlite3.Connection.close`。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 显式事务控制
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """按文件名顺序应用 ``migrations/*.sql``，幂等。

    已应用版本记录在 ``schema_migrations`` 表中。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for migration in files:
        version = migration.stem
        if version in applied:
            continue
        sql = migration.read_text(encoding="utf-8")
        # 显式事务内逐条执行（executescript 会先 COMMIT，破坏事务包裹）。
        conn.execute("BEGIN IMMEDIATE")
        try:
            _execute_script_in_tx(conn, sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, utcnow_iso()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _execute_script_in_tx(conn: sqlite3.Connection, sql: str) -> None:
    """在打开的显式事务内逐条执行 SQL 语句（按 ``;`` 切分）。"""
    for statement in sql.split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)


def init_db(db_path: Path) -> None:
    """初始化数据库：创建父目录、连接、迁移，并在无配置时写入默认配置 v1。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        migrate(conn)
        _seed_default_config(conn)
    finally:
        conn.close()


def _seed_default_config(conn: sqlite3.Connection) -> None:
    """若 ``config_versions`` 为空，则写入默认配置 v1（status=active）。

    为避免循环导入，在函数内延迟导入 config。
    """
    from nexusai_lite import config as config_module  # noqa: PLC0415

    existing = conn.execute("SELECT COUNT(*) AS c FROM config_versions").fetchone()
    if existing is not None and existing["c"] > 0:
        return
    active = conn.execute("SELECT id FROM config_versions WHERE status='active'").fetchone()
    if active is not None:
        # 存在 active 配置但表非空（极少数不一致），不重复播种。
        return
    config_module.seed_if_empty(conn)
