"""版本化配置管理（S1-09）。

使用 Pydantic v2 定义配置模型，后端以 ``config_versions`` + ``audit_events``
存储版本与审计。所有写入都在 ``BEGIN IMMEDIATE`` 事务内完成。

注意：安全策略、预算硬上限与 Agent 自身权限的修改须经人工审批，只有人工调用
的 CLI 路径可以 activate；**禁止 Agent 自行修改**。
"""

from __future__ import annotations

import hashlib
import json
import typing
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexusai_lite.db import utcnow_iso

if typing.TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

DEFAULT_CONFIG_ID = "nexusai-lite"
#: 受保护配置项：预算硬上限、沙箱策略、命令/目录白名单、并发上限。
#: 这些属于安全策略（§7/§12 规则 12），Agent（human_approval=False）不得自行修改，
#: 仅人工（human_approval=True）可变更。
_PROTECTED_KEYS = ("budget", "sandbox", "whitelist", "concurrency")
_MIN_WORKDAY = 1
_MAX_WORKDAY = 7
_MAX_HOUR = 23
_MAX_MINUTE = 59


class CollectorConfig(BaseModel):
    """官方信息采集器配置。"""

    model_config = ConfigDict(frozen=True)

    pricing_url: str = "https://api-docs.deepseek.com/quick_start/pricing"
    status_url: str = "https://status.deepseek.com/"
    timeout_seconds: int = 15
    user_agent: str = "nexusai-lite/0.1 (local)"


class WindowsConfig(BaseModel):
    """时段规则：官方高峰/空闲计价时段（可配置的初始调度规则，不等于全局负载）。"""

    model_config = ConfigDict(frozen=True)

    timezone: str = "Asia/Shanghai"
    workdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])  # ISO Mon=1
    # 官方计价时段，需定期复核（source=official-pricing-page）
    peak_windows: list[list[str]] = Field(
        default_factory=lambda: [["09:00", "12:00"], ["14:00", "18:00"]]
    )
    source: str = "official-pricing-page"

    @field_validator("workdays")
    @classmethod
    def _check_workdays(cls, v: list[int]) -> list[int]:
        for day in v:
            if not _MIN_WORKDAY <= day <= _MAX_WORKDAY:
                msg = f"workday {day} 超出范围 {_MIN_WORKDAY}..{_MAX_WORKDAY}"
                raise ValueError(msg)
        return v

    @field_validator("peak_windows")
    @classmethod
    def _check_peak_windows(cls, v: list[list[str]]) -> list[list[str]]:
        for start, end in v:
            _check_hh_mm(start, "peak start")
            _check_hh_mm(end, "peak end")
            if start >= end:
                msg = f"peak window start>=end: {start} >= {end}"
                raise ValueError(msg)
        return v


class AnalysisConfig(BaseModel):
    """时段规律分析配置。"""

    model_config = ConfigDict(frozen=True)

    night_start: str = "22:00"
    night_end: str = "07:00"
    min_sample: int = 20
    holidays: list[str] = Field(default_factory=list)  # "YYYY-MM-DD"，手工维护

    @field_validator("night_start", "night_end")
    @classmethod
    def _check_hh_mm(cls, v: str) -> str:
        _check_hh_mm(v, "time")
        return v


class ProviderConfig(BaseModel):
    """Provider 调用配置。"""

    model_config = ConfigDict(frozen=True)

    default_model: str = "deepseek-chat"
    models_whitelist: list[str] = Field(
        default_factory=lambda: ["deepseek-chat", "deepseek-reasoner"]
    )
    base_url: str = "https://api.deepseek.com"
    request_timeout_seconds: int = 60
    max_retries: int = 2
    retry_backoff_base_seconds: float = 1.0
    retry_backoff_max_seconds: float = 30.0
    allow_retry_on_timeout: bool = False


class PricingRow(BaseModel):
    """单个模型的价格条目（每百万 token）。"""

    model_config = ConfigDict(frozen=True)

    input_per_million: float
    output_per_million: float
    currency: str


class PricingConfig(BaseModel):
    """价格表与计价版本。

    价格表初始为空（``table={}``），成本估算在官方数据采集前返回未知；
    请勿在此填入臆造价格。
    """

    model_config = ConfigDict(frozen=True)

    pricing_version: str = "unverified-seed"
    table: dict[str, PricingRow] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    """预算阈值（均为待确认 blankers，机制字段保留）。

    币种与数值待人工确认；未确认前为 None，表示未设置上限，成本估算返回未知。
    """

    model_config = ConfigDict(frozen=True)

    currency: str | None = None
    daily_hard_limit: float | None = None
    daily_soft_limit: float | None = None
    per_task_hard_limit: float | None = None


