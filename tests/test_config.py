import json
from pathlib import Path
import re

import pytest

from pyrepo_check.config import (
    InvalidTestShortcutError,
    ProjectConfig,
    TestShortcut as ConfigTestShortcut,
    collect_existing_positionals,
    load_project_config,
)


def _write_test_shortcuts(
    root: Path,
    shortcuts: dict[str, object] | str,
) -> None:
    if isinstance(shortcuts, str):
        shortcut_toml = shortcuts
    else:
        shortcut_toml = "\n".join(
            f"{json.dumps(name)} = {json.dumps(args)}"
            for name, args in shortcuts.items()
        )
    (root / "pyproject.toml").write_text(
        "[tool.pyrepo-check.test-shortcuts]\n" + shortcut_toml,
        encoding="utf-8",
    )


def test_loads_no_test_shortcuts_when_table_is_absent(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)

    assert config.test_shortcuts == ()


def test_loads_test_shortcuts_without_changing_tokens_or_order(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "test_cli.py").write_text("", encoding="utf-8")
    _write_test_shortcuts(
        tmp_path,
        {
            "unit": ["tests/unit"],
            "integration": ["-m", "integration"],
            "cli": ["tests/test_cli.py", "-k", "json", "tests/test_cli.py"],
        },
    )

    config = load_project_config(tmp_path)

    assert config.test_shortcuts == (
        ConfigTestShortcut("unit", ("tests/unit",)),
        ConfigTestShortcut("integration", ("-m", "integration")),
        ConfigTestShortcut(
            "cli",
            ("tests/test_cli.py", "-k", "json", "tests/test_cli.py"),
        ),
    )


@pytest.mark.parametrize("name", ("a", "a0", "unit-tests", "unit_tests"))
def test_loads_valid_test_shortcut_name_boundaries(tmp_path: Path, name: str) -> None:
    (tmp_path / "tests").mkdir()
    _write_test_shortcuts(tmp_path, {name: ["tests"]})

    assert load_project_config(tmp_path).test_shortcuts == (
        ConfigTestShortcut(name, ("tests",)),
    )


@pytest.mark.parametrize(
    ("shortcut_toml", "message"),
    (
        ('test-shortcuts = "tests"', "[tool.pyrepo-check.test-shortcuts] must be a TOML table"),
        ('test-shortcuts = ["tests"]', "[tool.pyrepo-check.test-shortcuts] must be a TOML table"),
    ),
)
def test_rejects_non_table_test_shortcuts(
    tmp_path: Path, shortcut_toml: str, message: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyrepo-check]\n" + shortcut_toml,
        encoding="utf-8",
    )

    with pytest.raises(InvalidTestShortcutError, match=re.escape(message)):
        load_project_config(tmp_path)


@pytest.mark.parametrize("name", ("Unit", "1unit", "unit.test", "unit test", ""))
def test_rejects_invalid_test_shortcut_names(tmp_path: Path, name: str) -> None:
    _write_test_shortcuts(tmp_path, {name: ["tests"]})

    with pytest.raises(
        InvalidTestShortcutError,
        match=rf"Invalid Test Shortcut name {name!r}: must match \[a-z\]\[a-z0-9_-\]\*",
    ):
        load_project_config(tmp_path)


@pytest.mark.parametrize("value", ("tests", [], ["tests", 3]))
def test_rejects_invalid_test_shortcut_values(tmp_path: Path, value: object) -> None:
    _write_test_shortcuts(tmp_path, {"unit": value})

    with pytest.raises(
        InvalidTestShortcutError,
        match="Invalid Test Shortcut 'unit': value must be a non-empty list of strings",
    ):
        load_project_config(tmp_path)


