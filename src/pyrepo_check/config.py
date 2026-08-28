from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any, NoReturn
import re
import tomllib

from pyrepo_check.artifact_safety import (
    _BoundedReadError,
    _UnsafePathError,
    read_regular_file,
)


RUFF_DEFAULT_CANDIDATES = ("src", "tests", "scripts")
BANDIT_DEFAULT_CANDIDATES = ("src",)
TEST_SHORTCUT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
TEST_SHORTCUT_SELECTORS = frozenset(("-m", "-k"))
PYPROJECT_MAX_BYTES = 1024 * 1024


class InvalidTestShortcutError(ValueError):
    """Raised when configured Test Shortcut data violates version 1."""


class InvalidCoverageConfigError(ValueError):
    """Raised when native Coverage.py configuration is incomplete or unsafe."""


class InvalidProjectConfigError(ValueError):
    """Raised when pyproject.toml cannot be read and parsed safely."""


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
    test_shortcuts: tuple[TestShortcut, ...] = ()
    coverage: CoverageConfig | None = None
    pyproject_sha256: str | None = None


def collect_existing_positionals(
    root: Path,
    positionals: Sequence[str],
) -> frozenset[str]:
    return frozenset(token for token in positionals if _target_exists(root, token))


def _target_exists(root: Path, target: str) -> bool:
    try:
        validate_project_target(root, target)
    except ValueError:
        return False
    return True


def validate_project_target(root: Path, target: str) -> str:
    """Validate one path target and preserve an optional pytest node selector."""
    path_text = validate_project_target_syntax(target)
    separator = "::" if "::" in target else ""
    selector = target.split("::", 1)[1] if separator else ""
    path = Path(path_text)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_target = (resolved_root / path).resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except FileNotFoundError as error:
        raise ValueError("target must exist beneath the project root") from error
    except ValueError as error:
        raise ValueError("target must remain beneath the project root") from error
    except (OSError, RuntimeError) as error:
        raise ValueError("target path cannot be inspected safely") from error
    return path_text + (separator + selector if separator else "")


def validate_project_target_syntax(target: str) -> str:
    """Validate target syntax without reading repository state."""
    path_text = target.split("::", 1)[0]
    if not target.strip() or not path_text:
        raise ValueError("target must not be empty")
    if "\x00" in target:
        raise ValueError("target must not contain NUL")
    if target.lstrip().startswith("-"):
        raise ValueError("target must not begin like an option")
    path = Path(path_text)
    if path.is_absolute():
        raise ValueError("target must be project-relative")
    if ".." in path.parts:
        raise ValueError("target must not contain '..'")
    return path_text


def load_project_config(root: Path) -> ProjectConfig:
    resolved_root = root.resolve()
    pyproject_path = resolved_root / "pyproject.toml"
    table, coverage, pyproject_sha256 = _load_configuration_tables(pyproject_path)
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
        test_shortcuts=_configured_test_shortcuts(table, root=resolved_root),
        coverage=_configured_coverage(coverage, config_path=pyproject_path),
        pyproject_sha256=pyproject_sha256,
    )


def _load_configuration_tables(
    pyproject_path: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    try:
        content = read_regular_file(
            pyproject_path,
            max_bytes=PYPROJECT_MAX_BYTES,
        )
    except FileNotFoundError:
        return {}, None, None
    except (_BoundedReadError, _UnsafePathError, OSError) as error:
        raise InvalidProjectConfigError(
            f"Invalid project configuration in {pyproject_path}: "
            f"cannot read a stable regular file ({error})"
        ) from error
    try:
        data = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise InvalidProjectConfigError(
            f"Invalid project configuration in {pyproject_path}: "
            f"cannot parse UTF-8 TOML ({error})"
        ) from error
    pyproject_sha256 = hashlib.sha256(content).hexdigest()

    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}, None, pyproject_sha256

    table = tool.get("pyrepo-check", {})
    if not isinstance(table, dict):
        raise ValueError("[tool.pyrepo-check] must be a TOML table")
    raw_coverage = tool.get("coverage")
    if raw_coverage is None:
        return table, None, pyproject_sha256
    if not isinstance(raw_coverage, dict):
        raise InvalidCoverageConfigError(
            f"Invalid coverage configuration in {pyproject_path}: "
            "[tool.coverage] must be a TOML table"
        )
    return table, raw_coverage, pyproject_sha256


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
    if "parallel" in run and (not isinstance(run["parallel"], bool) or run["parallel"]):
        _invalid_coverage(
            config_path,
            "[tool.coverage.run].parallel must be false when present",
        )
    for key in ("concurrency", "patch"):
        if key not in run:
            continue
        value = run[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or value:
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
    raise InvalidCoverageConfigError(f"Invalid coverage configuration in {config_path}: {detail}")


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
        validated: list[str] = []
        for target in raw_targets:
            try:
                validated.append(validate_project_target(root, target))
            except ValueError as error:
                raise ValueError(f"{key} contains an invalid target {target!r}: {error}") from error
        return tuple(validated)

    detected: list[str] = []
    for target in default_candidates:
        try:
            (root / target).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            pass
        detected.append(validate_project_target(root, target))
    return tuple(detected) or (validate_project_target(root, "."),)


def _configured_test_shortcuts(
    table: dict[str, Any],
    *,
    root: Path,
) -> tuple[TestShortcut, ...]:
    raw_shortcuts = table.get("test-shortcuts")
    if raw_shortcuts is None:
        return ()
    if not isinstance(raw_shortcuts, dict):
        raise InvalidTestShortcutError("[tool.pyrepo-check.test-shortcuts] must be a TOML table")

    shortcuts: list[TestShortcut] = []
    for name, raw_args in raw_shortcuts.items():
        if TEST_SHORTCUT_NAME_PATTERN.fullmatch(name) is None:
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut name {name!r}: must match [a-z][a-z0-9_-]*"
            )
        if (
            not isinstance(raw_args, list)
            or not raw_args
            or not all(isinstance(arg, str) for arg in raw_args)
        ):
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: value must be a non-empty list of strings"
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
                    f"Invalid Test Shortcut {name!r}: selector {token} may appear at most once"
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
    try:
        validate_project_target(root, token)
    except ValueError as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: target path cannot be inspected safely: "
            f"{token.split('::', 1)[0]}"
        ) from error
