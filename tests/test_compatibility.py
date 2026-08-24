from pathlib import Path
import sys

import pytest

from pyrepo_check.cli import main, parse_args
from tests.support import RecordingRunner


def test_direct_pytest_node_id_is_forwarded_verbatim(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_example.py"
    test_file.parent.mkdir()
    test_file.write_text("", encoding="utf-8")
    runner = RecordingRunner()

    result = main(
        [
            "--root",
            str(tmp_path),
            "pytest",
            "tests/test_example.py::test_exact_behavior",
        ],
        runner=runner,
    )

    assert result == 0
    assert [call.command for call in runner.calls] == [
        (
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "tests/test_example.py::test_exact_behavior",
        )
    ]


@pytest.mark.parametrize(
    ("returncodes", "raise_on_call", "expected"),
    [
        ((-15, 7, 0, 0, 0), None, 7),
        ((-15, 0, 0, 0, 0), None, 2),
        ((0, 0, 0, 0, 0), 1, 2),
        ((0, 7, 0, 0, 0), 1, 7),
    ],
)
def test_legacy_exit_code_classifies_spawn_and_negative_outcomes(
    tmp_path: Path,
    returncodes: tuple[int, ...],
    raise_on_call: int | None,
    expected: int,
) -> None:
    (tmp_path / "src").mkdir()
    runner = RecordingRunner(returncodes=returncodes, raise_on_call=raise_on_call)

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == expected
    assert len(runner.calls) == 5


def test_spawn_exception_is_recorded_and_later_checks_continue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    error = FileNotFoundError("uv")
    stdout_at_spawn: list[str] = []
    runner = RecordingRunner(
        raise_on_call=2,
        exception=error,
        on_call=lambda _call: stdout_at_spawn.append(capsys.readouterr().out),
    )

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == 2
    assert len(runner.calls) == 5
    assert stdout_at_spawn == [
        "\n==> ruff: uv run python -m ruff check .\n",
        (
            "\n==> annotations: uv run python -m ruff check . "
            "--select ANN --output-format concise\n"
        ),
        "\n==> ty: uv run python -m ty check\n",
        "\n==> bandit: uv run python -m bandit -c pyproject.toml -r .\n",
        "\n==> pytest: uv run python -m pytest\n",
    ]
    assert capsys.readouterr().out == (
        "\n==> pyrepo-check summary: error (incomplete)\n"
        "    error: annotations: Could not start process: FileNotFoundError: uv\n"
        "    passed: ruff, ty, bandit, pytest\n"
    )


def test_runner_value_error_is_not_a_planning_error(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    error = ValueError("runner failed")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as captured:
        main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert captured.value is error


def test_banner_is_printed_before_each_spawn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    stdout_at_spawn: list[str] = []
    runner = RecordingRunner(
        on_call=lambda _call: stdout_at_spawn.append(capsys.readouterr().out)
    )

    result = main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert result == 0
    assert stdout_at_spawn == ["\n==> ruff: uv run python -m ruff check src\n"]


def test_help_surface_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["pyrepo-check"])

    with pytest.raises(SystemExit) as captured:
        parse_args(["--help"])

    output = capsys.readouterr()
    assert captured.value.code == 0
    assert output.out == """usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen]
                    [--format {terminal,json}] [--shortcut NAME]
                    [checks ...]

Run Python repository quality checks.

positional arguments:
  checks                Optional check names and target paths. Checks: ruff,
                        annotations, annotations-fix, ty, bandit, pytest.

options:
  -h, --help            show this help message and exit
  --all                 Run all checks.
  --root ROOT           Project root to check. Defaults to the current working
                        directory.
  --no-frozen           Run uv without --frozen even when uv.lock exists.
  --format {terminal,json}
                        Output terminal diagnostics or one JSON document.
  --shortcut NAME       Run a configured Test Shortcut in a pytest-only
                        focused run.
"""
    assert output.err == ""


def test_format_defaults_to_terminal() -> None:
    assert parse_args([]).format == "terminal"


def test_json_format_is_public_syntax_before_checks() -> None:
    args = parse_args(["--format", "json", "ty"])

    assert args.format == "json"
    assert args.checks == ["ty"]


@pytest.mark.parametrize(
    "argv",
    (
        ("--shortcut", "unit", "pytest"),
        ("pytest", "--shortcut", "unit"),
    ),
)
def test_shortcut_is_public_syntax_in_both_supported_placements(
    argv: tuple[str, ...],
) -> None:
    args = parse_args(argv)

    assert args.checks == ["pytest"]
    assert args.shortcut == "unit"


def test_missing_shortcut_operand_remains_argparse_owned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["--shortcut"])

    output = capsys.readouterr()
    assert captured.value.code == 2
    assert output.out == ""
    assert "argument --shortcut: expected one argument" in output.err


def test_invalid_format_remains_argparse_owned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["--format", "xml"])

    output = capsys.readouterr()
    assert captured.value.code == 2
    assert output.out == ""
    assert "invalid choice: 'xml'" in output.err
    assert "choose from terminal, json" in output.err
