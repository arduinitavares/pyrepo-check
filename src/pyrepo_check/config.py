from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re
import tomllib


RUFF_DEFAULT_CANDIDATES = ("src", "tests", "scripts")
BANDIT_DEFAULT_CANDIDATES = ("src",)
TEST_SHORTCUT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
TEST_SHORTCUT_SELECTORS = frozenset(("-m", "-k"))


class InvalidTestShortcutError(ValueError):
    """Raised when configured Test Shortcut data violates version 1."""


@dataclass(frozen=True)
class TestShortcut:
    name: str
    pytest_args: tuple[str, ...]


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    ruff_targets: tuple[str, ...]
    bandit_targets: tuple[str, ...]
    frozen: bool
    test_shortcuts: tuple[TestShortcut, ...] = ()


def collect_existing_positionals(
    root: Path,
    positionals: Sequence[str],
) -> frozenset[str]:
    return frozenset(token for token in positionals if _target_exists(root, token))


def _target_exists(root: Path, target: str) -> bool:
    path = Path(target)
    return path.exists() if path.is_absolute() else (root / path).exists()


def load_project_config(root: Path, *, no_frozen: bool = False) -> ProjectConfig:
    resolved_root = root.resolve()
    table = _load_tool_table(resolved_root / "pyproject.toml")
    return ProjectConfig(
        root=resolved_root,
        ruff_targets=_configured_targets(
            table,
            key="ruff_targets",
            default_candidates=RUFF_DEFAULT_CANDIDATES,
            root=resolved_root,
        ),
        bandit_targets=_configured_targets(
            table,
            key="bandit_targets",
            default_candidates=BANDIT_DEFAULT_CANDIDATES,
            root=resolved_root,
        ),
        frozen=(resolved_root / "uv.lock").exists() and not no_frozen,
        test_shortcuts=_configured_test_shortcuts(table, root=resolved_root),
    )


def _load_tool_table(pyproject_path: Path) -> dict[str, Any]:
    if not pyproject_path.exists():
        return {}

    with pyproject_path.open("rb") as file:
        data = tomllib.load(file)

    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}

    table = tool.get("pyrepo-check", {})
    if not isinstance(table, dict):
        raise ValueError("[tool.pyrepo-check] must be a TOML table")
    return table


def _configured_targets(
    table: dict[str, Any],
    *,
    key: str,
    default_candidates: tuple[str, ...],
    root: Path,
) -> tuple[str, ...]:
    raw_targets = table.get(key)
    if raw_targets is not None:
        if not isinstance(raw_targets, list) or not all(
            isinstance(item, str) for item in raw_targets
        ):
            raise ValueError(f"{key} must be a list of strings")
        if not raw_targets:
            raise ValueError(f"{key} must not be empty")
        return tuple(raw_targets)

    detected = tuple(path for path in default_candidates if (root / path).exists())
    return detected or (".",)


def _configured_test_shortcuts(
    table: dict[str, Any],
    *,
    root: Path,
) -> tuple[TestShortcut, ...]:
    raw_shortcuts = table.get("test-shortcuts")
    if raw_shortcuts is None:
        return ()
    if not isinstance(raw_shortcuts, dict):
        raise InvalidTestShortcutError(
            "[tool.pyrepo-check.test-shortcuts] must be a TOML table"
        )

    shortcuts: list[TestShortcut] = []
    for name, raw_args in raw_shortcuts.items():
        if TEST_SHORTCUT_NAME_PATTERN.fullmatch(name) is None:
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut name {name!r}: "
                "must match [a-z][a-z0-9_-]*"
            )
        if (
            not isinstance(raw_args, list)
            or not raw_args
            or not all(isinstance(arg, str) for arg in raw_args)
        ):
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: "
                "value must be a non-empty list of strings"
            )
        pytest_args = tuple(raw_args)
        _validate_test_shortcut(name, pytest_args, root=root)
        shortcuts.append(TestShortcut(name=name, pytest_args=pytest_args))
    return tuple(shortcuts)


def _validate_test_shortcut(
    name: str,
    pytest_args: tuple[str, ...],
    *,
    root: Path,
) -> None:
    seen_selectors: set[str] = set()
    index = 0
    while index < len(pytest_args):
        token = pytest_args[index]
        if token in TEST_SHORTCUT_SELECTORS:
            if token in seen_selectors:
                raise InvalidTestShortcutError(
                    f"Invalid Test Shortcut {name!r}: "
                    f"selector {token} may appear at most once"
                )
            operand_index = index + 1
            if (
                operand_index >= len(pytest_args)
                or not pytest_args[operand_index].strip()
                or pytest_args[operand_index].lstrip().startswith("-")
            ):
                raise InvalidTestShortcutError(
                    f"Invalid Test Shortcut {name!r}: selector {token} "
                    "requires one non-empty expression that does not begin with '-'"
                )
            seen_selectors.add(token)
            index += 2
            continue

        if not token.strip():
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: target must not be empty"
            )
        if token.startswith("-"):
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: unsupported option token: {token}"
            )
        _validate_test_shortcut_target(name, token, root=root)
        index += 1


def _validate_test_shortcut_target(name: str, token: str, *, root: Path) -> None:
    path_text = token.split("::", 1)[0]
    try:
        path = Path(path_text)
        if not path_text or path.is_absolute():
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: "
                f"target must be project-relative: {token}"
            )
        resolved_root = root.resolve()
        resolved_target = (resolved_root / path).resolve(strict=False)
    except InvalidTestShortcutError:
        raise
    except (ValueError, OSError, RuntimeError) as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target path cannot be inspected safely: {path_text}"
        ) from error
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: target escapes project root: {token}"
        ) from error
    try:
        resolved_target.stat()
    except FileNotFoundError:
        target_exists = False
    except (ValueError, OSError, RuntimeError) as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target path cannot be inspected safely: {path_text}"
        ) from error
    else:
        target_exists = True
    if not target_exists:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target path does not exist beneath project root: {path_text}"
        )
