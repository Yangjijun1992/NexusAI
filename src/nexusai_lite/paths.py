"""数据目录与数据库路径解析。

路径可通过环境变量覆盖；仅负责定位，若目录不存在则按需创建。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "nexusai-lite"


def data_dir() -> Path:
    """返回数据目录，必要时创建。

    优先使用环境变量 ``NEXUSAI_DATA_DIR``，否则回退到 XDG 数据目录
    （``~/.local/share/nexusai-lite``）。
    """
    override = os.environ.get("NEXUSAI_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg).expanduser().resolve() if xdg else Path.home() / ".local" / "share"
        path = base / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """返回 SQLite 数据库文件路径。

    优先使用环境变量 ``NEXUSAI_DB_PATH``，否则为 ``data_dir()/"nexusai.db"``。
    """
    override = os.environ.get("NEXUSAI_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return data_dir() / "nexusai.db"
