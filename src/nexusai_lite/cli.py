"""NexusAI Lite 命令行入口（Wave 1 基础子集）。

子命令：``db init``、``config list/show/propose/activate/rollback``。
每个命令一个独立函数，便于后续 Wave 扩展 ``collect``/``stats``/``analyze``。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from nexusai_lite import db as db_module
from nexusai_lite.analysis import period_stats
from nexusai_lite.api_client import ApiClient, ApiClientError
from nexusai_lite.collector.official import OfficialCollector
from nexusai_lite.collector.window_rules import get_active_window_rule
from nexusai_lite.config import (
    activate_config,
    get_active_config,
    list_versions,
    propose_config,
    rollback_config,
)
from nexusai_lite.logging_setup import setup_logging
from nexusai_lite.maintenance import run_maintenance
from nexusai_lite.paths import db_path
from nexusai_lite.providers.deepseek import DeepSeekAdapter
from nexusai_lite.records import ApiRequest
from nexusai_lite.secrets import get_provider_api_key
from nexusai_lite.stats import DbRequestLogger, summary


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------- #
# db 子命令
# --------------------------------------------------------------------------- #


def cmd_db_init(_args: argparse.Namespace) -> int:
    """初始化数据库并写入默认配置。"""
    path = db_path()
    db_module.init_db(path)
    print(f"database initialized at {path}")
    return 0


# --------------------------------------------------------------------------- #
# config 子命令
# --------------------------------------------------------------------------- #


def cmd_config_list(args: argparse.Namespace) -> int:
    """列出配置版本。"""
    conn = db_module.connect(db_path())
    try:
        versions = list_versions(conn)
    finally:
        conn.close()
    if args.json:
        _print_json(versions)
        return 0
    header = ["id", "version", "status", "created_at", "effective_at", "reason"]
    print("\t".join(header))
    keys = ("id", "version", "status", "created_at", "effective_at", "change_reason")
    for row in versions:
        print("\t".join(str(row.get(h, "")) for h in keys))
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    """显示当前生效配置（或指定版本）；``--diff`` 时输出该版本的配置差异。"""
    conn = db_module.connect(db_path())
    try:
        if args.diff:
            if args.version is None:
                print("error: --diff 需要 --version 指定版本", file=sys.stderr)
                return 1
            row = conn.execute(
                "SELECT diff_json FROM config_versions WHERE version=?", (args.version,)
            ).fetchone()
            if row is None:
                print(f"error: 配置版本 {args.version} 不存在", file=sys.stderr)
                return 1
            payload = json.loads(row["diff_json"]) if row["diff_json"] else {}
        elif args.version is not None:
            row = conn.execute(
                "SELECT payload_json FROM config_versions WHERE version=?", (args.version,)
            ).fetchone()
            if row is None:
                print(f"error: 配置版本 {args.version} 不存在", file=sys.stderr)
                return 1
            payload = json.loads(row["payload_json"])
        else:
            payload = get_active_config(conn).model_dump()
    finally:
        conn.close()
    if args.json:
        _print_json(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_config_propose(args: argparse.Namespace) -> int:
    """提交一份新配置。"""
    path = Path(args.file)
    text = path.read_text(encoding="utf-8")
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        print(f"error: 配置文件解析失败: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("error: 配置顶层必须是映射（object）", file=sys.stderr)
        return 1
    conn = db_module.connect(db_path())
    try:
        version = propose_config(
            conn, payload, args.reason, created_by=args.created_by, human_approval=True
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"config version {version} proposed (status=proposed)")
    return 0


def cmd_config_activate(args: argparse.Namespace) -> int:
    """激活指定版本。"""
    conn = db_module.connect(db_path())
    try:
        activate_config(conn, args.version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"config version {args.version} activated")
    return 0


def cmd_config_rollback(args: argparse.Namespace) -> int:
    """回滚到指定版本。"""
    conn = db_module.connect(db_path())
    try:
        rollback_config(conn, args.version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"config rolled back to version {args.version}")
    return 0


# --------------------------------------------------------------------------- #
# collect 子命令
# --------------------------------------------------------------------------- #


def cmd_collect(_args: argparse.Namespace) -> int:
    """采集官方价格页与服务状态页并保存快照。"""
    conn = db_module.connect(db_path())
    try:
        cfg = get_active_config(conn)
        collector = OfficialCollector(
            conn,
            pricing_url=cfg.collector.pricing_url,
            status_url=cfg.collector.status_url,
            timeout_seconds=cfg.collector.timeout_seconds,
            user_agent=cfg.collector.user_agent,
        )
        try:
            results = collector.collect_all()
        finally:
            collector.close()
    finally:
        conn.close()
    failed = 0
    for result in results:
        print(f"{result.snapshot_type} {result.status} {result.parse_status}")
        if result.status != "ok":
            failed = 1
    return failed


# --------------------------------------------------------------------------- #
# maintain 子命令
# --------------------------------------------------------------------------- #


def cmd_maintain(args: argparse.Namespace) -> int:
    """执行维护（崩溃恢复 + 过期清理 + 陈旧预留清理），周期性调度用。"""
    conn = db_module.connect(db_path())
    try:
        report = run_maintenance(conn)
    finally:
        conn.close()
    if args.json:
        _print_json(asdict(report))
        return 0
    print(
        f"recovered={report.recovered} expired={report.expired} "
        f"stale_reservations={report.stale_reservations}"
    )
    return 0


# --------------------------------------------------------------------------- #
# stats 子命令
# --------------------------------------------------------------------------- #


def cmd_stats(args: argparse.Namespace) -> int:
    """汇总自身 API 调用统计。"""
    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    conn = db_module.connect(db_path())
    try:
        cfg = get_active_config(conn)
        report = summary(
            conn,
            since=since,
            group_by=args.group_by,
            min_sample=cfg.analysis.min_sample,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    data = report.to_dict()
    rows = [
        ("total", data["total"]),
        ("success", data["success"]),
        ("failure", data["failure"]),
        ("success_rate", data["success_rate"]),
        ("rate_429", data["rate_429"]),
        ("rate_5xx", data["rate_5xx"]),
        ("timeout_rate", data["timeout_rate"]),
        ("retry_rate", data["retry_rate"]),
        ("latency_quality", data["quality"]["latency"]),
        ("latency_p50", data["latency_ms"]["p50"]),
        ("latency_p95", data["latency_ms"]["p95"]),
        ("latency_p99", data["latency_ms"]["p99"]),
        ("input_tokens", data["input_tokens"]),
        ("output_tokens", data["output_tokens"]),
        ("estimated_cost", data["estimated_cost"]),
        ("cost_unknown_count", data["cost_unknown_count"]),
    ]
    for key, value in rows:
        print(f"{key}: {value}")
    if data["groups"]:
        print("groups:")
        for key, group in data["groups"].items():
            print(f"  {key}: {json.dumps(group, ensure_ascii=False)}")
    return 0


# --------------------------------------------------------------------------- #
# analyze 子命令
# --------------------------------------------------------------------------- #


def cmd_analyze(args: argparse.Namespace) -> int:
    """按时段桶分析自身调用规律。"""
    conn = db_module.connect(db_path())
    try:
        cfg = get_active_config(conn)
        rule = get_active_window_rule(conn)
        report = period_stats(
            conn,
            days=args.days,
            rule=rule,
            holidays=cfg.analysis.holidays,
            night_start=cfg.analysis.night_start,
            night_end=cfg.analysis.night_end,
            min_sample=cfg.analysis.min_sample,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(
        f"days={report.days} timezone={report.timezone} "
        f"since={report.since} until={report.until} sample_count={report.sample_count}"
    )
    for bucket in report.buckets:
        print(
            f"{bucket.bucket}: count={bucket.sample_count} success_rate={bucket.success_rate} "
            f"quality={bucket.quality}"
        )
    return 0


# --------------------------------------------------------------------------- #
# request 子命令
# --------------------------------------------------------------------------- #


def cmd_request(args: argparse.Namespace) -> int:
    """发起一次 DeepSeek 模型请求（经统一 API Client）。"""
    conn = db_module.connect(db_path())
    try:
        cfg = get_active_config(conn)
    finally:
        conn.close()

    api_key = get_provider_api_key("deepseek")
    if api_key is None:
        print("error: 未配置 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    model = args.model if args.model is not None else cfg.provider.default_model
    if model not in cfg.provider.models_whitelist:
        print(f"error: 模型 {model} 不在白名单 {cfg.provider.models_whitelist}", file=sys.stderr)
        return 2

    price_table = {name: row.model_dump() for name, row in cfg.pricing.table.items()}
    adapter = DeepSeekAdapter(base_url=cfg.provider.base_url, price_table=price_table)
    client = ApiClient(
        adapter,
        api_key=api_key,
        timeout_seconds=cfg.provider.request_timeout_seconds,
        max_retries=cfg.provider.max_retries,
        backoff_base_seconds=cfg.provider.retry_backoff_base_seconds,
        backoff_max_seconds=cfg.provider.retry_backoff_max_seconds,
        allow_retry_on_timeout=cfg.provider.allow_retry_on_timeout,
        logger=DbRequestLogger(db_path()),
    )
    request = ApiRequest(
        model=model,
        messages=[{"role": "user", "content": args.prompt}],
        max_tokens=args.max_tokens,
    )
    try:
        response = client.chat(request)
    except ApiClientError as exc:
        record = exc.record
        category = record.error_category.value if record.error_category else "unknown"
        print(
            f"error: 请求失败（{category}）{record.safe_error_message or ''}",
            file=sys.stderr,
        )
        return 1
    if response.content:
        print(response.content)
    print(f"latency={response.record.latency_ms}ms cost_status={response.record.cost_status}")
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexusai-lite", description="NexusAI Lite - 智能 API 错峰调度与夜间开发工具"
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument("--log-json", action="store_true", help="以 JSON 输出日志")
    sub = parser.add_subparsers(dest="command", required=True)

    # db
    db_parser = sub.add_parser("db", help="数据库子命令")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="初始化数据库与默认配置").set_defaults(func=cmd_db_init)

    # config
    config_parser = sub.add_parser("config", help="配置管理子命令")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)

    list_p = config_sub.add_parser("list", help="列出配置版本")
    list_p.add_argument("--json", action="store_true", help="输出 JSON")
    list_p.set_defaults(func=cmd_config_list)

    show_p = config_sub.add_parser("show", help="显示配置")
    show_p.add_argument("--version", type=int, default=None, help="显示指定版本")
    show_p.add_argument("--diff", action="store_true", help="显示指定版本的差异（需 --version）")
    show_p.add_argument("--json", action="store_true", help="输出 JSON")
    show_p.set_defaults(func=cmd_config_show)

    propose_p = config_sub.add_parser("propose", help="提交新配置")
    propose_p.add_argument("--file", required=True, help="配置文件路径（YAML/JSON）")
    propose_p.add_argument("--reason", required=True, help="变更原因")
    propose_p.add_argument("--created-by", default="user", help="提交者")
    propose_p.set_defaults(func=cmd_config_propose)

    activate_p = config_sub.add_parser("activate", help="激活版本")
    activate_p.add_argument("--version", type=int, required=True, help="要激活的版本号")
    activate_p.set_defaults(func=cmd_config_activate)

    rollback_p = config_sub.add_parser("rollback", help="回滚版本")
    rollback_p.add_argument("--version", type=int, required=True, help="要回滚到的版本号")
    rollback_p.set_defaults(func=cmd_config_rollback)

    # collect
    collect_p = sub.add_parser("collect", help="采集官方价格页与服务状态页")
    collect_p.set_defaults(func=cmd_collect)

    # maintain
    maintain_p = sub.add_parser(
        "maintain", help="执行维护（崩溃恢复 + 过期清理 + 陈旧预留清理）"
    )
    maintain_p.add_argument("--json", action="store_true", help="输出 JSON")
    maintain_p.set_defaults(func=cmd_maintain)

    # stats
    stats_p = sub.add_parser("stats", help="汇总自身 API 调用统计")
    stats_p.add_argument("--days", type=int, default=7, help="统计近 N 天（默认 7）")
    stats_p.add_argument("--json", action="store_true", help="输出 JSON")
    stats_p.add_argument(
        "--group-by", choices=["provider", "model"], default=None, help="按 provider/model 分组"
    )
    stats_p.set_defaults(func=cmd_stats)

    # analyze
    analyze_p = sub.add_parser("analyze", help="按时段桶分析自身调用规律")
    analyze_p.add_argument("--days", type=int, default=30, help="分析窗口天数（默认 30）")
    analyze_p.add_argument("--json", action="store_true", help="输出 JSON")
    analyze_p.set_defaults(func=cmd_analyze)

    # request
    request_p = sub.add_parser("request", help="发起一次 DeepSeek 模型请求")
    request_p.add_argument("--prompt", required=True, help="用户提示词")
    request_p.add_argument("--model", default=None, help="模型名（默认取配置）")
    request_p.add_argument("--max-tokens", type=int, default=None, help="最大输出 Token 数")
    request_p.set_defaults(func=cmd_request)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；调用 setup_logging 后分派子命令。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    setup_logging(level=args.log_level, json_output=args.log_json)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return int(func(args))
