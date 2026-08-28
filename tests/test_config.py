import json
import hashlib
import os
from pathlib import Path
import re
import subprocess  # nosec B404
import sys
from typing import Any, cast

import pytest

from pyrepo_check.config import (
    CoverageConfig,
    InvalidCoverageConfigError,
    InvalidTestShortcutError,
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
            f"{json.dumps(name)} = {json.dumps(args)}" for name, args in shortcuts.items()
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

    assert load_project_config(tmp_path).test_shortcuts == (ConfigTestShortcut(name, ("tests",)),)


@pytest.mark.parametrize(
    ("shortcut_toml", "message"),
    (
        ('test-shortcuts = "tests"', "[tool.pyrepo-check.test-shortcuts] must be a TOML table"),
        ('test-shortcuts = ["tests"]', "[tool.pyrepo-check.test-shortcuts] must be a TOML table"),
    ),
)
def test_rejects_non_table_test_shortcuts(tmp_path: Path, shortcut_toml: str, message: str) -> None:
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

    assert load_project_config(tmp_path).test_shortcuts == (ConfigTestShortcut("unit", args),)


@pytest.mark.parametrize(
    "target",
    (
        "tests/test_one.py",
        "tests",
        "tests/test_one.py::test_name",
        ".",
        "tests/test_one.py",
    ),
)
def test_accepts_existing_contained_test_targets(tmp_path: Path, target: str) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").write_text("", encoding="utf-8")
    _write_test_shortcuts(tmp_path, {"unit": [target]})

    assert load_project_config(tmp_path).test_shortcuts == (ConfigTestShortcut("unit", (target,)),)


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
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyrepo-check]
ruff_targets = ["."]
bandit_targets = ["."]

