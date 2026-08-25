from pathlib import Path
import json

import pytest

from pyrepo_check.cli import main
from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import ExecutedCheck, ExecutedProcess, ExecutionResult, ProcessRunner
from pyrepo_check.planning import (
    PlannedCheck,
    PlanningFacts,
    PlanningFailure,
    RunPlan,
    RunRequest,
)
from tests.support import RecordingRunner


def _write_test_shortcuts(root: Path, shortcuts: dict[str, object]) -> None:
    shortcut_toml = "\n".join(
        f"{json.dumps(name)} = {json.dumps(args)}"
        for name, args in shortcuts.items()
    )
    (root / "pyproject.toml").write_text(
        "[tool.pyrepo-check.test-shortcuts]\n" + shortcut_toml,
        encoding="utf-8",
    )


def _assert_planning_error_output(
    stdout: bytes,
    stderr: bytes,
    *,
    output_format: str,
    code: str,
    message: str,
    hint: str | None,
) -> None:
    if output_format == "json":
        assert stderr == b""
        assert stdout.endswith(b"\n")
        assert json.loads(stdout.decode("utf-8")) == {
            "schema_version": 1,
            "kind": "planning_error",
            "overall_status": "error",
            "complete": False,
            "error": {"code": code, "message": message, "hint": hint},
        }
    else:
        assert stdout == b""
        assert stderr == (
            message + (f"\nHint: {hint}" if hint is not None else "") + "\n"
        ).encode()


def executed_check(
    planned: PlannedCheck,
    returncode: int | None,
    *,
    duration_ms: int = 1,
    stdout: bytes | None = None,
    stderr: bytes | None = None,
    spawn_error: str | None = None,
) -> ExecutedCheck:
    return ExecutedCheck(
        planned=planned,
        processes=(
            ExecutedProcess(
                role="primary",
                command=planned.command,
                cwd=planned.cwd,
                returncode=returncode,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                spawn_error=spawn_error,
            ),
        ),
    )


def test_cli_builds_request_and_executes_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned_check = PlannedCheck(
        name="ty",
        command=("uv", "run", "python", "-m", "ty", "check"),
        cwd=tmp_path.resolve(),
    )
    expected_plan = RunPlan(mode="focused", targets=(), checks=(planned_check,))
    planned_requests: list[RunRequest] = []
    executed_plans: list[RunPlan] = []
    injected_runner = RecordingRunner()

    def fake_plan_run(
        request: RunRequest,
        config: ProjectConfig,
        facts: PlanningFacts,
    ) -> RunPlan:
        planned_requests.append(request)
        assert config.root == tmp_path.resolve()
        assert config.frozen is False
        assert facts == PlanningFacts(existing_positionals=frozenset())
        return expected_plan

    def fake_execute_plan(
        plan: RunPlan,
        *,
        runner: ProcessRunner,
    ) -> ExecutionResult:
        executed_plans.append(plan)
        assert runner is injected_runner
        return ExecutionResult(
            checks=(
                executed_check(planned_check, 7),
            ),
            exit_code=7,
        )

    monkeypatch.setattr("pyrepo_check.cli.plan_run", fake_plan_run, raising=False)
    monkeypatch.setattr(
        "pyrepo_check.cli.execute_plan",
        fake_execute_plan,
        raising=False,
    )

    result = main(
        ["--root", str(tmp_path), "--no-frozen", "--shortcut", "unit", "ty"],
        runner=injected_runner,
    )

    assert result == 7
    assert planned_requests == [
        RunRequest(
            root=tmp_path,
            positionals=("ty",),
            all_selected=False,
            no_frozen=True,
            test_shortcut="unit",
        )
    ]
    assert executed_plans == [expected_plan]


@pytest.mark.parametrize("output_format", ("terminal", "json"))
@pytest.mark.parametrize(
    ("positionals", "all_selected"),
    (
        (("pytest", "tests/direct.py"), False),
        (("ruff", "pytest"), False),
        (("pytest",), True),
        ((), False),
    ),
)
def test_shortcut_conflicts_render_planning_errors_without_spawning(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
    positionals: tuple[str, ...],
    all_selected: bool,
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "direct.py").write_text("", encoding="utf-8")
    _write_test_shortcuts(tmp_path, {"unit": ["tests/unit"]})
    runner = RecordingRunner()
    argv = ["--root", str(tmp_path)]
    if output_format == "json":
        argv.extend(("--format", "json"))
    if all_selected:
        argv.append("--all")
    argv.extend(positionals)
    argv.extend(("--shortcut", "unit"))

    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    _assert_planning_error_output(
        captured.out,
        captured.err,
        output_format=output_format,
        code="invalid_arguments",
        message=(
            "--shortcut requires an explicit pytest-only run with no direct targets or --all."
        ),
        hint="Use: pyrepo-check pytest --shortcut NAME",
    )
    assert result == 2
    assert runner.calls == []


