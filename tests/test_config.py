from pathlib import Path

import pytest

from pyrepo_check.config import ProjectConfig, load_project_config


def test_loads_pyproject_targets(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyrepo-check]
ruff_targets = ["src/pkg", "tests", "scripts"]
bandit_targets = ["src/pkg"]
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    config = load_project_config(tmp_path)

    assert config == ProjectConfig(
        root=tmp_path,
        ruff_targets=("src/pkg", "tests", "scripts"),
        bandit_targets=("src/pkg",),
        frozen=True,
    )


def test_detects_default_targets_without_config(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()

    config = load_project_config(tmp_path)

    assert config.ruff_targets == ("src", "tests", "scripts")
    assert config.bandit_targets == ("src",)
    assert config.frozen is False


def test_falls_back_to_current_directory_when_no_targets_exist(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)

    assert config.ruff_targets == (".",)
    assert config.bandit_targets == (".",)


def test_no_frozen_overrides_uv_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    config = load_project_config(tmp_path, no_frozen=True)

    assert config.frozen is False


def test_rejects_non_list_target_config(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyrepo-check]
ruff_targets = "src"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ruff_targets must be a list of strings"):
        load_project_config(tmp_path)
