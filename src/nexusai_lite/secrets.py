"""Provider API 密钥与凭据管理。

密钥仅存于环境变量或本地受控配置（``.env``），绝不进入代码、日志或 Prompt。
载入的密钥自动登记到进程级注册表，供日志脱敏使用。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


class _EnvLoaderState:
    """进程级 ``.env`` 加载状态（避免使用 global 语句）。"""

    loaded: bool = False


_env_state = _EnvLoaderState()
_registered_secrets: set[str] = set()


def _ensure_env_loaded() -> None:
    """惰性地从项目根 / CWD 加载 ``.env``（仅一次，不覆盖既有环境变量）。"""
    if not _env_state.loaded:
        load_dotenv(override=False)
        _env_state.loaded = True


def register_secret(value: str | None) -> None:
    """把一个秘密值登记到进程级注册表（用于日志脱敏）。

    传入 None 或空串则忽略。
    """
    if value:
        _registered_secrets.add(value)


def get_registered_secrets() -> tuple[str, ...]:
    """返回当前进程已登记的全部秘密值（用于日志脱敏）。"""
    return tuple(_registered_secrets)


def get_provider_api_key(provider: str) -> str | None:
    """读取某个 Provider 的 API Key。

    环境变量命名为 ``{PROVIDER}_API_KEY``（如 ``DEEPSEEK_API_KEY``）。
    若 ``.env`` 存在则先载入（不覆盖既有环境变量）。读取到的值会自动登记
    到注册表以便日志脱敏，并默认缓存于进程级注册表（不在此处返回明文缓存，
    明文始终来自环境变量）。

    Args:
        provider: Provider 名（自动转为大写）。

    Returns:
        API Key 或 None（未配置）。
    """
    _ensure_env_loaded()
    value = os.environ.get(f"{provider.upper()}_API_KEY")
    if value:
        register_secret(value)
    return value


def mask_secret(value: str) -> str:
    """把秘密值脱敏为可展示形式（至多保留末尾 4 字符）。

    返回 ``"***<last up to 4 chars>"``；绝不返回完整值。
    """
    if not value:
        return ""
    tail = value[-4:]
    return f"***{tail}"
