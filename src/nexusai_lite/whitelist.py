"""命令/目录白名单与工具参数校验（S3-04）。

落实 §4.1、F7、§12 规则 5/7：使用**显式命令白名单**（非黑名单）、目录白名单与
路径规范化，保证 Agent 只能访问任务明确授权的仓库、目录与命令，且沙箱与白名单
不得被绕过。

- :class:`CommandWhitelist`：显式命令 basename 允许列表，默认禁止任意 shell 命令。
- :class:`DirectoryWhitelist`：根目录允许列表，统一用 :meth:`Path.resolve` 规范化后
  拒绝越界路径，防止 ``..`` 与符号链接逃逸。
- :func:`validate_tool_call`：对工具调用做结构化校验，产出 :class:`ValidatedCommand`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# shell 元字符：放在 argv 任一元素中即拒绝，防止参数注入与命令拼接。
_SHELL_METACHARACTERS: frozenset[str] = frozenset({";", "|", "&", "$", "`", ">", "<", "\n"})


class DangerousCommandError(ValueError):
    """命令不在白名单内或包含被禁止的 shell 元字符。"""


class PathNotAllowedError(ValueError):
    """解析后的路径落在授权目录之外。"""


@dataclass(frozen=True, slots=True)
class ValidatedCommand:
    """通过校验后的工具调用：规范化 argv 与解析后的绝对 cwd。"""

    argv: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True, slots=True)
class CommandWhitelist:
    """显式命令 basename 允许列表（默认最小权限，绝不使用黑名单）。

    Args:
        allowed: 允许执行的命令 basename（如 ``python``、``pytest``、``ruff``、``git``）。
    """

    allowed: frozenset[str] = field(default_factory=frozenset)

    def __init__(self, allowed: Iterable[str] = ()) -> None:
        """初始化白名单，把可迭代输入规范化为小写命令名集合。

        Args:
            allowed: 允许的命令 basename 可迭代对象。
        """
        # 手动 __init__ 以便把任意可迭代输入规范化为小写 frozenset。
        object.__setattr__(
            self,
            "allowed",
            frozenset(_normalize_command(name) for name in allowed),
        )

    def is_allowed(self, argv: Sequence[str]) -> bool:
        """判断 argv 的命令 basename 是否在允许列表中。

        空 argv 视为不允许。
        """
        if len(argv) == 0:
            return False
        return _normalize_command(argv[0]) in self.allowed

    def require(self, argv: Sequence[str]) -> None:
        """校验 argv 属于白名单；否则抛出 :class:`DangerousCommandError`。"""
        if not self.is_allowed(argv):
            msg = f"命令不在白名单内: {argv!r}"
            raise DangerousCommandError(msg)


@dataclass(frozen=True, slots=True)
class DirectoryWhitelist:
    """根目录允许列表，用于规范化并约束路径落在授权目录内。

    基于 :meth:`Path.resolve`（展开符号链接并归一化 ``..``）判断，任何解析后
    落在所有 ``roots`` 之外的路径都会抛出 :class:`PathNotAllowedError`。
    """

    roots: tuple[Path, ...] = field(default_factory=tuple)

    def __init__(self, roots: Iterable[Path] = ()) -> None:
        """初始化目录白名单，把根目录规范化为解析后的绝对路径。

        Args:
            roots: 允许的根目录可迭代对象。
        """
        object.__setattr__(
            self,
            "roots",
            tuple(_resolve_root(path) for path in roots),
        )

    def resolve(self, path: Path | str) -> Path:
        """规范化 ``path``：解析符号链接与 ``..``，并拒绝越界路径。"""
        resolved = Path(path).resolve()
        for root in self.roots:
            if resolved == root or _is_within(resolved, root):
                return resolved
        msg = f"路径不在授权目录内: {resolved}"
        raise PathNotAllowedError(msg)

    def allows(self, path: Path | str) -> Path:
        """同 :meth:`resolve`，但越界时抛出 :class:`PathNotAllowedError`。"""
        return self.resolve(path)


def validate_tool_call(
    argv: Sequence[str],
    *,
    cwd: Path,
    roots: Iterable[Path],
    commands: CommandWhitelist,
) -> ValidatedCommand:
    """对工具调用做结构化校验，产出可交给沙箱执行的 :class:`ValidatedCommand`。

    - 拒绝空 argv（§12 规则 7：非授权即拒绝）。
    - ``argv[0]`` basename 必须在命令白名单内（§12 规则 5，禁止黑名单）。
    - ``argv`` 任一元素不得包含 shell 元字符（``; | & $ ` > <`` 与换行），
      防止参数注入 / 命令拼接绕过白名单。
    - 拒绝解析后指向 ``roots`` 之外的绝对路径参数。

    Args:
        argv: 待校验的完整命令行（argv[0] 为命令名）。
        cwd: 命令工作目录，会被目录白名单规范化。
        roots: 允许的根目录集合。
        commands: 命令白名单。

    Returns:
        通过校验的 :class:`ValidatedCommand`。

    Raises:
        DangerousCommandError: 命令不在白名单或含 shell 元字符。
        PathNotAllowedError: cwd 或绝对路径参数落在授权目录之外。
    """
    if not isinstance(argv, (list, tuple)) or len(argv) == 0:
        msg = "工具调用缺少命令（空 argv）"
        raise DangerousCommandError(msg)

    argv_list = [str(element) for element in argv]

    # 命令必须显式写在白名单内。
    commands.require(argv_list)

    # 拒绝任何含 shell 元字符的参数（含命令名自身）。
    for element in argv_list:
        if not element:
            msg = f"参数为空字符串: {element!r}"
            raise DangerousCommandError(msg)
        if any(ch in element for ch in _SHELL_METACHARACTERS):
            msg = f"参数含 shell 元字符: {element!r}"
            raise DangerousCommandError(msg)

    # 规范化工作目录并约束在授权根目录内。
    directory_roots = DirectoryWhitelist(roots)
    resolved_cwd = directory_roots.resolve(cwd)

    # 拒绝解析后越界的绝对路径参数。
    for element in argv_list[1:]:
        if element.startswith("/"):
            directory_roots.resolve(element)

    return ValidatedCommand(argv=tuple(argv_list), cwd=resolved_cwd)


def _normalize_command(name: str) -> str:
    """把命令名规范化为小写字符串以做匹配（Windows 大小写不敏感习惯）。"""
    return name.strip().lower()


def _resolve_root(path: Path) -> Path:
    """规范化授权根目录路径（展开符号链接与 ``..``）。"""
    return path.resolve()


def _is_within(path: Path, root: Path) -> bool:
    """判断 ``path`` 是否位于 ``root`` 之下（含嵌套多级）。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
