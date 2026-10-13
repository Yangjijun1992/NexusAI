"""密钥扫描与静态安全检查（S3-07）。

对夜间开发产出的补丁/代码执行 Secret Scan：检测常见密钥形态并在检测到高危
泄露时停止任务、隔离工作区并告警（reference F7、§4.3、§12 规则 6）。所有
产出的文本（``Finding.redacted_excerpt``、异常消息、告警载荷）一律经
:func:`redact_text` 脱敏，绝不包含原始密钥。检测器与规则基于正则，仅依赖
标准库。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexusai_lite.alerts import Severity as AlertSeverity
from nexusai_lite.logging_setup import redact_text

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from nexusai_lite.alerts import AlertManager

#: 单文件扫描大小上限（字节）；超过则视为不可控/超大文件而跳过。
_MAX_FILE_BYTES = 1_000_000

#: 用于文本读取的容错编码（丢弃无法解码的字节，避免二进制旁路崩溃）。
_FILE_ENCODING = "utf-8"


class Severity(str, enum.Enum):  # noqa: UP042 - 持久化要求 str+Enum 便于比较/存储
    """扫描发现的严重级别。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Finding:
    """一次密钥扫描发现。

    Args:
        location: 发现位置（文件路径，或 ``"diff"`` / ``"text"``）。
        line: 所在行号（按 1 起始）；无法确定时为 ``None``。
        rule: 命中规则名。
        severity: 严重级别字符串（``"high"`` / ``"medium"`` / ``"low"``）。
        redacted_excerpt: 已脱敏的上下文摘录（绝不包含原始密钥）。
    """

    location: str
    line: int | None
    rule: str
    severity: str
    redacted_excerpt: str


class SecretLeakError(Exception):
    """检测到高危密钥泄露时抛出，用于停止任务（§4.3、§12 规则 6）。"""


# 内置规则定义：(规则名, 捕获整个密钥的编译正则, 严重级别)。
# 每个正则用捕获组包裹完整密钥串，以便逐字替换为 [REDACTED]。
_RULES: tuple[tuple[str, re.Pattern[str], Severity], ...] = (
    ("provider_api_key", re.compile(r"sk-[A-Za-z0-9]{10,}"), Severity.HIGH),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), Severity.HIGH),
    (
        "private_key_pem",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
            r".*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        Severity.HIGH,
    ),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        Severity.HIGH,
    ),
    (
        "auth_bearer",
        re.compile(
            r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+", re.IGNORECASE
        ),
        Severity.HIGH,
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
            re.IGNORECASE,
        ),
        Severity.MEDIUM,
    ),
    (
        "high_entropy_secret",
        re.compile(r"\b[0-9a-fA-F]{32,64}\b|\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        Severity.LOW,
    ),
)


