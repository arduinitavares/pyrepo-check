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


def test_first_negative_nonzero_is_returned_and_later_checks_run(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    runner = RecordingRunner(returncodes=(-15, 7, 0, 0, 0))

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == -15
    assert len(runner.calls) == 5


def test_spawn_exception_is_propagated_and_aborts(
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

    with pytest.raises(FileNotFoundError) as captured:
        main(["--root", str(tmp_path), "--all"], runner=runner)

    assert captured.value is error
    assert len(runner.calls) == 2
    assert stdout_at_spawn == [
        "\n==> ruff: uv run python -m ruff check .\n",
        (
            "\n==> annotations: uv run python -m ruff check . "
            "--select ANN --output-format concise\n"
        ),
    ]


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
    assert output.out == """usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen] [checks ...]

Run Python repository quality checks.

positional arguments:
  checks       Optional check names and target paths. Checks: ruff,
               annotations, annotations-fix, ty, bandit, pytest.

options:
  -h, --help   show this help message and exit
  --all        Run all checks.
  --root ROOT  Project root to check. Defaults to the current working
               directory.
  --no-frozen  Run uv without --frozen even when uv.lock exists.
"""
    assert output.err == ""
