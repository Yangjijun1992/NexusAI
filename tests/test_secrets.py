"""密钥与凭据管理测试（S1-06）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexusai_lite import secrets

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_get_provider_api_key_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """应从环境变量读取 ``{PROVIDER}_API_KEY``（provider 自动大写）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-testvalue1234")
    assert secrets.get_provider_api_key("deepseek") == "sk-testvalue1234"
    assert secrets.get_provider_api_key("DEEPSEEK") == "sk-testvalue1234"


def test_get_provider_api_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时返回 None。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert secrets.get_provider_api_key("deepseek") is None


def test_dotenv_ignored_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CWD 无 .env 时不报错，返回 None。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # 该目录不存在 .env
    assert secrets.get_provider_api_key("deepseek") is None


def test_mask_secret_never_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """mask_secret 至多保留末尾 4 字符，绝不返回完整值。"""
    full = "sk-abcdefghijklmnop123456"
    masked = secrets.mask_secret(full)
    assert masked != full
    assert masked == "***3456"  # 末尾 4 字符
    assert full not in masked


def test_mask_secret_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """短值与空值也应安全处理。"""
    assert secrets.mask_secret("") == ""
    assert secrets.mask_secret("ab") == "***ab"


def test_register_and_retrieve(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_secret / get_registered_secrets 应正常工作。"""
    monkeypatch.setattr(secrets, "_registered_secrets", set())
    secrets.register_secret("super-secret-value")
    secrets.register_secret(None)
    secrets.register_secret("")
    assert "super-secret-value" in secrets.get_registered_secrets()
    assert len(secrets.get_registered_secrets()) == 1


def test_get_provider_api_key_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    """读取 API Key 时应自动登记到注册表（用于日志脱敏）。"""
    monkeypatch.setattr(secrets, "_registered_secrets", set())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-needle-value")
    secrets.get_provider_api_key("deepseek")
    assert "sk-needle-value" in secrets.get_registered_secrets()
