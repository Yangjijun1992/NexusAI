"""官方价格页 + 服务状态页采集器（S1-01）。

定时抓取官方价格页与服务状态页并保存快照到 ``official_snapshots``，并从价格页
解析高峰/空闲计价时段写入 ``window_rules``（S1-02）。

数据边界（reference.md §1.3）：官方公开信息与自身观测分开存储。抓取失败/解析失败
一律记录失败，**绝不伪造默认值当作权威数据**。每次快照记录来源 URL 与采集时间。

限制说明：本模块以官方公开页面为准并定期复核。若官方页面结构变更导致无法解析，
解析器返回 None 并记录 ``parse_status="parse_failed"``，不编写窗口规则；如需更准确
的解析，需人工复核官方页面结构后调整正则/解析逻辑。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from nexusai_lite import logging_setup
from nexusai_lite.collector.window_rules import (
    WindowRuleData,
    detect_window_timezone,
    parse_peak_windows,
    upsert_window_rule,
)
from nexusai_lite.db import utcnow_iso

if TYPE_CHECKING:
    import sqlite3

# 官方二进制内容以 UTF-8 解码后存储；超过该长度截断并在 parsed_json 中标注。
_RAW_CAP = 1_000_000
# 错误信息脱敏后截断长度。
_ERROR_MSG_MAX = 300

_STATUS_OPERATIONAL_RE = re.compile(
    r"(operational|all systems operational|状态正常|运行正常)",
    re.IGNORECASE,
)

_PRICING_SNAPSHOT = "pricing"
_STATUS_SNAPSHOT = "status"
_SOURCE_OFFICIAL = "official-pricing-page"


@dataclass(frozen=True)
class SnapshotResult:
    """一次官方采集的结果（写入快照后的摘要，用于测试与审计）。"""

    snapshot_id: int | None
    snapshot_type: str
    source_url: str
    fetched_at: str
    status: str  # "ok" / "failed"
    parse_status: str | None
    parsed: dict | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class _SnapshotRow:
    """内部：对应 ``official_snapshots`` 表的一行，供批量插入使用。"""

    snapshot_type: str
    source_url: str
    fetched_at: str
    status: str
    parse_status: str | None
    raw: str
    raw_sha256: str
    parsed_json: dict[str, object]
    error_code: str | None = None
    error_message: str | None = None


class OfficialCollector:
    """官方信息采集器。

    可注入 ``http_client``（例如带 ``httpx.MockTransport`` 的客户端）用于测试；
    注入的客户端由调用方负责关闭，本类不主动关闭。
    """

    def __init__(  # noqa: PLR0913 - 任务规格（S1-01）约定的构造器签名：全部为可替换显式参数
        self,
        conn: sqlite3.Connection,
        *,
        http_client: httpx.Client | None = None,
        pricing_url: str = "https://api-docs.deepseek.com/quick_start/pricing",
        status_url: str = "https://status.deepseek.com/",
        timeout_seconds: float = 15.0,
        user_agent: str = "nexusai-lite/0.1 (local)",
    ) -> None:
        """初始化采集器。

        Args:
            conn: 数据库连接（官方快照与窗口规则的落库目标）。
            http_client: 可注入的 httpx 客户端（测试用 MockTransport），
                由调用方负责关闭；为 None 时自建客户端并在 close() 时关闭。
            pricing_url: 官方价格页 URL。
            status_url: 官方服务状态页 URL。
            timeout_seconds: 自建客户端的请求超时（秒）。
            user_agent: 自建客户端的 User-Agent 头。
        """
        self._conn = conn
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                timeout=timeout_seconds,
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            )
            self._owns_client = True
        self.pricing_url = pricing_url
        self.status_url = status_url

    def close(self) -> None:
        """关闭本采集器自建的客户端（不关闭注入的客户端）。"""
        if self._owns_client:
            self._client.close()

    def collect_all(self) -> list[SnapshotResult]:
        """依次采集价格页与状态页，返回两个结果；单次失败不影响另一次。"""
        results: list[SnapshotResult] = []
        try:
            results.append(self.collect_pricing())
        except Exception as exc:  # noqa: BLE001 - 边界兜底：采集失败不中断整体
            results.append(self._failed_result(_PRICING_SNAPSHOT, self.pricing_url, "NETWORK", exc))
        try:
            results.append(self.collect_status())
        except Exception as exc:  # noqa: BLE001
            results.append(self._failed_result(_STATUS_SNAPSHOT, self.status_url, "NETWORK", exc))
        return results

    # ------------------------------------------------------------------ pricing
    def collect_pricing(self) -> SnapshotResult:
        """采集官方价格页并尝试解析计价时段。"""
        try:
            response = self._client.get(self.pricing_url)
        except httpx.TimeoutException as exc:
            return self._record_failure(_PRICING_SNAPSHOT, self.pricing_url, "TIMEOUT", exc)
        except httpx.HTTPError as exc:
            return self._record_failure(_PRICING_SNAPSHOT, self.pricing_url, "NETWORK", exc)

        if not response.is_success:
            code = f"HTTP_{response.status_code}"
            return self._record_failure(
                _PRICING_SNAPSHOT, self.pricing_url, code, f"HTTP {response.status_code}"
            )

        text, truncated = self._cap_raw(response.text)
        raw_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fetched_at = utcnow_iso()

        windows = parse_peak_windows(text)
        if windows is not None:
            rule = WindowRuleData(
                timezone=detect_window_timezone(text),
                peak_windows=tuple(windows),
                source=_SOURCE_OFFICIAL,
            )
            # 先插入快照，拿到 snapshot_id 再写入窗口规则并关联。
            snapshot_id = self._insert_snapshot(
                _SnapshotRow(
                    snapshot_type=_PRICING_SNAPSHOT,
                    source_url=self.pricing_url,
                    fetched_at=fetched_at,
                    status="ok",
                    parse_status="parsed",
                    raw=text,
                    raw_sha256=raw_sha256,
                    parsed_json=self._pricing_parsed_json(windows, fetched_at, truncated=truncated),
                )
            )
            upsert_window_rule(
                self._conn,
                rule,
                source=_SOURCE_OFFICIAL,
                source_snapshot_id=snapshot_id,
            )
            return SnapshotResult(
                snapshot_id=snapshot_id,
                snapshot_type=_PRICING_SNAPSHOT,
                source_url=self.pricing_url,
                fetched_at=fetched_at,
                status="ok",
                parse_status="parsed",
                parsed={"peak_windows": windows},
            )

        # 无可解析时段：记录 parse_failed，不写窗口规则、不伪造默认值。
        parsed_json: dict[str, object] = {"source": _SOURCE_OFFICIAL, "fetched_at": fetched_at}
        if truncated:
            parsed_json["truncated"] = True
        snapshot_id = self._insert_snapshot(
            _SnapshotRow(
                snapshot_type=_PRICING_SNAPSHOT,
                source_url=self.pricing_url,
                fetched_at=fetched_at,
                status="ok",
                parse_status="parse_failed",
                raw=text,
                raw_sha256=raw_sha256,
                parsed_json=parsed_json,
            )
        )
        return SnapshotResult(
            snapshot_id=snapshot_id,
            snapshot_type=_PRICING_SNAPSHOT,
            source_url=self.pricing_url,
            fetched_at=fetched_at,
            status="ok",
            parse_status="parse_failed",
            parsed=None,
        )

    # ------------------------------------------------------------------- status
    def collect_status(self) -> SnapshotResult:
        """采集官方服务状态页并尽力解析当前状态。"""
        try:
            response = self._client.get(self.status_url)
        except httpx.TimeoutException as exc:
            return self._record_failure(_STATUS_SNAPSHOT, self.status_url, "TIMEOUT", exc)
        except httpx.HTTPError as exc:
            return self._record_failure(_STATUS_SNAPSHOT, self.status_url, "NETWORK", exc)

        if not response.is_success:
            code = f"HTTP_{response.status_code}"
            return self._record_failure(
                _STATUS_SNAPSHOT, self.status_url, code, f"HTTP {response.status_code}"
            )

        text, truncated = self._cap_raw(response.text)
        raw_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fetched_at = utcnow_iso()

        parse_status, status_text = self._parse_status_text(text)
        parsed_json: dict[str, object] = {"fetched_at": fetched_at}
        if truncated:
            parsed_json["truncated"] = True
        if status_text is not None:
            parsed_json["status_text"] = status_text

        snapshot_id = self._insert_snapshot(
            _SnapshotRow(
                snapshot_type=_STATUS_SNAPSHOT,
                source_url=self.status_url,
                fetched_at=fetched_at,
                status="ok",
                parse_status=parse_status,
                raw=text,
                raw_sha256=raw_sha256,
                parsed_json=parsed_json,
            )
        )
        return SnapshotResult(
            snapshot_id=snapshot_id,
            snapshot_type=_STATUS_SNAPSHOT,
            source_url=self.status_url,
            fetched_at=fetched_at,
            status="ok",
            parse_status=parse_status,
            parsed={"status_text": status_text} if status_text is not None else None,
        )

    # ------------------------------------------------------------- internals
    def _parse_status_text(self, text: str) -> tuple[str, str | None]:
        """尽力解析状态文本。优先 JSON（statuspage.io），其次 HTML 标记。

        Returns:
            ``(parse_status, status_text)``；无法解析时 ``("parse_failed", None)``。
        """
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                return "parse_failed", None
            status = data.get("status") if isinstance(data, dict) else None
            if isinstance(status, dict):
                status_text = status.get("description") or status.get("indicator")
                if status_text:
                    return "parsed", str(status_text)
            return "parse_failed", None

        match = _STATUS_OPERATIONAL_RE.search(text)
        if match:
            return "parsed", match.group(0)
        return "parse_failed", None

    def _cap_raw(self, text: str) -> tuple[str, bool]:
        """截断原始文本至上限，返回 ``(capped_text, truncated)``。"""
        if len(text) <= _RAW_CAP:
            return text, False
        return text[:_RAW_CAP], True

    def _pricing_parsed_json(
        self, windows: list[tuple[str, str]], fetched_at: str, *, truncated: bool
    ) -> dict[str, object]:
        """构造价格页解析结果 JSON。"""
        parsed_json: dict[str, object] = {
            "peak_windows": [list(w) for w in windows],
            "source": _SOURCE_OFFICIAL,
            "fetched_at": fetched_at,
        }
        if truncated:
            parsed_json["truncated"] = True
        return parsed_json

    def _record_failure(
        self,
        snapshot_type: str,
        source_url: str,
        error_code: str,
        error: object,
    ) -> SnapshotResult:
        """记录一次抓取/请求失败快照（不写任何窗口规则）。"""
        fetched_at = utcnow_iso()
        message = _sanitize_error(error)
        snapshot_id = self._insert_snapshot(
            _SnapshotRow(
                snapshot_type=snapshot_type,
                source_url=source_url,
                fetched_at=fetched_at,
                status="failed",
                parse_status=None,
                raw="",
                raw_sha256="",
                parsed_json={},
                error_code=error_code,
                error_message=message,
            )
        )
        return SnapshotResult(
            snapshot_id=snapshot_id,
            snapshot_type=snapshot_type,
            source_url=source_url,
            fetched_at=fetched_at,
            status="failed",
            parse_status=None,
            parsed=None,
            error_code=error_code,
            error_message=message,
        )

    def _failed_result(
        self,
        snapshot_type: str,
        source_url: str,
        error_code: str,
        error: object,
    ) -> SnapshotResult:
        """collect_all 兜底：极端情况下构造失败结果（不落库）。"""
        fetched_at = utcnow_iso()
        return SnapshotResult(
            snapshot_id=None,
            snapshot_type=snapshot_type,
            source_url=source_url,
            fetched_at=fetched_at,
            status="failed",
            parse_status=None,
            parsed=None,
            error_code=error_code,
            error_message=_sanitize_error(error),
        )

    def _insert_snapshot(self, row: _SnapshotRow) -> int:
        """插入一条官方快照并返回其 id（含失败行的错误字段）。"""
        cursor = self._conn.execute(
            "INSERT INTO official_snapshots "
            "(snapshot_type, source_url, fetched_at, status, parse_status, "
            " raw_content, raw_sha256, parsed_json, error_code, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.snapshot_type,
                row.source_url,
                row.fetched_at,
                row.status,
                row.parse_status,
                row.raw,
                row.raw_sha256,
                json.dumps(row.parsed_json, ensure_ascii=False),
                row.error_code,
                row.error_message,
            ),
        )
        row_id = cursor.lastrowid
        if row_id is None:
            msg = "插入官方快照后未返回 rowid"
            raise RuntimeError(msg)
        return int(row_id)


def _sanitize_error(error: object) -> str:
    """把异常/消息脱敏并截断为固定长度。"""
    message = str(error)
    message = logging_setup.redact_text(message)
    return message[:_ERROR_MSG_MAX]
