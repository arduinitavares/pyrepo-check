from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, NoReturn
import re
import tomllib


RUFF_DEFAULT_CANDIDATES = ("src", "tests", "scripts")
BANDIT_DEFAULT_CANDIDATES = ("src",)
TEST_SHORTCUT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
TEST_SHORTCUT_SELECTORS = frozenset(("-m", "-k"))


class InvalidTestShortcutError(ValueError):
    """Raised when configured Test Shortcut data violates version 1."""


class InvalidCoverageConfigError(ValueError):
    """Raised when native Coverage.py configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class TestShortcut:
    name: str
    pytest_args: tuple[str, ...]


@dataclass(frozen=True)
class CoverageConfig:
    config_path: Path
    fail_under: int | float | None


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    ruff_targets: tuple[str, ...]
    bandit_targets: tuple[str, ...]
    frozen: bool
    test_shortcuts: tuple[TestShortcut, ...] = ()
    coverage: CoverageConfig | None = None


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
    pyproject_path = resolved_root / "pyproject.toml"
    table, coverage = _load_configuration_tables(pyproject_path)
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
        coverage=_configured_coverage(coverage, config_path=pyproject_path),
    )


def _load_configuration_tables(
    pyproject_path: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not pyproject_path.exists():
        return {}, None

    with pyproject_path.open("rb") as file:
        data = tomllib.load(file)

    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}, None

    table = tool.get("pyrepo-check", {})
    if not isinstance(table, dict):
        raise ValueError("[tool.pyrepo-check] must be a TOML table")
    raw_coverage = tool.get("coverage")
    if raw_coverage is None:
        return table, None
    if not isinstance(raw_coverage, dict):
        raise InvalidCoverageConfigError(
            f"Invalid coverage configuration in {pyproject_path}: "
            "[tool.coverage] must be a TOML table"
        )
    return table, raw_coverage


def _configured_coverage(
    table: dict[str, Any] | None,
    *,
    config_path: Path,
) -> CoverageConfig | None:
    if table is None:
        return None

    run = table.get("run")
    if not isinstance(run, dict):
        _invalid_coverage(config_path, "[tool.coverage.run] must be a TOML table")
    if run.get("branch") is not True:
        _invalid_coverage(config_path, "[tool.coverage.run].branch must be true")
    if not _has_coverage_source(run):
        _invalid_coverage(
            config_path,
            "[tool.coverage.run] requires a non-empty source, source_pkgs, or source_dirs entry",
        )
    if "parallel" in run and (
        not isinstance(run["parallel"], bool) or run["parallel"]
    ):
        _invalid_coverage(
            config_path,
            "[tool.coverage.run].parallel must be false when present",
        )
    for key in ("concurrency", "patch"):
        if key not in run:
            continue
        value = run[key]
        if (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
            or value
        ):
            _invalid_coverage(
                config_path,
                f"[tool.coverage.run].{key} must be an empty list of strings when present",
            )

    report = table.get("report")
    if report is not None and not isinstance(report, dict):
        _invalid_coverage(config_path, "[tool.coverage.report] must be a TOML table")
    fail_under = None if report is None else report.get("fail_under")
    if fail_under is not None and (
        isinstance(fail_under, bool)
        or not isinstance(fail_under, int | float)
        or not math.isfinite(fail_under)
    ):
        _invalid_coverage(
            config_path,
            "[tool.coverage.report].fail_under must be a finite TOML integer or float",
        )
    return CoverageConfig(config_path=config_path, fail_under=fail_under)


def _has_coverage_source(run: dict[str, Any]) -> bool:
    found = False
    for key in ("source", "source_pkgs", "source_dirs"):
        value = run.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return False
        found = found or any(item.strip() for item in value)
    return found


def _invalid_coverage(config_path: Path, detail: str) -> NoReturn:
    raise InvalidCoverageConfigError(
        f"Invalid coverage configuration in {config_path}: {detail}"
    )


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
            if "\x00" in pytest_args[operand_index]:
                raise InvalidTestShortcutError(
                    f"Invalid Test Shortcut {name!r}: selector {token} "
                    "expression cannot contain NUL"
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