@pytest.mark.parametrize(
    "args",
    (
        ("tests/test_one.py",),
        ("-m", "unit"),
        ("-k", "json"),
        ("-m", "unit", "-k", "json"),
        ("-k", "json", "-m", "unit"),
        ("tests/test_one.py", "-m", "unit", "tests"),
    ),
)
def test_accepts_test_shortcut_grammar_shapes(tmp_path: Path, args: tuple[str, ...]) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").write_text("", encoding="utf-8")
    _write_test_shortcuts(tmp_path, {"unit": list(args)})

    assert load_project_config(tmp_path).test_shortcuts == (
        ConfigTestShortcut("unit", args),
    )


@pytest.mark.parametrize(
    "target",
    (
        "tests/test_one.py",
        "tests",
        "tests/test_one.py::test_name",
        ".",
        "tests/test_one.py",
        "tests/../tests/test_one.py",
    ),
)
def test_accepts_existing_contained_test_targets(tmp_path: Path, target: str) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").write_text("", encoding="utf-8")
    _write_test_shortcuts(tmp_path, {"unit": [target]})

    assert load_project_config(tmp_path).test_shortcuts == (
        ConfigTestShortcut("unit", (target,)),
    )


@pytest.mark.parametrize(
    "args",
    (
        ("-m", "unit", "-m", "fast"),
        ("-k", "one", "-k", "two"),
        ("-m",),
        ("-k", ""),
        ("-m", "  "),
        ("-k", "-not-json"),
        ("--maxfail=1",),
        ("-x",),
        ("-p",),
        ("",),
        ("missing.py",),
        ("../escape",),
    ),
)
def test_rejects_invalid_test_shortcut_grammar_or_targets(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    _write_test_shortcuts(tmp_path, {"unit": list(args)})

    with pytest.raises(InvalidTestShortcutError):
        load_project_config(tmp_path)


@pytest.mark.parametrize("selector", ("-k", "-m"))
def test_rejects_test_shortcut_selector_expression_with_embedded_nul(
    tmp_path: Path, selector: str
) -> None:
    _write_test_shortcuts(tmp_path, {"unit": [selector, "bad\x00expr"]})

    with pytest.raises(
        InvalidTestShortcutError,
        match=rf"Invalid Test Shortcut 'unit': selector {selector} expression cannot contain NUL",
    ):
        load_project_config(tmp_path)


def test_rejects_absolute_test_shortcut_target(tmp_path: Path) -> None:
    absolute_target = tmp_path / "test_absolute.py"
    absolute_target.write_text("", encoding="utf-8")
    _write_test_shortcuts(tmp_path, {"unit": [str(absolute_target)]})

    with pytest.raises(InvalidTestShortcutError):
        load_project_config(tmp_path)


def test_rejects_test_shortcut_symlink_to_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-test-shortcuts"
    outside.mkdir(exist_ok=True)
    (outside / "test_outside.py").write_text("", encoding="utf-8")
    link = tmp_path / "tests"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip()
    _write_test_shortcuts(tmp_path, {"unit": ["tests/test_outside.py"]})

    with pytest.raises(InvalidTestShortcutError):
        load_project_config(tmp_path)


@pytest.mark.parametrize("target", ("bad\x00path", "loop"))
def test_translates_unsafe_test_shortcut_path_inspection(
    tmp_path: Path, target: str
) -> None:
    if target == "loop":
        try:
            (tmp_path / "loop").symlink_to("loop")
        except OSError:
            pytest.skip()
    _write_test_shortcuts(tmp_path, {"unit": [target]})

    with pytest.raises(
        InvalidTestShortcutError,
        match="Invalid Test Shortcut 'unit': target path cannot be inspected safely",
    ):
        load_project_config(tmp_path)


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


def test_collects_existing_relative_and_absolute_positionals(tmp_path: Path) -> None:
    relative = tmp_path / "api.py"
    relative.write_text("", encoding="utf-8")
    absolute = tmp_path / "outside.py"
    absolute.write_text("", encoding="utf-8")

    result = collect_existing_positionals(
        tmp_path,
        ("api.py", str(absolute), "missing.py", "tests/test_x.py::test_name"),
    )

    assert result == frozenset(("api.py", str(absolute)))
