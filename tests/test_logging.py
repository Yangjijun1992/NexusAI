"""日志脱敏与 JSON 格式测试（S1-07）。"""

from __future__ import annotations

import io
import json
import logging

from nexusai_lite import secrets
from nexusai_lite.logging_setup import PACKAGE_LOGGER_NAME, redact_text, setup_logging


def _capture_logger(*, json_output: bool) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    setup_logging(level="INFO", json_output=json_output)
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    # setup_logging 已为 logger 安装了带 RedactionFilter 的 StreamHandler；
    # 这里仅把该 handler 的流重定向到内存，以断言实际过滤后的输出。
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(stream)
    return logger, stream


def test_redaction_registered_value() -> None:
    """已登记的秘密值应被替换为 [REDACTED]。"""
    secrets.register_secret("sk-registered-value-123456")
    text = "request auth sk-registered-value-123456 end"
    assert redact_text(text) == "request auth [REDACTED] end"
    assert "sk-registered-value-123456" not in redact_text(text)


def test_redaction_sk_pattern() -> None:
    """sk- 密钥模式应被替换。"""
    out = redact_text("key=sk-abcdefghijklmnopqrstuvwxyz123")
    assert "[REDACTED]" in out
    assert "sk-abcdefghijklmnopqrstuvwxyz123" not in out


def test_redaction_authorization_pattern() -> None:
    """Authorization header 模式应被替换。"""
    out = redact_text("Authorization: Bearer abcdef123456")
    assert "[REDACTED]" in out
    assert "abcdef123456" not in out


def test_redaction_bearer_pattern() -> None:
    """Bearer token 模式应被替换。"""
    out = redact_text("token = Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    assert "[REDACTED]" in out
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out


def test_redaction_apikey_pattern() -> None:
    """api_key/token/secret 赋值模式应被替换。"""
    out = redact_text("api_key: sk-cafebabe1234")
    assert "[REDACTED]" in out
    assert "sk-cafebabe1234" not in out


def test_redaction_cookie_pattern() -> None:
    """cookie 模式应被替换。"""
    out = redact_text("Cookie: session=abc123; auth=xyz")
    assert "[REDACTED]" in out
    assert "session=abc123" not in out


def test_json_formatter_fields_present() -> None:
    """JSON 输出应包含 ts/level/logger/message 字段。"""
    stream_logger, stream = _capture_logger(json_output=True)
    stream_logger.info("hello world")
    payload = json.loads(stream.getvalue().strip())
    assert {"ts", "level", "logger", "message"} <= set(payload)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == PACKAGE_LOGGER_NAME


def test_log_output_redacts_raw_secret() -> None:
    """日志输出应脱敏：出现 [REDACTED] 且不含原始秘密。"""
    raw = "sk-super-raw-secret-value-9999"
    secrets.register_secret(raw)
    stream_logger, stream = _capture_logger(json_output=False)
    stream_logger.info("connecting with %s", raw)
    text = stream.getvalue()
    assert "[REDACTED]" in text
    assert raw not in text


def test_log_output_redacts_pattern_no_raw() -> None:
    """日志输出应替换模式样例且不含原始 token。"""
    # sk-pattern-secret-abcdefgh 含连字符，不属于 sk-[A-Za-z0-9]{10,} 模式，
    # 故显式登记，证明注册表脱敏链路（而非仅靠正则）不泄露原始值。
    raw_sk = "sk-pattern-secret-abcdefgh"
    secrets.register_secret(raw_sk)
    stream_logger, stream = _capture_logger(json_output=False)
    stream_logger.info("using key %s and Bearer abcXYZ123", raw_sk)
    text = stream.getvalue()
    assert "[REDACTED]" in text
    assert raw_sk not in text
    assert "abcXYZ123" not in text
