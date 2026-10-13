"""密钥扫描与静态安全检查测试（S3-07）。

确定性验证各内置规则的命中与严重级别、行号、摘录/异常/告警的脱敏（绝不包含
原始密钥）、diff 只扫新增行、文件扫描、阻断（:class:`SecretLeakError`）与
告警落库，以及额外规则扩展（reference F7、§4.3、§12 规则 6）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.alerts import AlertManager
from nexusai_lite.secretscan import Finding, SecretLeakError, SecretScanner

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

#: 各内置规则的代表性样例：规则名 -> 待匹配的原始密钥。
_SAMPLES: dict[str, str] = {
    "provider_api_key": "sk-abcdefghijklmnop",
    "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
    "jwt": (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    ),
    "auth_bearer": "Authorization: Bearer secret-token-1234",
    "credential_assignment": "api_key = f9e8d7c6b5a4",
    "high_entropy_secret": "deadbeefdeadbeefdeadbeefdeadbeef",
}

_PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCAmIwggJeAgEAAoGB\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（含 006 告警表迁移）。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


def _findings_by_rule(findings: list[Finding]) -> dict[str, Finding]:
    """把发现列表按规则名聚合（同名规则仅取首个）。"""
    return {f.rule: f for f in findings}


def test_each_builtin_rule_detected_in_text() -> None:
    """每一内置规则都应在文本中命中，且返回规则名与严重级别。"""
    scanner = SecretScanner()
    for rule, sample in _SAMPLES.items():
        text = f"line one\n{sample}\nline three"
        findings = scanner.scan_text(text)
        assert rule in {f.rule for f in findings}, f"规则 {rule} 未命中"


def test_pem_private_key_detected() -> None:
    """多行 PEM 私钥块应被检出（规则 private_key_pem，严重 high）。"""
    scanner = SecretScanner()
    findings = scanner.scan_text(_PEM_BLOCK)
    by_rule = _findings_by_rule(findings)
    assert by_rule["private_key_pem"].rule == "private_key_pem"
    assert by_rule["private_key_pem"].severity == "high"
    assert by_rule["private_key_pem"].location == "text"


def test_line_numbers_are_correct() -> None:
    """发现应携带正确的 1 起始行号。"""
    scanner = SecretScanner()
    text = "first\nsecond sk-abcdefghijklmnop\nthird"
    findings = scanner.scan_text(text)
    by_rule = _findings_by_rule(findings)
    assert by_rule["provider_api_key"].line == 2


def test_severity_levels() -> None:
    """各规则应映射到约定严重级别。"""
    scanner = SecretScanner()
    expectations = {
        "provider_api_key": "high",
        "aws_access_key": "high",
        "jwt": "high",
        "auth_bearer": "high",
        "private_key_pem": "high",
        "credential_assignment": "medium",
        "high_entropy_secret": "low",
    }
    # PEM 与 high_entropy 共享样例；其余逐条独立扫描。
    by_rule: dict[str, Finding] = {}
    for rule, sample in _SAMPLES.items():
        by_rule[rule] = _findings_by_rule(scanner.scan_text(sample))[rule]
    by_rule["private_key_pem"] = _findings_by_rule(scanner.scan_text(_PEM_BLOCK))[
        "private_key_pem"
    ]
    for rule, expected in expectations.items():
        assert by_rule[rule].severity == expected, f"规则 {rule} 严重级别错误"


def test_redacted_excerpt_never_contains_raw_secret() -> None:
    """摘录必须脱敏：绝不包含任一原始密钥。"""
    scanner = SecretScanner()
    for sample in _SAMPLES.values():
        for finding in scanner.scan_text(sample):
            assert sample not in finding.redacted_excerpt
    for finding in scanner.scan_text(_PEM_BLOCK):
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEF" not in finding.redacted_excerpt


def test_scan_diff_only_flags_added_lines() -> None:
    """diff 扫描应忽略上下文与删除行，只标记新增行并给出 diff 行号。"""
    scanner = SecretScanner()
    diff = (
        "--- a/file.py\n"
        "+++ b/file.py\n"
        "@@ -1,3 +1,2 @@\n"
        " context line\n"
        "-removed line with sk-SECRETREMOVED12345\n"
        "+added line with sk-ADDEDSECRET67890\n"
    )
    findings = scanner.scan_diff(diff)
    assert findings, "应至少命中新增行"
    assert all(f.location == "diff" for f in findings)
    assert all(f.line == 6 for f in findings), "应为 diff 中新增行的真实行号"
    added_secret = "sk-ADDEDSECRET67890"  # noqa: S105 - 测试用伪造密钥
    removed_secret = "sk-SECRETREMOVED12345"  # noqa: S105 - 测试用伪造密钥
    for finding in findings:
        assert added_secret not in finding.redacted_excerpt
        assert removed_secret not in finding.redacted_excerpt
        assert finding.redacted_excerpt.startswith("added line with [REDACTED]")


def test_scan_diff_ignores_context_and_removed() -> None:
    """纯上下文/删除行（无新增行）的 diff 不应产生发现。"""
    scanner = SecretScanner()
    diff = (
        "@@ -1 +1 @@\n"
        " context\n"
        "-old sk-SECRETREMOVED12345\n"
    )
    assert scanner.scan_diff(diff) == []
    assert all(
        f.location == "diff" for f in scanner.scan_diff("+added sk-ADDEDSECRET67890")
    )


def test_scan_file_with_secret(tmp_path: Path) -> None:
    """含密钥的文件应被检出，位置为文件路径。"""
    secret = "sk-a1b2c3d4e5f6g7h8"  # noqa: S105 - 测试用伪造密钥
    path = tmp_path / "app.py"
    path.write_text(f"API_KEY = '{secret}'\n", encoding="utf-8")
    scanner = SecretScanner()
    findings = scanner.scan_file(path)
    assert findings, "应检出文件中的密钥"
    assert all(f.location == str(path) for f in findings)


def test_scan_file_clean(tmp_path: Path) -> None:
    """干净文件不应产生任何发现。"""
    path = tmp_path / "clean.txt"
    path.write_text("hello world\nno secrets here\n", encoding="utf-8")
    assert SecretScanner().scan_file(path) == []


def test_scan_file_skips_binary(tmp_path: Path) -> None:
    """含 NUL 字节的二进制文件应被安全跳过。"""
    path = tmp_path / "blob.bin"
    path.write_bytes(b"\x00\x01\x02sk-abcdefghijklmnop\x00")
    assert SecretScanner().scan_file(path) == []


def test_scan_files_aggregates(tmp_path: Path) -> None:
    """scan_files 应聚合多个文件的发现。"""
    secret = "sk-a1b2c3d4e5f6g7h8"  # noqa: S105 - 测试用伪造密钥
    path = tmp_path / "a.py"
    path.write_text(secret, encoding="utf-8")
    findings = SecretScanner().scan_files([path, tmp_path / "missing.py"])
    assert findings
    assert all(f.location == str(path) for f in findings)


def test_assert_clean_no_raise_for_clean() -> None:
    """无高危发现时，assert_clean 不应抛异常。"""
    scanner = SecretScanner()
    low = Finding(location="text", line=1, rule="x", severity="low", redacted_excerpt="a")
    medium = Finding(
        location="text", line=1, rule="y", severity="medium", redacted_excerpt="b"
    )
    scanner.assert_clean([low, medium])


def test_assert_clean_raises_on_high() -> None:
    """存在高危发现时，assert_clean 应抛 SecretLeakError 且消息不含原始密钥。"""
    scanner = SecretScanner()
    raw = "sk-high-secret-123456789"
    high = Finding(
        location="text",
        line=1,
        rule="provider_api_key",
        severity="high",
        redacted_excerpt="[REDACTED]",
    )
    with pytest.raises(SecretLeakError) as excinfo:
        scanner.assert_clean([high])
    assert raw not in str(excinfo.value)


def test_assert_clean_writes_critical_alert(conn: sqlite3.Connection) -> None:
    """提供 AlertManager 时，阻断同时写入一条 SECRET_LEAK_DETECTED 告警且载荷脱敏。"""
    scanner = SecretScanner()
    raw = "sk-alert-secret-123456789"
    high = Finding(
        location="diff",
        line=3,
        rule="provider_api_key",
        severity="high",
        redacted_excerpt="[REDACTED]",
    )
    manager = AlertManager(conn)
    with pytest.raises(SecretLeakError):
        scanner.assert_clean([high], alert_manager=manager, scope="task:t1")
    row = conn.execute(
        "SELECT severity, code, message, title FROM alerts WHERE code=?",
        ("SECRET_LEAK_DETECTED",),
    ).fetchone()
    assert row is not None
    assert row["severity"] == "critical"
    assert raw not in row["message"]
    assert raw not in row["title"]
    assert row["code"] == "SECRET_LEAK_DETECTED"


def test_extra_patterns_extension() -> None:
    """额外规则应被加入扫描并生效。"""
    scanner = SecretScanner(
        extra_patterns=[("custom_key", re.compile(r"CUSTOM-[A-Z0-9]{8}"), "high")]
    )
    findings = scanner.scan_text("value CUSTOM-ABCD1234 end")
    by_rule = _findings_by_rule(findings)
    assert by_rule["custom_key"].rule == "custom_key"
    assert by_rule["custom_key"].severity == "high"
    assert by_rule["custom_key"].location == "text"
    assert "CUSTOM-ABCD1234" not in by_rule["custom_key"].redacted_excerpt
