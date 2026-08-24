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


def make_json_plan(tmp_path: Path) -> RunPlan:
    plan = make_plan(tmp_path)
    return RunPlan(
        mode=plan.mode,
        targets=plan.targets,
        checks=plan.checks,
        output_format="json",
    )


def make_single_check_plan(tmp_path: Path) -> RunPlan:
    plan = make_plan(tmp_path)
    return RunPlan(mode=plan.mode, targets=plan.targets, checks=plan.checks[:1])


def test_zero_return_codes_produce_exit_zero(tmp_path: Path) -> None:
    result = execute_plan(make_plan(tmp_path), runner=RecordingRunner(returncodes=(0, 0)))

    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 0
    assert tuple(check.returncode for check in result.checks) == (0, 0)


def test_terminal_commands_run_in_plan_order_with_exact_arguments(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(0, 0))

    execute_plan(make_plan(tmp_path), runner=runner)

    assert runner.calls == [
        runner.calls[0].__class__(
            command=("uv", "run", "python", "-m", "ruff", "check", "src"),
            cwd=tmp_path,
            check=False,
            capture_output=False,
        ),
        runner.calls[1].__class__(
            command=("uv", "run", "python", "-m", "ty", "check"),
            cwd=tmp_path,
            check=False,
            capture_output=False,
        ),
    ]


def test_terminal_banner_is_printed_before_spawn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []

    def on_call(_call: object) -> None:
        events.append(capsys.readouterr().out)

    execute_plan(make_plan(tmp_path), runner=RecordingRunner(on_call=on_call))

    assert events == [
        "\n==> ruff: uv run python -m ruff check src\n",
        "\n==> ty: uv run python -m ty check\n",
    ]


def test_json_commands_capture_output_without_printing_banner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = RecordingRunner(stdout=(b"first", "second"), stderr=(b"", b"error"))

    result = execute_plan(make_json_plan(tmp_path), runner=runner)

    assert [call.capture_output for call in runner.calls] == [True, True]
    assert capsys.readouterr().out == ""
    assert [(check.stdout, check.stderr) for check in result.checks] == [
        (b"first", b""),
        (b"second", b"error"),
    ]


def test_terminal_observations_do_not_claim_uncaptured_output(tmp_path: Path) -> None:
    result = execute_plan(
        make_plan(tmp_path),
        runner=RecordingRunner(stdout=(b"ignored",), stderr=(b"ignored",)),
    )

    assert [(check.stdout, check.stderr) for check in result.checks] == [
        (None, None),
        (None, None),
    ]


@pytest.mark.parametrize(
    ("elapsed_ns", "duration_ms"),
    [(499_999, 0), (500_000, 1), (1_500_000, 2)],
)
def test_duration_rounds_to_nearest_millisecond(
    tmp_path: Path,
    elapsed_ns: int,
    duration_ms: int,
) -> None:
    clock_values = iter((1_000_000_000, 1_000_000_000 + elapsed_ns))

    result = execute_plan(
        make_single_check_plan(tmp_path),
        runner=RecordingRunner(),
        clock_ns=lambda: next(clock_values),
    )

    assert result.checks[0].duration_ms == duration_ms


def test_ordinary_failures_continue_and_first_positive_nonzero_wins(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(3, 7))

    result = execute_plan(make_plan(tmp_path), runner=runner)

    assert result.exit_code == 3
    assert len(runner.calls) == 2


def test_negative_return_codes_are_recorded_and_later_checks_run(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(-9, 4))

    result = execute_plan(make_plan(tmp_path), runner=runner)

    assert tuple(check.returncode for check in result.checks) == (-9, 4)
    assert result.exit_code == 4
    assert len(runner.calls) == 2


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied")])
def test_spawn_errors_are_recorded_and_later_checks_run(
    tmp_path: Path,
    error: OSError,
) -> None:
    runner = RecordingRunner(raise_on_call=1, exception=error)
    clock_values = iter((0, 500_000, 1_000_000, 1_500_000))

    result = execute_plan(
        make_plan(tmp_path),
        runner=runner,
        clock_ns=lambda: next(clock_values),
    )

    assert result.checks[0].returncode is None
    assert result.checks[0].duration_ms == 1
    assert result.checks[0].spawn_error == f"{type(error).__name__}: {error}"
    assert result.checks[1].returncode == 0
    assert len(runner.calls) == 2


def test_injected_valueerror_is_propagated_by_identity(tmp_path: Path) -> None:
    error = ValueError("injected")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as raised:
        execute_plan(make_plan(tmp_path), runner=runner)

    assert raised.value is error