class RetentionConfig(BaseModel):
    """数据保留与日志脱敏配置。"""

    model_config = ConfigDict(frozen=True)

    log_retention_days: int = 90
    store_full_prompt: bool = False

    @field_validator("log_retention_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            msg = "log_retention_days 必须 > 0"
            raise ValueError(msg)
        return v


class ConcurrencyConfig(BaseModel):
    """并发上限配置（§7，可配置初始值；未确认容量时不虚构固定 QPS）。"""

    model_config = ConfigDict(frozen=True)

    max_inflight: int = 4
    conservative_inflight: int = 2

    @field_validator("max_inflight", "conservative_inflight")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            msg = f"并发数必须 > 0，收到 {v}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _conservative_le_max(self) -> ConcurrencyConfig:
        if self.conservative_inflight > self.max_inflight:
            msg = (
                f"conservative_inflight({self.conservative_inflight}) "
                f"不能大于 max_inflight({self.max_inflight})"
            )
            raise ValueError(msg)
        return self


class SandboxConfig(BaseModel):
    """沙箱策略（§4.1/§7）：默认最小权限、默认关闭出站网络。"""

    model_config = ConfigDict(frozen=True)

    allow_network: bool = False
    workspace_roots: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(
        default_factory=lambda: ["python", "pytest", "ruff", "git"]
    )

    @field_validator("workspace_roots")
    @classmethod
    def _non_blank_roots(cls, v: list[str]) -> list[str]:
        for root in v:
            if not root.strip():
                msg = "workspace_roots 不能包含空路径"
                raise ValueError(msg)
        return v

    @field_validator("allowed_commands")
    @classmethod
    def _non_blank_commands(cls, v: list[str]) -> list[str]:
        for cmd in v:
            if not cmd.strip():
                msg = "sandbox.allowed_commands 不能包含空命令名"
                raise ValueError(msg)
        return v


class WhitelistConfig(BaseModel):
    """任务白名单（§7）：命令与目录 allowlist。"""

    model_config = ConfigDict(frozen=True)

    commands: list[str] = Field(
        default_factory=lambda: ["python", "pytest", "ruff", "git"]
    )
    directories: list[str] = Field(default_factory=list)

    @field_validator("commands")
    @classmethod
    def _non_blank_commands(cls, v: list[str]) -> list[str]:
        for cmd in v:
            if not cmd.strip():
                msg = "whitelist.commands 不能包含空命令名"
                raise ValueError(msg)
        return v

    @field_validator("directories")
    @classmethod
    def _non_blank_dirs(cls, v: list[str]) -> list[str]:
        for directory in v:
            if not directory.strip():
                msg = "whitelist.directories 不能包含空目录路径"
                raise ValueError(msg)
        return v


class MetaConfig(BaseModel):
    """配置元信息。"""

    model_config = ConfigDict(frozen=True)

    config_id: str = DEFAULT_CONFIG_ID
    version: int | None = None  # 由存储层设置


class AppConfig(BaseModel):
    """应用根配置：各 section 的集合。"""

    model_config = ConfigDict(frozen=True)

    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    windows: WindowsConfig = Field(default_factory=WindowsConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    meta: MetaConfig = Field(default_factory=MetaConfig)

    @model_validator(mode="after")
    def _night_bounds(self) -> AppConfig:
        if self.analysis.night_start == self.analysis.night_end:
            msg = "night_start 不能等于 night_end"
            raise ValueError(msg)
        return self

    def model_dump_canonical(self) -> str:
        """输出稳定的 canonical JSON（用于 checksum），排除 version 字段。"""
        data = self.model_dump(exclude={"meta": {"version"}})
        return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _check_hh_mm(value: str, label: str) -> None:
    """校验 ``HH:MM`` 格式（00-23:00-59）。"""
    try:
        hour_s, minute_s = value.split(":")
        hour, minute = int(hour_s), int(minute_s)
    except (ValueError, AttributeError) as exc:  # 拆分/转换失败
        msg = f"{label} 不是合法 HH:MM: {value!r}"
        raise ValueError(msg) from exc
    if not (0 <= hour <= _MAX_HOUR and 0 <= minute <= _MAX_MINUTE):
        msg = f"{label} 超出范围: {value!r}"
        raise ValueError(msg)


def default_config() -> AppConfig:
    """返回默认配置。"""
    return AppConfig()


def _config_from_payload(payload: Mapping[str, Any]) -> AppConfig:
    """把 dict 载荷解析为 AppConfig（含校验）；失败抛出 Pydantic 校验异常。"""
    return AppConfig.model_validate(dict(payload))


def validate_config(data: Mapping[str, Any]) -> AppConfig:
    """静态校验一份配置 dict，返回解析后的 AppConfig。

    Pydantic 负责类型与结构的校验，``app`` 级校验负责跨字段约束
    （peak 窗口、workdays、HH:MM、night 边界、retention>0）。
    """
    return _config_from_payload(data)


def _canonical_checksum(payload: Mapping[str, Any]) -> str:
    """对 canonical 化后的配置计算 sha256 checksum。

    使用与 :meth:`AppConfig.model_dump_canonical` 一致的序列化，忽略 version。
    """
    cfg = _config_from_payload(payload)
    return hashlib.sha256(cfg.model_dump_canonical().encode("utf-8")).hexdigest()


def _diff_json(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, list[Any]]:
    """计算两份配置的简单 JSON 路径差异：``{path: [old_value, new_value]}``。"""
    if old is None:
        return {}
    diff: dict[str, list[Any]] = {}

    def walk(o: Mapping[str, Any], n: Mapping[str, Any], prefix: str) -> None:
        for key in set(o) | set(n):
            path = f"{prefix}.{key}" if prefix else key
            o_val = o.get(key)
            n_val = n.get(key)
            if isinstance(o_val, dict) and isinstance(n_val, dict):
                walk(o_val, n_val, path)
            elif o_val != n_val:
                diff[path] = [o_val, n_val]

    walk(old, new, "")
    return diff


class ProtectedConfigError(ValueError):
    """受保护配置项（预算硬上限/安全策略）在未获人工批准时被修改。"""


def _reject_protected_change(diff: Mapping[str, Any], *, human_approval: bool) -> None:
    """拒绝未经人工批准的受保护配置项变更（reference.md §7/§12：禁止 Agent 自行修改）。"""
    if human_approval:
        return
    for path in diff:
        if any(path == key or path.startswith(f"{key}.") for key in _PROTECTED_KEYS):
            msg = f"受保护配置项需人工批准，不得自行修改: {path}"
            raise ProtectedConfigError(msg)


def get_active_config(conn: sqlite3.Connection) -> AppConfig:
    """返回当前生效配置；若无则回退默认配置（并校验）。"""
    row = conn.execute(
        "SELECT payload_json FROM config_versions WHERE status='active' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return default_config()
    payload = json.loads(row["payload_json"])
    return validate_config(payload)


def list_versions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """列出配置版本信息（id, version, status, timestamps, reason）。"""
    rows = conn.execute(
        "SELECT id, version, status, created_at, effective_at, change_reason "
        "FROM config_versions ORDER BY version"
    ).fetchall()
    return [dict(row) for row in rows]


def _next_version(conn: sqlite3.Connection) -> int:
    """计算下一个配置版本号。"""
    row = conn.execute(
        "SELECT MAX(version) AS m FROM config_versions WHERE config_id=?", (DEFAULT_CONFIG_ID,)
    ).fetchone()
    return 1 if row is None or row["m"] is None else int(row["m"]) + 1


def propose_config(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    change_reason: str,
    created_by: str = "user",
    *,
    human_approval: bool = False,
) -> int:
    """提交一份新配置（status=proposed），返回新版本号。

    流程：校验 → 校验受保护项 → checksum → 计算 diff → 写入 ``config_versions`` →
    审计 ``ConfigProposed``。事务内执行。受保护配置项需 ``human_approval=True``。
    """
    cfg = validate_config(payload)
    active = conn.execute(
        "SELECT version, payload_json FROM config_versions WHERE status='active' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    old_payload = json.loads(active["payload_json"]) if active else None
    diff = _diff_json(old_payload, cfg.model_dump())
    _reject_protected_change(diff, human_approval=human_approval)
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _next_version(conn)
        checksum = _canonical_checksum(cfg.model_dump())
        diff_json = json.dumps(diff, sort_keys=True) if diff else None
        now = utcnow_iso()
        conn.execute(
            "INSERT INTO config_versions "
            "(config_id, version, payload_json, checksum, status, diff_json, "
            " created_by, created_at, change_reason, previous_version, rollback_supported) "
            "VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, 1)",
            (
                DEFAULT_CONFIG_ID,
                version,
                cfg.model_dump_json(exclude={"meta": {"version"}}),
                checksum,
                diff_json,
                created_by,
                now,
                change_reason,
                active["version"] if active else None,
            ),
        )
        _audit(conn, "ConfigProposed", created_by, "config_versions", str(version))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return version


def _perform_activate(conn: sqlite3.Connection, version: int) -> str:
    """在已开启的事务内把指定版本设为 active、降级旧 active，返回其 change_reason。

    调用方负责 ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` 与审计事件写入。
    """
    now = utcnow_iso()
    conn.execute(
        "UPDATE config_versions SET status='superseded', effective_at=? "
        "WHERE status='active'",
        (now,),
    )
    conn.execute(
        "UPDATE config_versions SET status='active', effective_at=? WHERE version=?",
        (now, version),
    )
    row = conn.execute(
        "SELECT change_reason FROM config_versions WHERE version=?", (version,)
    ).fetchone()
    return row["change_reason"] if row is not None and row["change_reason"] is not None else ""


def _audit_policy_changed(conn: sqlite3.Connection, version: int, change_reason: str) -> None:
    """写入 ``PolicyChanged`` 审计事件，携带版本与变更原因（reference.md §7）。

    配置生效（激活/回滚）时发布；须在事务内调用。
    """
    detail = json.dumps({"version": version, "change_reason": change_reason}, sort_keys=True)
    _audit(conn, "PolicyChanged", "user", "config_versions", str(version), detail_json=detail)


def activate_config(conn: sqlite3.Connection, version: int) -> None:
    """激活指定版本：将旧 active 降级为 superseded，设置 effective_at，审计

    ``ConfigActivated`` 与 ``PolicyChanged``。
    """
    row = conn.execute(
        "SELECT id, status FROM config_versions WHERE version=?", (version,)
    ).fetchone()
    if row is None:
        msg = f"配置版本 {version} 不存在"
        raise ValueError(msg)
    conn.execute("BEGIN IMMEDIATE")
    try:
        reason = _perform_activate(conn, version)
        _audit(conn, "ConfigActivated", "user", "config_versions", str(version))
        _audit_policy_changed(conn, version, reason)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def rollback_config(conn: sqlite3.Connection, to_version: int) -> None:
    """回滚到某个旧版本：重新激活该版本、降级当前 active，并审计

    ``ConfigRolledBack`` 与 ``PolicyChanged``。目标版本不存在时抛出
    ``ValueError``，事务回滚不产生任何副作用（幂等、可重试）。
    """
    row = conn.execute(
        "SELECT id, status FROM config_versions WHERE version=?", (to_version,)
    ).fetchone()
    if row is None:
        msg = f"配置版本 {to_version} 不存在"
        raise ValueError(msg)
    conn.execute("BEGIN IMMEDIATE")
    try:
        reason = _perform_activate(conn, to_version)
        _audit(conn, "ConfigRolledBack", "user", "config_versions", str(to_version))
        _audit_policy_changed(conn, to_version, reason)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _audit(  # noqa: PLR0913, PLR0917 - 审计写入需要完整的事件字段（类型/主体/对象/时间/明细）
    conn: sqlite3.Connection,
    event_type: str,
    subject: str,
    object_type: str,
    object_id: str,
    detail_json: str | None = None,
) -> None:
    """写入一条审计事件（须在事务内调用），时间戳取当前 UTC。"""
    conn.execute(
        "INSERT INTO audit_events (event_type, subject, object_type, object_id, at, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_type, subject, object_type, object_id, utcnow_iso(), detail_json),
    )


def seed_if_empty(conn: sqlite3.Connection) -> None:
    """在表为空时写入默认配置 v1（status=active）并审计（须在调用方事务内或自动提交）。"""
    cfg = default_config()
    checksum = hashlib.sha256(cfg.model_dump_canonical().encode("utf-8")).hexdigest()
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO config_versions "
            "(config_id, version, payload_json, checksum, status, created_by, created_at, "
            " change_reason, previous_version, rollback_supported) "
            "VALUES (?, 1, ?, ?, 'active', 'system', ?, 'initial seed', NULL, 1)",
            (DEFAULT_CONFIG_ID, cfg.model_dump_json(exclude={"meta": {"version"}}), checksum, now),
        )
        _audit(conn, "ConfigActivated", "system", "config_versions", "1")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# 说明：rollback 与 activate 共享 ``_perform_activate``（重激活旧版本并降级当前版本），
# 各自补充专属审计事件（ConfigActivated / ConfigRolledBack）并均发布 PolicyChanged。
