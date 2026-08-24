from __future__ import annotations

from pathlib import Path

import pytest

from pyrepo_check.execution import ExecutionResult, execute_plan
from pyrepo_check.planning import PlannedCheck, RunPlan
from tests.support import RecordingRunner


def make_plan(tmp_path: Path) -> RunPlan:
    return RunPlan(
        mode="focused",
        targets=(),
        checks=(
            PlannedCheck(
                name="ruff",
                command=("uv", "run", "python", "-m", "ruff", "check", "src"),
                cwd=tmp_path,
            ),
            PlannedCheck(
                name="ty",
                command=("uv", "run", "python", "-m", "ty", "check"),
                cwd=tmp_path,
            ),
        ),
    )


def test_zero_return_codes_produce_exit_zero(tmp_path: Path) -> None:
    result = execute_plan(make_plan(tmp_path), runner=RecordingRunner(returncodes=(0, 0)))

    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 0
    assert tuple(check.returncode for check in result.checks) == (0, 0)


def test_commands_run_in_plan_order_with_exact_arguments(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(0, 0))

    execute_plan(make_plan(tmp_path), runner=runner)

    assert runner.calls == [
        runner.calls[0].__class__(
            command=("uv", "run", "python", "-m", "ruff", "check", "src"),
            cwd=tmp_path,
            check=False,
        ),
        runner.calls[1].__class__(
            command=("uv", "run", "python", "-m", "ty", "check"),
            cwd=tmp_path,
            check=False,
        ),
    ]


def test_each_banner_is_printed_before_spawn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events: list[str] = []

    def on_call(_call: object) -> None:
        events.append(capsys.readouterr().out)

    execute_plan(make_plan(tmp_path), runner=RecordingRunner(on_call=on_call))

    assert events == [
        "\n==> ruff: uv run python -m ruff check src\n",
        "\n==> ty: uv run python -m ty check\n",
    ]


def test_ordinary_failures_continue_and_first_positive_nonzero_wins(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(3, 7))

    result = execute_plan(make_plan(tmp_path), runner=runner)

    assert result.exit_code == 3
    assert len(runner.calls) == 2


def test_first_negative_nonzero_remains_final_exit_code(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(-9, 4))

    result = execute_plan(make_plan(tmp_path), runner=runner)

    assert result.exit_code == -9
    assert len(runner.calls) == 2


def test_filenotfounderror_is_propagated_and_later_checks_do_not_run(tmp_path: Path) -> None:
    error = FileNotFoundError("missing")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(FileNotFoundError) as raised:
        execute_plan(make_plan(tmp_path), runner=runner)

    assert raised.value is error
    assert len(runner.calls) == 1


def test_injected_valueerror_is_propagated_by_identity(tmp_path: Path) -> None:
    error = ValueError("injected")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as raised:
        execute_plan(make_plan(tmp_path), runner=runner)

    assert raised.value is error
