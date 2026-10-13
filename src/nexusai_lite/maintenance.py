"""维护入口：崩溃恢复与过期清理（§3 / §12 规则 3）。

:func:`run_maintenance` 按固定顺序执行一组幂等维护操作，用于周期性运行
（建议 cron / systemd timer）：

1. :meth:`BudgetGuard.from_config` 从当前生效配置播种 ``platform`` 账户限额。
2. :meth:`BudgetGuard.expire_stale` 把已过 TTL 的活跃预留标记为过期，归还预算容量。
3. :meth:`TaskService.recover` 保守崩溃恢复：超期 ``RUNNING`` 转 ``UNKNOWN``、
   超截止时间的非终态转 ``EXPIRED``（不盲目重试）。
4. :meth:`TaskService.expire_overdue` 把超过截止时间的非终态任务转 ``EXPIRED``。

满足 §3 崩溃恢复，并配合执行器端到端保证「不留悬挂预留 / 卡在 RUNNING」。
本模块只依赖标准库与现有 ``nexusai_lite`` 模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexusai_lite.budget import BudgetGuard
from nexusai_lite.config import get_active_config
from nexusai_lite.tasks import TaskService

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """一次维护操作的统计报告。

    Args:
        recovered: 被恢复（``RUNNING`` → ``UNKNOWN``）的任务数。
        expired: 被转为 ``EXPIRED`` 的任务数。
        stale_reservations: 已过 TTL 并被标记过期的预算预留数。
    """

    recovered: int
    expired: int
    stale_reservations: int


def run_maintenance(conn: sqlite3.Connection) -> MaintenanceReport:
    """执行一次维护：恢复崩溃任务、过期超期任务、清理陈旧预留。

    操作顺序固定：播种预算限制 → 刷新过期预留 → 恢复任务 → 过期任务。
    全部幂等，可安全周期性重复调用。
    """
    guard = BudgetGuard(conn)
    guard.from_config(get_active_config(conn))
    stale_reservations = guard.expire_stale()

    tasks = TaskService(conn)
    recovered = len(tasks.recover())
    expired = len(tasks.expire_overdue())

    return MaintenanceReport(
        recovered=recovered,
        expired=expired,
        stale_reservations=stale_reservations,
    )