@pytest.mark.parametrize("output_format", ("terminal", "json"))
@pytest.mark.parametrize(
    ("shortcuts", "message"),
    (
        (
            {"unit": ["bad\x00path"]},
            "Invalid Test Shortcut 'unit': target path cannot be inspected safely: bad\x00path",
        ),
        ({"broken": []}, "Invalid Test Shortcut 'broken': value must be a non-empty list of strings"),
    ),
)
def test_invalid_shortcut_config_renders_typed_planning_error_without_spawning(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
    shortcuts: dict[str, object],
    message: str,
) -> None:
    _write_test_shortcuts(tmp_path, shortcuts)
    runner = RecordingRunner()
    argv = ["--root", str(tmp_path)]
    if output_format == "json":
        argv.extend(("--format", "json"))
    argv.append("ty")

    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    _assert_planning_error_output(
        captured.out,
        captured.err,
        output_format=output_format,
        code="invalid_test_shortcut",
        message=message,
        hint="Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.",
    )
    assert result == 2
    assert runner.calls == []


@pytest.mark.parametrize("output_format", ("terminal", "json"))
@pytest.mark.parametrize("selector", ("-k", "-m"))
def test_nul_selector_config_renders_typed_planning_error_without_spawning(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
    selector: str,
) -> None:
    _write_test_shortcuts(tmp_path, {"unit": [selector, "bad\x00expr"]})
    runner = RecordingRunner()
    argv = ["--root", str(tmp_path)]
    if output_format == "json":
        argv.extend(("--format", "json"))
    argv.append("ty")

    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    _assert_planning_error_output(
        captured.out,
        captured.err,
        output_format=output_format,
        code="invalid_test_shortcut",
        message=(
            f"Invalid Test Shortcut 'unit': selector {selector} expression cannot contain NUL"
        ),
        hint="Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.",
    )
    assert result == 2
    assert runner.calls == []


@pytest.mark.parametrize("output_format", ("terminal", "json"))
def test_symlink_loop_shortcut_config_renders_typed_error_without_spawning(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
) -> None:
    try:
        (tmp_path / "loop").symlink_to("loop")
    except OSError:
        pytest.skip()
    _write_test_shortcuts(tmp_path, {"unit": ["loop"]})
    runner = RecordingRunner()
    argv = ["--root", str(tmp_path)]
    if output_format == "json":
        argv.extend(("--format", "json"))
    argv.append("ty")

    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    _assert_planning_error_output(
        captured.out,
        captured.err,
        output_format=output_format,
        code="invalid_test_shortcut",
        message="Invalid Test Shortcut 'unit': target path cannot be inspected safely: loop",
        hint="Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.",
    )
    assert result == 2
    assert runner.calls == []


