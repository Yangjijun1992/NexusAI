"""官方价格页 + 服务状态页采集器测试（S1-01）。

全部使用 ``httpx.MockTransport``，不访问真实网络。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from nexusai_lite import db
from nexusai_lite.collector.official import OfficialCollector
from nexusai_lite.collector.window_rules import get_active_window_rule, is_peak

if TYPE_CHECKING:
    import sqlite3

FIXTURES = Path(__file__).parent / "fixtures"

PRICING_PATH = "/quick_start/pricing"
STATUS_PATH = "/"  # 默认状态页 URL https://status.deepseek.com/ 的路径


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    return db.connect(db_path)


def _client_for(
    *,
    pricing: httpx.Response | None = None,
    status: httpx.Response | None = None,
    pricing_error: Exception | None = None,
    status_error: Exception | None = None,
) -> httpx.Client:
    """构造带 MockTransport 的客户端，按 URL 路径分发 pricing/status 响应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == PRICING_PATH:
            if pricing_error is not None:
                raise pricing_error
            if pricing is not None:
                return pricing
        if request.url.path == STATUS_PATH:
            if status_error is not None:
                raise status_error
            if status is not None:
                return status
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _make_collector(
    conn: sqlite3.Connection,
    *,
    pricing: httpx.Response | None = None,
    status: httpx.Response | None = None,
    pricing_error: Exception | None = None,
    status_error: Exception | None = None,
) -> OfficialCollector:
    client = _client_for(
        pricing=pricing,
        status=status,
        pricing_error=pricing_error,
        status_error=status_error,
    )
    return OfficialCollector(conn, http_client=client)


