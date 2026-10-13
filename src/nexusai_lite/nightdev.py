"""夜间/非工作日授权窗口自动启动开发任务（S3-01）。

对应 reference.md F7「夜间/非工作日自动开发（★阶段3）」的需求主线：在允许的
夜间/非工作日窗口内，自动创建并**入队**已授权开发任务，交由调度器/执行器消费
（本模块**不执行任何东西，也不自动启动**）。

- **授权窗口判定（安全关键）：**
  - ``is_allowed_window`` 是放行决策的安全门。**当活跃时段规则缺失（``rule is
    None``）时返回 False**（保守：未授权时间不启动）。绝不因为「空闲计价时段」
    而错误推断为无风险窗口（reference.md §1.1：计价时段 ≠ 全局实时负载；§0.3
    失败时安全降级）。普通工作日的白天（即便是空闲计价时段，例如 13:00）也
    **不是**夜间开发授权窗口。
  - 允许场景：计入 ``holidays`` 的假日日期、非工作日（规则定义的周末）、或落在
    ``[night_start, night_end)`` 夜间窗口（支持跨午夜，如 22:00–07:00）。
- **每日幂等创建：** 每个 spec 按 ``nightdev:{name}:{local_date}`` 作为幂等键，
  同一授权窗口内重复调用 :meth:`NightDevPlanner.plan` 不会产生重复任务（S2-01
  幂等创建，§12 规则 3 有限重试 / 去重）。
- **配置缺口（显式声明）：** 现阶段**尚无 ``nightdev`` 配置节**。夜间窗口
  ``night_start`` / ``night_end`` / ``holidays``、以及 spec 集合，均由调用方提供
  （可传 :func:`nexusai_lite.config.get_active_config(conn).analysis` 的值）；
  待建立独立配置节后再收敛。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nexusai_lite.collector.window_rules import WindowRuleData, get_active_window_rule
from nexusai_lite.tasks import TaskService, TaskStatus

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from pathlib import Path

_TZ_UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class NightDevSpec:
    """一个已授权夜间开发任务的规格（需执行内容由调用方/S3-02 执行器提供）。

    Attributes:
        name: 任务唯一名（用于构造幂等键，同窗口内应稳定）。
        task_type: 任务类型（写入 ``tasks.task_type``）。
        repo_path: 目标仓库路径（供执行器使用）。
        priority: 任务优先级（默认 ``normal``）。
        estimated_cost: 预估成本（无则 None）。
        max_cost: 成本硬上限（无则 None，§12 规则 9）。
        max_execution_seconds: 单次执行时长上限（默认 3600）。
    """

    name: str
    task_type: str
    repo_path: Path
    priority: str = "normal"
    estimated_cost: float | None = None
    max_cost: float | None = None
    max_execution_seconds: int = 3600


@dataclass(frozen=True)
class PlannedNightTask:
    """一次 ``plan`` 后为某个 spec 建立的计划任务结果。"""

    name: str
    task_id: str
    created: bool


def _local_in_rule(dt: datetime, rule: WindowRuleData) -> datetime:
    """把（可能带时区或 naive 的）时刻换算到规则时区。

    与 :func:`nexusai_lite.collector.window_rules` 语义一致：naive 输入按 UTC
    处理（本工具内部统一用 UTC 存储/传递时间戳）。
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=_TZ_UTC)
    return aware.astimezone(ZoneInfo(rule.timezone))


def _to_minutes(token: str) -> int:
    """把 ``HH:MM`` 转为自午夜的分钟数。"""
    hour_s, minute_s = token.split(":")
    return int(hour_s) * 60 + int(minute_s)


def _in_night_window(local: datetime, night_start: str, night_end: str) -> bool:
    """判断时刻是否落在 ``[night_start, night_end)`` 夜间窗口（支持跨午夜）。

    - ``night_start <= night_end``：直接比较（如 00:00–06:00）。
    - ``night_start > night_end``：跨午夜（如 22:00–07:00），窗口为
      ``[start, 24:00) ∪ [00:00, end)``。
    - ``night_start == night_end``：空窗口（保守，不视为授权时段）。
    """
    start = _to_minutes(night_start)
    end = _to_minutes(night_end)
    minutes = local.hour * 60 + local.minute
    if start > end:  # 跨午夜
        return minutes >= start or minutes < end
    return start <= minutes < end


