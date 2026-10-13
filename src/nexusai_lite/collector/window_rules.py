"""官方时段规则解析与版本化 ``window_rules`` 管理（S1-02）。

从官方价格页解析高峰/空闲计价时段，写入可版本化的 ``window_rules`` 规则表，
作为**可配置的初始调度规则**（计价时段 ≠ 全局实时负载曲线，reference.md §1.1）。

关键原则：
- 解析失败时返回 ``None``，绝不伪造默认值当作权威数据。
- 每条规则记录来源（official-pricing-page / manual）与采集时间。
- 支持人工覆盖（source="manual"）并作为版本化配置管理（§7）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nexusai_lite.db import utcnow_iso

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

_MAX_HOUR = 23
_MAX_MINUTE = 59

# 匹配 HH:MM（00-23:00-59），含可选空格分隔的连字符/短横线/全角短横线。
_HOUR = r"(?:[01]\d|2[0-3])"
_TIME = rf"{_HOUR}:[0-5]\d"
_WINDOW_RE = re.compile(
    rf"({_TIME})\s*[-–—]\s*({_TIME})",
    re.IGNORECASE,
)
# 仅在包含「高峰 / peak」相关标记的文本片段中才抽取时段，降低误报。
_CONTEXT_RE = re.compile(
    r"(peak|高峰|peak hours|计价时段)",
    re.IGNORECASE,
)

_SOURCE_OFFICIAL = "official-pricing-page"
_SOURCE_MANUAL = "manual"

_UTC_MARKER_RE = re.compile(r"\bUTC\b(?!\s*[+-]\s*\d)")
_BEIJING_MARKER_RE = re.compile(r"北京时间|[Bb]eijing|UTC\s*\+\s*0?8\b|GMT\s*\+\s*0?8\b")
# 在「peak / 高峰」上下文前后各取该字符数做时区判定。
_TZ_CONTEXT_WINDOW = 400

_TIMEZONE_UTC = "UTC"
_TIMEZONE_BEIJING = "Asia/Shanghai"


@dataclass(frozen=True)
class WindowRuleData:
    """一条可配置的时段规则。

    Attributes:
        timezone: 规则使用的 IANA 时区（默认北京时间）。
        workdays: ISO 工作日集合（Mon=1..Sun=7），默认周一至周五。
        peak_windows: 高峰计价时段 [[HH:MM, HH:MM], ...]。窗口**不跨午夜**。
        source: 数据来源（official-pricing-page / manual）。
    """

    timezone: str = "Asia/Shanghai"
    workdays: tuple[int, ...] = (1, 2, 3, 4, 5)  # ISO Mon=1..Sun=7
    peak_windows: tuple[tuple[str, str], ...] = (
        ("09:00", "12:00"),
        ("14:00", "18:00"),
    )
    source: str = _SOURCE_OFFICIAL

    def to_json(self) -> str:
        """序列化为稳定的 JSON（用于 ``rule_json`` 列）。"""
        return json.dumps(
            {
                "timezone": self.timezone,
                "workdays": list(self.workdays),
                "peak_windows": [list(w) for w in self.peak_windows],
                "source": self.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> WindowRuleData:
        """从 JSON 反序列化并重建 :class:`WindowRuleData`。"""
        data = json.loads(raw)
        return cls(
            timezone=data["timezone"],
            workdays=tuple(int(d) for d in data["workdays"]),
            peak_windows=tuple((str(s), str(e)) for s, e in data["peak_windows"]),
            source=data["source"],
        )


def _parse_time(token: str) -> tuple[int, int] | None:
    """把 ``HH:MM`` 解析为 ``(hour, minute)``；非法返回 None。"""
    hour_s, minute_s = token.split(":")
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= _MAX_HOUR and 0 <= minute <= _MAX_MINUTE):
        return None
    return hour, minute


def parse_peak_windows(text: str) -> list[tuple[str, str]] | None:
    """从官方价格页文本中容错提取高峰/空闲计价时段。

    仅提取位于「peak / 高峰」上下文片段中的 ``HH:MM–HH:MM`` / ``HH:MM-HH:MM``
    时段对，兼容短横线 / 连字符 / 全角短横线与可选空格。校验 start<end 且
    HH 在 00-23。

    Returns:
        ``[(start, end), ...]`` 列表；**无可解析时段时返回 None**（绝不伪造默认值）。
    """
    # 只处理包含高峰上下文标记的行/片段，降低把无关 HH:MM 误当时段的概率。
    if not _CONTEXT_RE.search(text):
        return None

    found: list[tuple[str, str]] = []
    for match in _WINDOW_RE.finditer(text):
        start_raw = match.group(1)
        end_raw = match.group(2)
        start = _parse_time(start_raw)
        end = _parse_time(end_raw)
        if start is None or end is None:
            continue
        # 窗口不跨午夜：start 必须小于 end。
        if start[0] * 60 + start[1] >= end[0] * 60 + end[1]:
            continue
        window = (start_raw, end_raw)
        if window not in found:
            found.append(window)
    return found or None


def detect_window_timezone(text: str) -> str:
    """检测官方页面对高峰时段标注的时区。

    在「peak / 高峰」上下文的近邻文本中查找显式时区标记：
    - 出现 ``UTC`` → ``"UTC"``；
    - 出现 ``北京时间`` / ``Beijing`` → ``"Asia/Shanghai"``；
    - 均无标记时按官方中文价格页的常规表述默认 ``"Asia/Shanghai"``
      （该假设需定期对照官方页面复核，reference.md §1.1）。
    """
    for match in _CONTEXT_RE.finditer(text):
        segment = text[
            max(0, match.start() - _TZ_CONTEXT_WINDOW) : match.end() + _TZ_CONTEXT_WINDOW
        ]
        if _UTC_MARKER_RE.search(segment):
            return _TIMEZONE_UTC
        if _BEIJING_MARKER_RE.search(segment):
            return _TIMEZONE_BEIJING
    if _UTC_MARKER_RE.search(text):
        return _TIMEZONE_UTC
    return _TIMEZONE_BEIJING


def upsert_window_rule(
    conn: sqlite3.Connection,
    rule: WindowRuleData,
    *,
    source: str,
    source_snapshot_id: int | None = None,
) -> int:
    """写入一条新版本时段规则并使其激活。

    流程（``BEGIN IMMEDIATE`` 事务内）：
    1. 将当前所有 active 行降级为 inactive；
    2. 插入 ``MAX(version)+1`` 的新版本，active=1，source_snapshot_id 视情况记录。

    Args:
        conn: 数据库连接（调用方负责事务外的资源释放）。
        rule: 要写入的规则。
        source: 数据来源（official-pricing-page / manual）。
        source_snapshot_id: 关联的官方快照 id（无可选 None）。

    Returns:
        新版本号。
    """
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        version_row = conn.execute("SELECT MAX(version) AS m FROM window_rules").fetchone()
        next_version = (
            1
            if version_row is None or version_row["m"] is None
            else int(version_row["m"]) + 1
        )
        conn.execute(
            "UPDATE window_rules SET active=0 WHERE active=1",
        )
        conn.execute(
            "INSERT INTO window_rules "
            "(version, rule_json, source, source_snapshot_id, effective_at, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (next_version, rule.to_json(), source, source_snapshot_id, now, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return next_version


def get_active_window_rule(conn: sqlite3.Connection) -> WindowRuleData | None:
    """返回最新激活的时段规则；若无则返回 None。"""
    row = conn.execute(
        "SELECT rule_json FROM window_rules WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return WindowRuleData.from_json(row["rule_json"])


def is_peak(dt: datetime, rule: WindowRuleData | None) -> bool | None:
    """判断给定时刻是否处于计费高峰。

    - ``rule is None``：返回 None，由调用方采用保守默认（不推断为低压力）。
    - 非工作日 → False。
    - 否则把 ``dt`` 转换到规则时区，检查是否落在任一高峰窗口内。
      **窗口不跨午夜**（当前规则语义约束，见 docstring）。
    """
    if rule is None:
        return None
    if not is_workday(dt, rule):
        return False
    local = _in_rule_tz(dt, rule.timezone)
    minutes = local.hour * 60 + local.minute
    for start_raw, end_raw in rule.peak_windows:
        start = _to_minutes(start_raw)
        end = _to_minutes(end_raw)
        if start is not None and end is not None and start <= minutes < end:
            return True
    return False


def is_workday(dt: datetime, rule: WindowRuleData | None) -> bool | None:
    """判断给定时刻是否为规则定义的工作日。

    - ``rule is None``：返回 None。
    - 否则按规则时区的 ISO 星期数（Mon=1..Sun=7）是否属于 ``workdays``。
    """
    if rule is None:
        return None
    local = _in_rule_tz(dt, rule.timezone)
    return local.isoweekday() in rule.workdays


def _in_rule_tz(dt: datetime, timezone: str) -> datetime:
    """把（可能带时区或 naive 的）时刻转换到规则时区。

    naive 输入按 UTC 处理（本工具内部统一用 UTC 存储/传递时间戳）。
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=ZoneInfo("UTC"))
    return aware.astimezone(ZoneInfo(timezone))


def _to_minutes(token: str) -> int | None:
    """把 ``HH:MM`` 转为自午夜的分钟数；非法返回 None。"""
    parsed = _parse_time(token)
    if parsed is None:
        return None
    return parsed[0] * 60 + parsed[1]