class SecretScanner:
    """执行密钥扫描的扫描器；支持注入自定义规则。

    Args:
        extra_patterns: 额外规则序列，每项为 ``(规则名, 编译正则, 严重级别串)``。
            严重级别串取 ``"high"`` / ``"medium"`` / ``"low"`` 之一。
    """

    def __init__(
        self, *, extra_patterns: Sequence[tuple[str, re.Pattern[str], str]] = ()
    ) -> None:
        """以内置规则叠加额外规则构造扫描器。"""
        rules: list[tuple[str, re.Pattern[str], Severity]] = list(_RULES)
        for name, pattern, severity in extra_patterns:
            rules.append((name, pattern, Severity(severity)))
        self._rules = tuple(rules)

    # ------------------------------------------------------------- 规则扫描

    def scan_text(self, text: str, *, location: str = "text") -> list[Finding]:
        """扫描整段文本，返回带行号、已脱敏摘录的发现列表。"""
        findings: list[Finding] = []
        for name, pattern, severity in self._rules:
            for match in pattern.finditer(text):
                line, excerpt = _line_and_excerpt(text, match.start(), match.end())
                findings.append(
                    Finding(
                        location=location,
                        line=line,
                        rule=name,
                        severity=severity.value,
                        redacted_excerpt=excerpt,
                    )
                )
        return findings

    def scan_file(self, path: Path) -> list[Finding]:
        """扫描单个文件（跳过二进制/超大文件），位置为文件路径。

        Args:
            path: 待扫描文件路径。

        Returns:
            该文件发现的密钥列表；文件不可读/二进制/超大时返回空列表。
        """
        try:
            if not path.is_file():
                return []
            stat = path.stat()
            if stat.st_size > _MAX_FILE_BYTES:
                return []
            data = path.read_bytes()
            if b"\x00" in data:
                return []
            text = data.decode(_FILE_ENCODING, errors="replace")
        except (OSError, UnicodeError):
            return []
        return self.scan_text(text, location=str(path))

    def scan_diff(self, diff_text: str) -> list[Finding]:
        """只扫描补丁中新增的行（``+`` 开头且非 ``+++``），位置为 ``"diff"``。

        Args:
            diff_text: unified diff 文本。

        Returns:
            仅针对新增行发现的密钥列表；行号相对于 diff 全文。
        """
        findings: list[Finding] = []
        for line_no, raw_line in enumerate(diff_text.splitlines(), start=1):
            if not raw_line.startswith("+") or raw_line.startswith("+++"):
                continue
            content = raw_line[1:]
            # scan_text 返回的 line 恒为 1（单行文本）；改为 diff 中的真实行号。
            findings.extend(
                Finding(
                    location=finding.location,
                    line=line_no,
                    rule=finding.rule,
                    severity=finding.severity,
                    redacted_excerpt=finding.redacted_excerpt,
                )
                for finding in self.scan_text(content, location="diff")
            )
        return findings

    def scan_files(self, paths: Sequence[Path]) -> list[Finding]:
        """聚合扫描多个文件。

        Args:
            paths: 待扫描文件路径序列。

        Returns:
            全部文件的发现列表（按扫描顺序拼接）。
        """
        findings: list[Finding] = []
        for path in paths:
            findings.extend(self.scan_file(path))
        return findings

    # ------------------------------------------------------------- 阻断与告警

    def assert_clean(
        self,
        findings: Sequence[Finding],
        *,
        alert_manager: AlertManager | None = None,
        scope: str | None = None,
    ) -> None:
        """若存在高危发现的密钥，停止任务并（可选）触发告警。

         在检测到任何 ``high`` 级别的密钥时，抛出 :class:`SecretLeakError`
         （§4.3、§12 规则 6），并在提供 :class:`AlertManager` 时写入一条
         ``SECRET_LEAK_DETECTED`` 的 CRITICAL 告警（载荷经脱敏，绝不包含密钥）。

        Args:
            findings: 待审查的扫描发现。
            alert_manager: 可选告警管理器；提供时在阻断同时写入告警。
            scope: 告警影响范围（如 ``task:<id>``）。

        Raises:
            SecretLeakError: 当存在高危发现的密钥时。
        """
        high = [f for f in findings if f.severity == Severity.HIGH.value]
        if not high:
            return
        if alert_manager is not None:
            alert_manager.raise_alert(
                severity=AlertSeverity.CRITICAL,
                code="SECRET_LEAK_DETECTED",
                scope=scope,
                title="检测到密钥泄露，任务已阻断",
                message=(
                    f"在 {len(high)} 处发现高危密钥，任务已停止并隔离工作区；"
                    "请人工核查并轮换泄露凭据。"
                ),
                detail={
                    "rules": sorted({f.rule for f in high}),
                    "locations": [f.location for f in high],
                },
            )
        rules_detail = ", ".join(sorted({f.rule for f in high}))
        msg = f"检测到 {len(high)} 处高危密钥泄露（{rules_detail}），任务已停止。"
        raise SecretLeakError(msg)


def _line_and_excerpt(text: str, start: int, end: int) -> tuple[int, str]:
    """计算匹配所在行号与已脱敏的整行摘录。

    行号按 1 起始；摘录为所在整行，其中命中的密钥串替换为 ``[REDACTED]``，
    并对结果再经 :func:`redact_text` 兜底脱敏，确保不泄露原始密钥。
    """
    line_offsets = text.count("\n", 0, start)
    line_no = line_offsets + 1
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    line_text = text[line_start:line_end]
    rel_start = start - line_start
    rel_end = end - line_start
    excerpt = line_text[:rel_start] + "[REDACTED]" + line_text[rel_end:]
    return line_no, redact_text(excerpt)