def is_allowed_window(
    dt: datetime,
    rule: WindowRuleData | None,
    *,
    night_start: str,
    night_end: str,
    holidays: Sequence[str] = (),
) -> bool:
    """判断给定时刻是否处于夜间开发授权窗口（**安全关键路径**）。

    仅在以下任一情形下放行（按规则时区的本地日期/本地时刻判定）：

    1. 本地日期在 ``holidays`` 中（格式 ``YYYY-MM-DD``）；
    2. 本地日期为规则定义的非工作日（周末）；
    3. 本地时刻落在 ``[night_start, night_end)`` 夜间窗口（支持跨午夜）。

    **保守策略（必须在规则缺失时收紧）：** ``rule is None`` 直接返回 False。
    未授权时间不启动；也不得因「空闲计价时段」（如工作日下午 13:00）错误推断为
    无风险窗口。普通工作日白天一律不是授权窗口。
    """
    if rule is None:
        return False
    local = _local_in_rule(dt, rule)
    local_date = local.date()
    if local_date.isoformat() in holidays:
        return True
    if local.isoweekday() not in rule.workdays:
        return True
    return _in_night_window(local, night_start, night_end)


class NightDevPlanner:
    """在授权窗口内为已授权 spec 创建并入队夜间开发任务（S3-01）。

    - **不执行任何东西**：只负责创建/入队，调度器与执行器（S3-02，另一 Agent
      负责）消费这些 ``QUEUED`` 任务。
    - **每日幂等**：同一授权窗口内重复调用 :meth:`plan` 不产生重复任务。
    - 配置作为参数注入（当前无 ``nightdev`` 配置节，见模块 docstring）。

    Args:
        conn: 数据库连接（调用方负责释放）。
        specs: 已授权开发任务规格集合。
        night_start: 夜间窗口起始 ``HH:MM``（默认 22:00）。
        night_end: 夜间窗口结束 ``HH:MM``（默认 07:00）。
        holidays: 假日日期 ``YYYY-MM-DD`` 序列（默认空）。
        now_fn: 注入时钟（确定性测试用）；默认取当前 UTC 时间。
    """

    def __init__(  # noqa: PLR0913 - 构造参数即调度窗口配置集合，属领域契约
        self,
        conn: sqlite3.Connection,
        *,
        specs: Sequence[NightDevSpec],
        night_start: str = "22:00",
        night_end: str = "07:00",
        holidays: Sequence[str] = (),
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化：固定窗口参数与任务服务（时钟可由调用方注入）。"""
        self._conn = conn
        self._specs = tuple(specs)
        self._night_start = night_start
        self._night_end = night_end
        self._holidays = tuple(holidays)
        self._now_fn = now_fn
        self._tasks = TaskService(conn)

    def _now(self, now: datetime | None) -> datetime:
        """解析当前时刻：显式参数 > 注入时钟 > UTC 现在。"""
        if now is not None:
            return now
        if self._now_fn is not None:
            return self._now_fn()
        return datetime.now(UTC)

    def plan(self, now: datetime | None = None) -> list[PlannedNightTask]:
        """若处于授权窗口，则为每个 spec 创建/入队任务并返回其结果。

        不在授权窗口（或活跃规则缺失）时返回空列表（不创建任何任务）。

        Returns:
            各 spec 对应的计划结果；同一 spec 幂等去重后不重复创建
            （``created=False`` 且 task_id 与既有任务一致）。
        """
        now_dt = self._now(now)
        rule = get_active_window_rule(self._conn)
        if not is_allowed_window(
            now_dt,
            rule,
            night_start=self._night_start,
            night_end=self._night_end,
            holidays=self._holidays,
        ):
            return []
        if rule is None:  # is_allowed_window 已保守拒绝；此处仅为类型收窄
            return []
        local_date = _local_in_rule(now_dt, rule).date().isoformat()

        planned: list[PlannedNightTask] = []
        for spec in self._specs:
            idempotency_key = f"nightdev:{spec.name}:{local_date}"
            pre = self._conn.execute(
                "SELECT task_id FROM tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            task = self._tasks.create_task(
                task_type=spec.task_type,
                idempotency_key=idempotency_key,
                priority=spec.priority,
                estimated_cost=spec.estimated_cost,
                max_cost=spec.max_cost,
                max_execution_seconds=spec.max_execution_seconds,
            )
            created = pre is None
            if created and task.status == TaskStatus.PENDING:
                # 仅新任务入队；去重（既有任务可能已 QUEUED/进行中）不重复入队，
                # 避免对已 QUEUED 任务触发非法转换。
                self._tasks.queue(task.task_id)
            planned.append(
                PlannedNightTask(
                    name=spec.name,
                    task_id=task.task_id,
                    created=created,
                )
            )
        return planned
