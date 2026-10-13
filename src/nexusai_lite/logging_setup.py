"""结构化日志与数据脱敏（S1-07）。

配置一个 ``nexusai`` 包级 logger（propagate=False，单一 StreamHandler），
handler 附带 :class:`RedactionFilter`，在写出前对每条记录做敏感信息脱敏。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from nexusai_lite import secrets

PACKAGE_LOGGER_NAME = "nexusai"

# 匹配常见密钥形式的正则集合：(regex, 说明) —— 匹配到即替换为 [REDACTED]。
# ``(?:\s+\S+)*`` 使授权头/Bearer/Key 值能吞掉含空格的整个 token 串。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{10,}"), "sk- 密钥"),
    (re.compile(r"(?i)authorization[\s:]*\S+(?:\s+\S+)*"), "Authorization header"),
    (re.compile(r"(?i)bearer\s+\S+(?:\s+\S+)*"), "Bearer token"),
    (
        re.compile(r"(?i)(?:api[_-]?key|token|secret)['\"\s:=]+\S+(?:\s+\S+)*"),
        "api key/token/secret",
    ),
    (re.compile(r"(?i)cookie[\s:]*\S+(?:\s+\S+)*"), "cookie"),
)

_REDACTED = "[REDACTED]"


class RedactionFilter(logging.Filter):
    """日志过滤器：把记录中的已登记秘密与敏感模式替换为 ``[REDACTED]``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """对记录做脱敏后放行（始终返回 True）。"""
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = tuple(
                redact_text(str(a)) if isinstance(a, str) else a for a in record.args
            )
        if record.exc_info and record.exc_info[2]:
            record.exc_text = redact_text(record.exc_text or "")
        return True


class _JsonFormatter(logging.Formatter):
    """JSON 格式器：输出 ``{"ts", "level", "logger", "message"}``。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        return json.dumps(payload)


class _RedactingFormatter(logging.Formatter):
    """文本格式器：对最终渲染结果再做一次脱敏（覆盖非字符串 args 的泄露路径）。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def redact_text(text: str) -> str:
    """对输入文本做敏感信息脱敏（纯函数，可单元测试）。

    1. 把每个已登记秘密值替换为 ``[REDACTED]``；
    2. 依次应用内置敏感模式（密钥 / Authorization / Bearer / api key / cookie）。
    """
    result = text
    for secret in secrets.get_registered_secrets():
        if secret:
            result = result.replace(secret, _REDACTED)
    for pattern, _desc in _PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


def setup_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """配置 ``nexusai`` 包级 logger。

    Args:
        level: 日志级别（"DEBUG" / "INFO" / "WARNING" / "ERROR"）。
        json_output: 为 True 时输出 JSON 行，否则输出纯文本。
    """
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False

    # 覆盖已有 handler，避免重复添加。
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler()
    if json_output:
        formatter = _JsonFormatter()
    else:
        formatter = _RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(level.upper())
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