@pytest.mark.parametrize("output_format", ("terminal", "json"))
@pytest.mark.parametrize(
    ("shortcuts", "expected_hint"),
    (
        (
            {
                "unit": ["tests/unit"],
                "cli": ["tests/unit"],
                "integration": ["tests/unit"],
            },
            "Available Test Shortcuts: cli, integration, unit",
        ),
        ({}, "No Test Shortcuts are configured."),
    ),
)
def test_unknown_shortcut_renders_name_hint_without_spawning(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
    shortcuts: dict[str, object],
    expected_hint: str,
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    _write_test_shortcuts(tmp_path, shortcuts)
    runner = RecordingRunner()
    argv = ["--root", str(tmp_path)]
    if output_format == "json":
        argv.extend(("--format", "json"))
    argv.extend(("pytest", "--shortcut", "missing"))

    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    _assert_planning_error_output(
        captured.out,
        captured.err,
        output_format=output_format,
        code="unknown_test_shortcut",
        message="Unknown Test Shortcut: missing",
        hint=expected_hint,
    )
    assert result == 2
    assert runner.calls == []


@pytest.mark.parametrize("output_format", ("terminal", "json"))
def test_cli_executes_named_shortcut_with_authoritative_selection_metadata(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    _write_test_shortcuts(tmp_path, {"unit": ["tests/unit", "-m", "not slow"]})
    runner = RecordingRunner()
    argv = ["--root", str(tmp_path)]
    if output_format == "json":
        argv.extend(("--format", "json"))
    argv.extend(("pytest", "--shortcut", "unit"))

    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    assert result == 0
    assert [call.command for call in runner.calls] == [
        ("uv", "run", "python", "-c", runner.calls[0].command[-1]),
        (
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "-p",
            "pyrepo_check_pytest_evidence_plugin",
            "tests/unit",
            "-m",
            "not slow",
        )
    ]
    if output_format == "json":
        assert captured.err == b""
        assert captured.out.endswith(b"\n")
        payload = json.loads(captured.out.decode("utf-8"))
        assert payload["selection"] == {
            "checks": ["pytest"],
            "targets": [],
            "test_shortcut": "unit",
            "pytest_args": ["tests/unit", "-m", "not slow"],
            "planned_test_scope": "partial",
            "planned_coverage_scope": "not_requested",
        }
    else:
        assert captured.err == b""
        assert captured.out.endswith(
            b"\n==> pyrepo-check summary: passed (complete)\n    passed: pytest\n"
        )


def test_terminal_summary_is_written_only_after_final_runner_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    stdout_at_spawn: list[str] = []
    runner = RecordingRunner(
        returncodes=(0, 7),
        on_call=lambda _call: stdout_at_spawn.append(capsys.readouterr().out),
    )

    result = main(["--root", str(tmp_path), "ruff", "ty"], runner=runner)

    assert result == 7
    assert stdout_at_spawn == [
        "\n==> ruff: uv run python -m ruff check src\n",
        "\n==> ty: uv run python -m ty check\n",
    ]
    assert capsys.readouterr().out == (
        "\n==> pyrepo-check summary: failed (complete)\n"
        "    failed: ty (exit 7)\n"
        "    passed: ruff\n"
    )


@pytest.mark.parametrize(
    ("returncode", "expected_exit", "expected_summary"),
    (
        (0, 0, "passed (complete)\n    passed: ruff"),
        (4, 4, "failed (complete)\n    failed: ruff (exit 4)"),
        (
            -15,
            2,
            "error (incomplete)\n"
            "    error: ruff: Primary process terminated by signal 15.",
        ),
    ),
)
def test_terminal_summary_and_exit_follow_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
    expected_exit: int,
    expected_summary: str,
) -> None:
    (tmp_path / "src").mkdir()

    result = main(
        ["--root", str(tmp_path), "ruff"],
        runner=RecordingRunner(returncodes=(returncode,)),
    )

    output = capsys.readouterr()
    assert result == expected_exit
    assert output.out.endswith(
        f"\n==> pyrepo-check summary: {expected_summary}\n"
    )
    assert output.err == ""


def test_cli_reports_planning_error_without_spawning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = RecordingRunner()

    result = main(["--root", str(tmp_path), "mypy"], runner=runner)

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == (
        "Unknown check(s): mypy\n"
        "Hint: Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest\n"
    )
    assert runner.calls == []


def test_json_is_one_isolated_utf8_document_with_captured_process_streams(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    (tmp_path / "src").mkdir()
    runner = RecordingRunner(
        returncodes=(-15, 7),
        stdout=(
            b'{"fragment":true}\n\x1b[31mred\x1b[0m\nUTF-8: \xe2\x98\x83',
            b"later {stdout}\n",
        ),
        stderr=(
            b"warn\n\x1b]0;title\x07visible",
            b"}\n",
        ),
    )

    result = main(
        ["--root", str(tmp_path), "--format", "json", "ruff", "ty"],
        runner=runner,
    )

    captured = capsysbinary.readouterr()
    payload = json.loads(captured.out.decode("utf-8"))
    assert result == 7
    assert captured.out.endswith(b"\n")
    assert captured.err == b""
    assert b"==>" not in captured.out
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert [check["name"] for check in payload["checks"]] == ["ruff", "ty"]
    assert payload["checks"][0]["processes"][0]["stdout"]["text"] == (
        '{"fragment":true}\nred\nUTF-8: \u2603'
    )
    assert payload["checks"][0]["processes"][0]["stderr"]["text"] == (
        "warn\nvisible"
    )
    assert payload["checks"][1]["processes"][0]["stdout"]["text"] == (
        "later {stdout}\n"
    )
    assert payload["checks"][1]["processes"][0]["stderr"]["text"] == "}\n"


def test_json_planning_error_is_one_document_and_spawns_nothing(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    runner = RecordingRunner()

    result = main(
        ["--root", str(tmp_path), "--format", "json", "mypy"],
        runner=runner,
    )

    captured = capsysbinary.readouterr()
    payload = json.loads(captured.out.decode("utf-8"))
    assert result == 2
    assert captured.out.endswith(b"\n")
    assert captured.err == b""
    assert set(payload) == {
        "schema_version",
        "kind",
        "overall_status",
        "complete",
        "error",
    }
    assert payload == {
        "schema_version": 1,
        "kind": "planning_error",
        "overall_status": "error",
        "complete": False,
        "error": {
            "code": "unknown_check",
            "message": "Unknown check(s): mypy",
            "hint": (
                "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest"
            ),
        },
    }
    assert runner.calls == []


@pytest.mark.parametrize("output_format", ["terminal", "json"])
@pytest.mark.parametrize(
    ("dependency", "error", "expected_code", "expected_hint"),
    (
        ("load_project_config", ValueError("bad config"), "invalid_project_config", None),
        (
            "load_project_config",
            RuntimeError("config bug"),
            "internal_planning_error",
            None,
        ),
        (
            "collect_existing_positionals",
            RuntimeError("facts bug"),
            "internal_planning_error",
            None,
        ),
        (
            "plan_run",
            PlanningFailure(
                "unknown_target",
                "Unknown check(s): missing.py",
                hint="Check the target path or select a check name.",
            ),
            "unknown_target",
            "Check the target path or select a check name.",
        ),
        (
            "plan_run",
            RuntimeError("planner bug"),
            "internal_planning_error",
            None,
        ),
        (
            "plan_run",
            ValueError("planner value bug"),
            "internal_planning_error",
            None,
        ),
    ),
)
def test_planning_exception_boundary_builds_report_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
    output_format: str,
    dependency: str,
    error: Exception,
    expected_code: str,
    expected_hint: str | None,
) -> None:
    runner = RecordingRunner()

    def raise_error(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error

    monkeypatch.setattr(f"pyrepo_check.cli.{dependency}", raise_error)

    argv = ["--root", str(tmp_path), "ruff"]
    if output_format == "json":
        argv[2:2] = ["--format", "json"]
    result = main(argv, runner=runner)

    captured = capsysbinary.readouterr()
    assert result == 2
    assert runner.calls == []
    if output_format == "json":
        assert captured.err == b""
        payload = json.loads(captured.out.decode("utf-8"))
        assert payload["error"]["code"] == expected_code
        assert payload["error"]["message"] == str(error)
        assert payload["error"]["hint"] == expected_hint
    else:
        assert captured.out == b""
        assert captured.err == (
            str(error)
            + (f"\nHint: {expected_hint}" if expected_hint is not None else "")
            + "\n"
        ).encode()


@pytest.mark.parametrize(
    ("dependency", "error"),
    (
        ("load_project_config", KeyboardInterrupt()),
        ("collect_existing_positionals", SystemExit(19)),
        ("plan_run", KeyboardInterrupt()),
    ),
)
def test_planning_baseexception_propagates_by_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
    error: BaseException,
) -> None:
    runner = RecordingRunner()

    def raise_error(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error

    monkeypatch.setattr(f"pyrepo_check.cli.{dependency}", raise_error)

    with pytest.raises(type(error)) as raised:
        main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert raised.value is error
    assert runner.calls == []


def test_json_build_failure_writes_no_partial_document_and_preserves_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    (tmp_path / "src").mkdir()

    def fail_build(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("build failed")

    monkeypatch.setattr("pyrepo_check.cli.build_run_report", fail_build, raising=False)

    result = main(
        ["--root", str(tmp_path), "--format", "json", "ruff"],
        runner=RecordingRunner(returncodes=(7,)),
    )

    captured = capsysbinary.readouterr()
    assert result == 7
    assert captured.out == b""
    assert captured.err == b"pyrepo-check: internal reporting error: build failed\n"


def test_json_validation_failure_writes_no_partial_document_and_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    (tmp_path / "src").mkdir()

    def fail_validation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("validation failed")

    monkeypatch.setattr(
        "pyrepo_check.cli.validate_report_v1",
        fail_validation,
        raising=False,
    )

    result = main(
        ["--root", str(tmp_path), "--format", "json", "ruff"],
        runner=RecordingRunner(),
    )

    captured = capsysbinary.readouterr()
    assert result == 2
    assert captured.out == b""
    assert captured.err == (
        b"pyrepo-check: internal reporting error: validation failed\n"
    )


def test_json_encoding_failure_writes_no_partial_document_and_preserves_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    (tmp_path / "src").mkdir()

    def fail_encoding(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("encoding failed")

    monkeypatch.setattr("pyrepo_check.reporting.json.dumps", fail_encoding)

    result = main(
        ["--root", str(tmp_path), "--format", "json", "ruff"],
        runner=RecordingRunner(returncodes=(9,)),
    )

    captured = capsysbinary.readouterr()
    assert result == 9
    assert captured.out == b""
    assert captured.err == b"pyrepo-check: internal reporting error: encoding failed\n"


@pytest.mark.parametrize(
    ("malformation", "expected_message"),
    (
        ("extra", "unexpected execution observation for check bandit"),
        ("duplicate", "duplicate execution observation for check ruff"),
        (
            "mismatched",
            "mismatched or out-of-order observation for check ruff",
        ),
        (
            "out_of_order",
            "mismatched or out-of-order observation for check ruff",
        ),
    ),
)
def test_json_malformed_execution_cardinality_uses_reporting_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
    malformation: str,
    expected_message: str,
) -> None:
    (tmp_path / "src").mkdir()

    def observed(check: PlannedCheck, returncode: int) -> ExecutedCheck:
        return executed_check(check, returncode, stdout=b"", stderr=b"")

    def malformed_execution(
        plan: RunPlan,
        *,
        runner: ProcessRunner,
    ) -> ExecutionResult:
        del runner
        ruff, ty = plan.checks
        if malformation == "extra":
            bandit = PlannedCheck(
                name="bandit",
                command=("uv", "run", "python", "-m", "bandit"),
                cwd=ruff.cwd,
            )
            checks = (observed(ruff, 7), observed(ty, 0), observed(bandit, 0))
        elif malformation == "duplicate":
            checks = (observed(ruff, 7), observed(ruff, 0))
        elif malformation == "mismatched":
            mismatched = PlannedCheck(
                name="ruff",
                command=(*ruff.command, "different"),
                cwd=ruff.cwd,
            )
            checks = (observed(mismatched, 7),)
        else:
            checks = (observed(ty, 7), observed(ruff, 0))
        return ExecutionResult(checks=checks, exit_code=99)

    monkeypatch.setattr("pyrepo_check.cli.execute_plan", malformed_execution)

    result = main(
        ["--root", str(tmp_path), "--format", "json", "ruff", "ty"],
        runner=RecordingRunner(),
    )

    captured = capsysbinary.readouterr()
    assert result == 7
    assert captured.out == b""
    assert captured.err == (
        f"pyrepo-check: internal reporting error: {expected_message}\n".encode()
    )


def test_json_missing_execution_observation_is_a_schema_valid_error_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    (tmp_path / "src").mkdir()

    def incomplete_execution(
        plan: RunPlan,
        *,
        runner: ProcessRunner,
    ) -> ExecutionResult:
        del runner
        ty = plan.checks[1]
        observation = executed_check(ty, 0, stdout=b"", stderr=b"")
        return ExecutionResult(checks=(observation,), exit_code=0)

    monkeypatch.setattr("pyrepo_check.cli.execute_plan", incomplete_execution)

    result = main(
        ["--root", str(tmp_path), "--format", "json", "ruff", "ty"],
        runner=RecordingRunner(),
    )

    captured = capsysbinary.readouterr()
    payload = json.loads(captured.out.decode("utf-8"))
    assert result == 2
    assert captured.err == b""
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["checks"][0]["name"] == "ruff"
    assert payload["checks"][0]["status"] == "error"
    assert payload["checks"][0]["error"] == {
        "code": "missing_primary_process",
        "message": "No primary process observation was recorded.",
    }


def test_cli_propagates_runner_value_error_by_identity(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    error = ValueError("runner failed")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as raised:
        main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert raised.value is error
