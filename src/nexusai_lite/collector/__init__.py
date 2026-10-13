"""官方信息采集器（S1-01 / S1-02）。

本模块负责采集 DeepSeek 官方公开信息（官方价格页 + 服务状态页），并从官方
资料中解析高峰/空闲计价时段，生成可配置的初始调度规则 ``window_rules``。

数据边界（reference.md §1.1 / §1.3）：官方公开信息与自身调用数据分开存储。
若抓取失败或内容无法解析，一律记录为失败，绝不伪造默认值当作权威数据。
官方计价时段仅是「计价时段定义」、可配置的初始调度规则，不等于全局实时负载曲线。
"""

from nexusai_lite.collector.official import OfficialCollector, SnapshotResult
from nexusai_lite.collector.window_rules import (
    WindowRuleData,
    get_active_window_rule,
    is_peak,
    is_workday,
    parse_peak_windows,
    upsert_window_rule,
)

__all__ = [
    "OfficialCollector",
    "SnapshotResult",
    "WindowRuleData",
    "get_active_window_rule",
    "is_peak",
    "is_workday",
    "parse_peak_windows",
    "upsert_window_rule",
]