[tool.pyrepo-check.test-shortcuts]
unit = ["tests/test_outside.py"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(InvalidTestShortcutError):
        load_project_config(tmp_path)


@pytest.mark.parametrize("target", ("bad\x00path", "loop"))
def test_translates_unsafe_test_shortcut_path_inspection(tmp_path: Path, target: str) -> None:
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
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
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

    assert config.root == tmp_path
    assert config.ruff_targets == ("src/pkg", "tests", "scripts")
    assert config.bandit_targets == ("src/pkg",)
    assert config.pyproject_sha256 == hashlib.sha256(
        (tmp_path / "pyproject.toml").read_bytes()
    ).hexdigest()


def test_rejects_symlinked_pyproject_before_parsing(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-pyproject.toml"
    outside.write_text("[project]\nname='outside'\n", encoding="utf-8")
    try:
        (tmp_path / "pyproject.toml").symlink_to(outside)
    except OSError:
        pytest.skip()

    with pytest.raises(ValueError, match="pyproject.toml.*regular"):
        load_project_config(tmp_path)


def test_rejects_oversized_pyproject_before_toml_parsing(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes(b"#" * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="pyproject.toml.*1048576-byte limit"):
        load_project_config(tmp_path)


@pytest.mark.parametrize(
    "content",
    (
        b"\xff",
        b"[tool.pyrepo-check\n",
    ),
)
def test_rejects_invalid_pyproject_encoding_or_toml(
    tmp_path: Path,
    content: bytes,
) -> None:
    (tmp_path / "pyproject.toml").write_bytes(content)

    with pytest.raises(ValueError, match="pyproject.toml"):
        load_project_config(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_rejects_fifo_pyproject_without_blocking(tmp_path: Path) -> None:
    cast(Any, os).mkfifo(tmp_path / "pyproject.toml")
    completed = subprocess.run(  # nosec B603
        (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from pyrepo_check.config import load_project_config; "
            f"load_project_config(Path({str(tmp_path)!r}))",
        ),
        check=False,
        capture_output=True,
        timeout=2,
    )

    assert completed.returncode != 0
    assert b"pyproject.toml" in completed.stderr
    assert b"regular" in completed.stderr


def test_detects_default_targets_without_config(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()

    config = load_project_config(tmp_path)

    assert config.ruff_targets == ("src", "tests", "scripts")
    assert config.bandit_targets == ("src",)


def test_falls_back_to_current_directory_when_no_targets_exist(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)

    assert config.ruff_targets == (".",)
    assert config.bandit_targets == (".",)


def test_rejects_auto_detected_target_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-default-target"
    outside.mkdir()
    try:
        (tmp_path / "src").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip()

    with pytest.raises(ValueError, match="target must remain beneath the project root"):
        load_project_config(tmp_path)


def test_loading_configuration_does_not_depend_on_uv_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    config = load_project_config(tmp_path)

    assert config.root == tmp_path


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


@pytest.mark.parametrize(
    "target",
    (
        "--fix",
        "--exit-zero",
        "../outside.py",
        "src/../src/example.py",
        "bad\x00path",
        "missing.py",
    ),
)
def test_rejects_unsafe_or_missing_configured_targets(
    tmp_path: Path,
    target: str,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyrepo-check]\nruff_targets = " + json.dumps([target]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ruff_targets"):
        load_project_config(tmp_path)


def test_rejects_configured_absolute_and_symlink_escape_targets(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target.py"
    outside.write_text("", encoding="utf-8")
    try:
        (tmp_path / "escape.py").symlink_to(outside)
    except OSError:
        pytest.skip()

    for target in (str(outside), "escape.py"):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pyrepo-check]\nruff_targets = " + json.dumps([target]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="ruff_targets"):
            load_project_config(tmp_path)


def test_accepts_safe_configured_file_directory_and_dot_targets(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyrepo-check]\n"
        'ruff_targets = ["src", "src/example.py", "."]\n'
        'bandit_targets = ["src/example.py"]\n',
        encoding="utf-8",
    )

    config = load_project_config(tmp_path)

    assert config.ruff_targets == ("src", "src/example.py", ".")
    assert config.bandit_targets == ("src/example.py",)


def test_collects_only_existing_contained_relative_positionals(tmp_path: Path) -> None:
    relative = tmp_path / "api.py"
    relative.write_text("", encoding="utf-8")

    result = collect_existing_positionals(
        tmp_path,
        ("api.py", "missing.py", "tests/test_x.py::test_name"),
    )

    assert result == frozenset(("api.py",))


def _write_coverage_config(root: Path, coverage_toml: str) -> Path:
    pyproject_path = root / "pyproject.toml"
    pyproject_path.write_text(coverage_toml, encoding="utf-8")
    return pyproject_path


@pytest.mark.parametrize("pyproject", (None, "[tool.pytest.ini_options]"))
def test_loads_no_coverage_config_when_coverage_table_is_absent(
    tmp_path: Path, pyproject: str | None
) -> None:
    if pyproject is not None:
        _write_coverage_config(tmp_path, pyproject)

    assert load_project_config(tmp_path).coverage is None


@pytest.mark.parametrize(
    "coverage_toml",
    (
        "[tool.coverage.report]\nshow_missing = true",
        "[tool.coverage]\n",
    ),
)
def test_rejects_partial_or_unrelated_coverage_configuration(
    tmp_path: Path, coverage_toml: str
) -> None:
    _write_coverage_config(tmp_path, coverage_toml)

    with pytest.raises(InvalidCoverageConfigError, match="pyproject.toml"):
        load_project_config(tmp_path)


@pytest.mark.parametrize("source_key", ("source", "source_pkgs", "source_dirs"))
def test_loads_valid_coverage_source_family(tmp_path: Path, source_key: str) -> None:
    pyproject_path = _write_coverage_config(
        tmp_path,
        f'[tool.coverage.run]\nbranch = true\n{source_key} = ["src/package"]',
    )

    config = load_project_config(tmp_path)

    assert config.coverage == CoverageConfig(config_path=pyproject_path, fail_under=None)


def test_loads_coverage_config_with_multiple_source_families_and_threshold(
    tmp_path: Path,
) -> None:
    pyproject_path = _write_coverage_config(
        tmp_path,
        """
[tool.coverage.run]
branch = true
source = ["src/package"]
source_pkgs = ["package"]
source_dirs = ["src"]

[tool.coverage.report]
fail_under = 87.5
""".strip(),
    )

    assert load_project_config(tmp_path).coverage == CoverageConfig(
        config_path=pyproject_path,
        fail_under=87.5,
    )


@pytest.mark.parametrize(("fail_under", "expected"), (("87", 87), ("87.5", 87.5)))
def test_loads_finite_numeric_coverage_threshold(
    tmp_path: Path, fail_under: str, expected: int | float
) -> None:
    _write_coverage_config(
        tmp_path,
        "\n".join(
            (
                "[tool.coverage.run]",
                "branch = true",
                'source = ["src/package"]',
                "[tool.coverage.report]",
                f"fail_under = {fail_under}",
            )
        ),
    )

    coverage = load_project_config(tmp_path).coverage

    assert coverage is not None
    assert coverage.fail_under == expected


@pytest.mark.parametrize(
    "run_options",
    (
        "",
        "parallel = false",
        "concurrency = []",
        "patch = []",
        "parallel = false\nconcurrency = []\npatch = []",
    ),
)
def test_loads_inactive_coverage_parallelism_options(tmp_path: Path, run_options: str) -> None:
    _write_coverage_config(
        tmp_path,
        "\n".join(
            option
            for option in (
                "[tool.coverage.run]",
                "branch = true",
                'source = ["src/package"]',
                run_options,
            )
            if option
        ),
    )

    assert load_project_config(tmp_path).coverage is not None


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("parallel", '"false"'),
        ("parallel", "0"),
        ("parallel", "[]"),
        ("parallel", "{ enabled = false }"),
        ("concurrency", '"thread"'),
        ("concurrency", "0"),
        ("concurrency", "false"),
        ("concurrency", "true"),
        ("concurrency", "{ enabled = false }"),
        ("concurrency", '["thread"]'),
        ("concurrency", "[1]"),
        ("concurrency", '["thread", 1]'),
        ("patch", '"subprocess"'),
        ("patch", "0"),
        ("patch", "false"),
        ("patch", "true"),
        ("patch", "{ enabled = false }"),
        ("patch", '["subprocess"]'),
        ("patch", "[1]"),
        ("patch", '["subprocess", 1]'),
    ),
)
def test_rejects_invalid_coverage_parallelism_options(tmp_path: Path, key: str, value: str) -> None:
    _write_coverage_config(
        tmp_path,
        "\n".join(
            (
                "[tool.coverage.run]",
                "branch = true",
                'source = ["src/package"]',
                f"{key} = {value}",
            )
        ),
    )

    with pytest.raises(InvalidCoverageConfigError, match="pyproject.toml"):
        load_project_config(tmp_path)


@pytest.mark.parametrize(
    "coverage_toml",
    (
        '[tool.coverage.run]\nsource = ["src/package"]',
        '[tool.coverage.run]\nbranch = false\nsource = ["src/package"]',
        '[tool.coverage.run]\nbranch = "true"\nsource = ["src/package"]',
        "[tool.coverage.run]\nbranch = true",
        "[tool.coverage.run]\nbranch = true\nsource = []",
        '[tool.coverage.run]\nbranch = true\nsource = [""]',
        "[tool.coverage.run]\nbranch = true\nsource = [1]",
        '[tool.coverage.run]\nbranch = true\nsource = "src/package"',
        "[tool.coverage]\nrun = []",
        '[tool.coverage.run]\nbranch = true\nsource = []\nsource_pkgs = [" "]',
        "[tool.coverage.run]\nbranch = true\nsource_dirs = [1]",
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\nparallel = true',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\nconcurrency = ["thread"]',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\npatch = ["subprocess"]',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\n[tool.coverage.report]\nfail_under = true',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\n[tool.coverage.report]\nfail_under = "90"',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\n[tool.coverage.report]\nfail_under = nan',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\n[tool.coverage.report]\nfail_under = inf',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\n[tool.coverage.report]\nfail_under = -inf',
        '[tool.coverage.run]\nbranch = true\nsource = ["src/package"]\n[tool.coverage.report]\nfail_under = []',
        '[tool.coverage]\nreport = []\n[tool.coverage.run]\nbranch = true\nsource = ["src/package"]',
    ),
)
def test_rejects_invalid_coverage_configuration(tmp_path: Path, coverage_toml: str) -> None:
    _write_coverage_config(tmp_path, coverage_toml)

    with pytest.raises(InvalidCoverageConfigError, match="pyproject.toml"):
        load_project_config(tmp_path)
