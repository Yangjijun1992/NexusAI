"""命令/目录白名单与工具参数校验测试（S3-04）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nexusai_lite.whitelist import (
    CommandWhitelist,
    DangerousCommandError,
    DirectoryWhitelist,
    PathNotAllowedError,
    ValidatedCommand,
    validate_tool_call,
)

if TYPE_CHECKING:
    from pathlib import Path


def _cmds(*names: str) -> CommandWhitelist:
    return CommandWhitelist(names)


# ---------- CommandWhitelist ----------


def test_command_allowlist_allows_known_command() -> None:
    """白名单内的命令应被允许。"""
    whitelist = _cmds("python", "pytest")
    assert whitelist.is_allowed(["python", "-c", "print(1)"])
    assert whitelist.is_allowed(["PYTHON"])  # 大小写不敏感


def test_command_allowlist_rejects_unknown() -> None:
    """白名单外的命令应被拒绝（而非黑名单放行）。"""
    whitelist = _cmds("python")
    assert not whitelist.is_allowed(["rm", "-rf", "/"])
    assert not whitelist.is_allowed(["sh", "-c", "echo hi"])


def test_command_require_raises_for_unknown() -> None:
    """require 对非白名单命令应抛出 DangerousCommandError。"""
    with pytest.raises(DangerousCommandError):
        _cmds("python").require(["bash", "-c", "true"])


def test_command_allowlist_rejects_empty() -> None:
    """空 argv 应被拒绝。"""
    assert not _cmds("python").is_allowed([])


# ---------- DirectoryWhitelist ----------


def test_directory_resolve_within_root(tmp_path: Path) -> None:
    """位于根内（含嵌套）的路径应解析。"""
    sub = tmp_path / "repo" / "src"
    sub.mkdir(parents=True)
    whitelist = DirectoryWhitelist([tmp_path])
    assert whitelist.resolve(sub) == sub.resolve()


def test_directory_traversal_escape_rejected(tmp_path: Path) -> None:
    """``..`` 越界应抛出 PathNotAllowedError。"""
    outside = tmp_path.parent
    whitelist = DirectoryWhitelist([tmp_path])
    with pytest.raises(PathNotAllowedError):
        whitelist.resolve(outside / "victim")


def test_directory_symlink_escape_rejected(tmp_path: Path) -> None:
    """符号链接指向根外应被拒绝（resolve 展开符号链接）。"""
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "esc"  # 位于根内但符号链接指向根外
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("环境不支持创建符号链接")
    whitelist = DirectoryWhitelist([tmp_path])
    with pytest.raises(PathNotAllowedError):
        whitelist.resolve(link)


def test_directory_absolute_outside_rejected(tmp_path: Path) -> None:
    """根外绝对路径应被拒绝。"""
    whitelist = DirectoryWhitelist([tmp_path])
    with pytest.raises(PathNotAllowedError):
        whitelist.resolve(tmp_path.parent / "other")


# ---------- validate_tool_call ----------


def test_validate_rejects_empty_argv(tmp_path: Path) -> None:
    """空 argv 应被拒绝。"""
    with pytest.raises(DangerousCommandError):
        validate_tool_call([], cwd=tmp_path, roots=[tmp_path], commands=_cmds("python"))


def test_validate_rejects_non_allowlisted_command(tmp_path: Path) -> None:
    """非白名单命令应被拒绝。"""
    with pytest.raises(DangerousCommandError):
        validate_tool_call(
            ["rm", "-rf", "/"], cwd=tmp_path, roots=[tmp_path], commands=_cmds("python")
        )


def test_validate_rejects_shell_metachar(tmp_path: Path) -> None:
    """argv 元素含 shell 元字符应被拒绝。"""
    dangerous = [
        ["python", "-c", "print(1); rm -rf /"],
        ["python", "script.py|python"],
        ["python", "x&y"],
        ["python", "$(whoami)"],
        ["python", "`id`"],
        ["python", "a>b"],
        ["python", "a<b"],
        ["python", "line\nbreak"],
    ]
    for argv in dangerous:
        with pytest.raises(DangerousCommandError):
            validate_tool_call(
                argv, cwd=tmp_path, roots=[tmp_path], commands=_cmds("python")
            )


def test_validate_rejects_cwd_outside_roots(tmp_path: Path) -> None:
    """cwd 落在授权根之外应被拒绝。"""
    with pytest.raises(PathNotAllowedError):
        validate_tool_call(
            ["python", "-c", "pass"],
            cwd=tmp_path.parent,
            roots=[tmp_path],
            commands=_cmds("python"),
        )


def test_validate_rejects_absolute_path_outside_roots(tmp_path: Path) -> None:
    """绝对路径参数解析后越界应被拒绝。"""
    with pytest.raises(PathNotAllowedError):
        validate_tool_call(
            ["python", str(tmp_path.parent / "evil.py")],
            cwd=tmp_path,
            roots=[tmp_path],
            commands=_cmds("python"),
        )


def test_validate_returns_normalized_command(tmp_path: Path) -> None:
    """合法调用应返回规范化 ValidatedCommand（argv tuple + 绝对 cwd）。"""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    result = validate_tool_call(
        ["python", "-c", "print('hi')"],
        cwd=repo,
        roots=[tmp_path],
        commands=_cmds("python"),
    )
    assert isinstance(result, ValidatedCommand)
    assert result.argv == ("python", "-c", "print('hi')")
    assert result.cwd == repo.resolve()