def _snapshot_row(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM official_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


def _window_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM window_rules").fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------- pricing ok
def test_pricing_ok_writes_snapshot_and_rule(tmp_path: Path) -> None:
    """成功抓取价格页：快照 ok、sha256 正确、记录来源 URL、写入窗口规则。"""
    conn = _conn(tmp_path)
    try:
        body = (FIXTURES / "pricing_page.html").read_text(encoding="utf-8")
        collector = _make_collector(
            conn, pricing=httpx.Response(200, text=body)
        )
        result = collector.collect_pricing()

        assert result.status == "ok"
        assert result.parse_status == "parsed"
        assert result.snapshot_id is not None
        assert result.parsed == {"peak_windows": [("01:00", "04:00"), ("06:00", "10:00")]}

        row = _snapshot_row(conn, result.snapshot_id)
        assert row["status"] == "ok"
        assert row["parse_status"] == "parsed"
        assert row["source_url"] == collector.pricing_url
        assert row["raw_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert row["raw_content"] == body
        assert row["fetched_at"].endswith(("+00:00", "Z"))

        # 窗口规则应已写入并关联快照。
        rules = _window_rows(conn)
        assert len(rules) == 1
        assert rules[0]["active"] == 1
        assert rules[0]["source"] == "official-pricing-page"
        assert rules[0]["source_snapshot_id"] == result.snapshot_id
        rule_json = json.loads(rules[0]["rule_json"])
        assert rule_json["peak_windows"] == [["01:00", "04:00"], ["06:00", "10:00"]]
        # 官方页面以 UTC 表达时段 → 规则时区必须随之为 UTC（而非默认北京时间）。
        assert rule_json["timezone"] == "UTC"
    finally:
        conn.close()


def test_collected_rule_peak_semantics_beijing(tmp_path: Path) -> None:
    """真实页面（UTC 表达）写入的规则应与北京时间语义一致。

    北京周一 09:30 = UTC 01:30 → 高峰；13:30 = UTC 05:30 → 间隙；15:00 = UTC 07:00 → 高峰。
    """
    conn = _conn(tmp_path)
    try:
        body = (FIXTURES / "pricing_page.html").read_text(encoding="utf-8")
        collector = _make_collector(conn, pricing=httpx.Response(200, text=body))
        collector.collect_pricing()

        rule = get_active_window_rule(conn)
        assert rule is not None
        assert rule.timezone == "UTC"
        assert is_peak(datetime(2026, 10, 12, 1, 30, tzinfo=UTC), rule) is True
        assert is_peak(datetime(2026, 10, 12, 5, 30, tzinfo=UTC), rule) is False
        assert is_peak(datetime(2026, 10, 12, 7, 0, tzinfo=UTC), rule) is True
    finally:
        conn.close()


def test_pricing_unparseable_no_rule(tmp_path: Path) -> None:
    """无法解析的价格页：parse_failed 且不写任何窗口规则。"""
    conn = _conn(tmp_path)
    try:
        collector = _make_collector(
            conn,
            pricing=httpx.Response(200, text="<html><body>not parseable</body></html>"),
        )
        result = collector.collect_pricing()

        assert result.status == "ok"
        assert result.parse_status == "parse_failed"
        assert result.snapshot_id is not None
        assert result.parsed is None
        assert _window_rows(conn) == []
    finally:
        conn.close()


# ------------------------------------------------------------------- pricing fail
def test_pricing_http_500_failed_no_rule(tmp_path: Path) -> None:
    """HTTP 500：失败行、error_code=HTTP_500，且不写窗口规则。"""
    conn = _conn(tmp_path)
    try:
        collector = _make_collector(
            conn, pricing=httpx.Response(503, text="Service Unavailable")
        )
        result = collector.collect_pricing()

        assert result.status == "failed"
        assert result.error_code == "HTTP_503"
        assert result.snapshot_id is not None
        assert _window_rows(conn) == []

        row = _snapshot_row(conn, result.snapshot_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "HTTP_503"
        assert row["parse_status"] is None
    finally:
        conn.close()


def test_pricing_timeout_failed(tmp_path: Path) -> None:
    """请求超时：失败行、error_code=TIMEOUT，不写窗口规则。"""
    conn = _conn(tmp_path)
    try:
        collector = _make_collector(
            conn, pricing_error=httpx.ConnectTimeout("timed out")
        )
        result = collector.collect_pricing()

        assert result.status == "failed"
        assert result.error_code == "TIMEOUT"
        assert result.snapshot_id is not None
        assert _window_rows(conn) == []
    finally:
        conn.close()


def test_pricing_network_error_failed(tmp_path: Path) -> None:
    """连接性错误：失败行、error_code=NETWORK。"""
    conn = _conn(tmp_path)
    try:
        collector = _make_collector(
            conn, pricing_error=httpx.ConnectError("connection refused")
        )
        result = collector.collect_pricing()

        assert result.status == "failed"
        assert result.error_code == "NETWORK"
        assert result.snapshot_id is not None
    finally:
        conn.close()


# ----------------------------------------------------------------- raw cap
def test_pricing_raw_cap_truncation(tmp_path: Path) -> None:
    """超长原始内容应截断至上限并在 parsed_json 标注 truncated。"""
    conn = _conn(tmp_path)
    try:
        # 高峰标记放在内容开头（截断后仍在），其余用填充撑破上限。
        body = "peak hours 09:00-12:00 " + ("x" * 2_000_000)
        collector = _make_collector(
            conn, pricing=httpx.Response(200, text=body)
        )
        result = collector.collect_pricing()

        assert result.status == "ok"
        assert result.snapshot_id is not None
        row = _snapshot_row(conn, result.snapshot_id)
        assert len(row["raw_content"]) == 1_000_000
        parsed = json.loads(row["parsed_json"])
        assert parsed["truncated"] is True
        assert parsed["peak_windows"] == [["09:00", "12:00"]]
    finally:
        conn.close()


# --------------------------------------------------------------------- status
def test_status_json_ok(tmp_path: Path) -> None:
    """statuspage.io 风格 JSON：应解析出状态文本。"""
    conn = _conn(tmp_path)
    try:
        body = json.dumps(
            {
                "page": {"name": "DeepSeek Service"},
                "status": {"indicator": "none", "description": "All Systems Operational"},
            }
        )
        collector = _make_collector(
            conn, status=httpx.Response(200, text=body)
        )
        result = collector.collect_status()

        assert result.status == "ok"
        assert result.parse_status == "parsed"
        assert result.parsed == {"status_text": "All Systems Operational"}
        assert result.snapshot_id is not None

        row = _snapshot_row(conn, result.snapshot_id)
        parsed = json.loads(row["parsed_json"])
        assert parsed["status_text"] == "All Systems Operational"
    finally:
        conn.close()


def test_status_html_operational(tmp_path: Path) -> None:
    """HTML 状态页含 operational 标记：应解析成功。"""
    conn = _conn(tmp_path)
    try:
        body = (FIXTURES / "status_page.html").read_text(encoding="utf-8")
        collector = _make_collector(conn, status=httpx.Response(200, text=body))
        result = collector.collect_status()

        assert result.status == "ok"
        assert result.parse_status == "parsed"
        assert result.parsed is not None
        assert "operational" in result.parsed["status_text"].lower()
    finally:
        conn.close()


def test_status_garbage_parse_failed(tmp_path: Path) -> None:
    """状态页无法解析（既非 JSON 也无标记）：parse_status=parse_failed。"""
    conn = _conn(tmp_path)
    try:
        collector = _make_collector(
            conn, status=httpx.Response(200, text="<html><body>??</body></html>")
        )
        result = collector.collect_status()

        assert result.status == "ok"
        assert result.parse_status == "parse_failed"
        assert result.parsed is None
    finally:
        conn.close()


def test_status_http_error_failed(tmp_path: Path) -> None:
    """状态页 HTTP 错误：失败行且不写窗口规则。"""
    conn = _conn(tmp_path)
    try:
        collector = _make_collector(
            conn, status=httpx.Response(500, text="boom")
        )
        result = collector.collect_status()

        assert result.status == "failed"
        assert result.error_code == "HTTP_500"
        assert result.snapshot_id is not None
    finally:
        conn.close()


# ------------------------------------------------------------------ collect_all
def test_collect_all_returns_two_and_never_raises(tmp_path: Path) -> None:
    """collect_all 应返回 2 个结果，且单次失败不影响另一次。"""
    conn = _conn(tmp_path)
    try:
        body = (FIXTURES / "pricing_page.html").read_text(encoding="utf-8")
        collector = _make_collector(
            conn,
            pricing=httpx.Response(200, text=body),
            status_error=httpx.ConnectError("status down"),
        )
        results = collector.collect_all()

        assert len(results) == 2
        pricing_result, status_result = results
        assert pricing_result.status == "ok"
        assert status_result.status == "failed"
        assert status_result.error_code == "NETWORK"

        # 快照应写入 2 行（1 ok + 1 failed）。
        count = conn.execute("SELECT COUNT(*) AS c FROM official_snapshots").fetchone()
        assert count is not None
        assert count["c"] == 2
    finally:
        conn.close()
