from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib


RUFF_DEFAULT_CANDIDATES = ("src", "tests", "scripts")
BANDIT_DEFAULT_CANDIDATES = ("src",)


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    ruff_targets: tuple[str, ...]
    bandit_targets: tuple[str, ...]
    frozen: bool


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
