from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pyrepo_check.cli import main, parse_args
from pyrepo_check.execution import (
    AnalysisPythonAuthorityObservation,
    CapturedBytes,
    CheckStartObservation,
    DependencyObservation,
    ExecutedProcess,
    ProcessRunner,
    PythonObservation,
    RepositoryCheckObservation,
    RepositoryEnvironmentObservation,
    RepositoryExecutionResult,
    ToolEnvironmentObservation,
    TerminalWriter,
)
from pyrepo_check.planning import RunPlan
from tests.support import RecordingRunner


@pytest.fixture(autouse=True)
def _uv_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n",
        encoding="utf-8",
    )


def _tool_environment() -> ToolEnvironmentObservation:
    return ToolEnvironmentObservation(
        pyrepo_check_version="0.1.0",
        python=PythonObservation("cpython", (3, 13, 15), Path("/tool/bin/python")),
    )


def _process(role: str, command: tuple[str, ...], cwd: Path) -> ExecutedProcess:
    return ExecutedProcess(
        role=role,
        command=command,
        cwd=cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )


def _successful_ty_execution(plan: RunPlan) -> RepositoryExecutionResult:
    invocation = plan.checks[0]
    repository_python = PythonObservation(
        "cpython",
        (3, 12, 11),
        plan.root / ".venv/bin/python",
    )
    digest = hashlib.sha256()
    for argument in invocation.arguments:
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    primary = _process(
        "primary",
        (
            "uv",
            "run",
            "--locked",
            "--python",
            str(repository_python.executable),
            "python",
            str(plan.root / ".pyrepo-check/check-launcher.py"),
            "--evidence",
            str(plan.root / ".pyrepo-check/start.json"),
            "--check",
            "ty",
            "--module",
            "ty",
            "--",
            *invocation.arguments,
        ),
        plan.root,
    )
    dependency_process = _process(
        "dependency_probe",
        ("uv", "run", "--locked", "python", "-c", "dependency-probe"),
        plan.root,
    )
    return RepositoryExecutionResult(
        tool_environment=_tool_environment(),
        repository_environment=RepositoryEnvironmentObservation(
            manager_version="0.10.12",
            path=plan.root / ".venv",
            python_selection=plan.repository_python,
            python=repository_python,
            lock_path=plan.root / "uv.lock",
            lock_status="current",
            mutation_protection="protected_files",
            dependencies=(
                DependencyObservation(
                    name="ty",
                    module="ty",
                    required=">=0.0.35,<0.1",
                    status="available",
                    version="0.0.35",
                    origin=str(plan.root / ".venv/lib/python3.12/site-packages/ty/__init__.py"),
                    process=dependency_process,
                    error=None,
                ),
            ),
            processes=(
                _process(
                    "repository_git_root",
                    ("git", "-C", str(plan.root), "rev-parse", "--show-toplevel"),
                    plan.root,
                ),
                _process("uv_version", ("uv", "--version"), plan.root),
                _process(
                    "environment_probe",
                    ("uv", "run", "--locked", "python", "-c", "environment-probe"),
                    plan.root,
                ),
            ),
            error=None,
        ),
        checks=(
            RepositoryCheckObservation(
                invocation=invocation,
                execution_environment="repository",
                analysis_python_authority=AnalysisPythonAuthorityObservation(),
                start=CheckStartObservation(
                    schema_version=1,
                    check="ty",
                    module="ty",
                    arguments_sha256=digest.hexdigest(),
                    python=repository_python,
                ),
                processes=(primary,),
                error=None,
            ),
        ),
    )


def test_python_request_is_accepted_before_or_after_check_tokens() -> None:
    assert parse_args(("--python", "3.12", "ty")).python == "3.12"
    assert parse_args(("ty", "--python", "3.12")).checks == ["ty"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    (
        (("--all",), (True, None, "terminal", [])),
        (("--python", "3.12", "--all"), (True, "3.12", "terminal", [])),
        (
            ("--python", "3.12", "annotations", "ty", "src/"),
            (False, "3.12", "terminal", ["annotations", "ty", "src/"]),
        ),
        (
            ("--python", "3.12", "--format", "json", "--all"),
            (True, "3.12", "json", []),
        ),
    ),
)
def test_canonical_repository_environment_commands_are_public_cli_syntax(
    argv: tuple[str, ...],
    expected: tuple[bool, str | None, str, list[str]],
) -> None:
    arguments = parse_args(argv)

    assert (
        arguments.all,
        arguments.python,
        arguments.format,
        arguments.checks,
    ) == expected


def test_schema_v2_planning_error_observes_tool_environment_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    observations: list[ToolEnvironmentObservation] = []

    def observe() -> ToolEnvironmentObservation:
        observation = _tool_environment()
        observations.append(observation)
        return observation

    monkeypatch.setattr("pyrepo_check.cli.observe_tool_environment", observe)

    assert main(("--root", str(tmp_path), "--format", "json", "mypy")) == 2
    planning_payload = json.loads(capsysbinary.readouterr().out)
    assert planning_payload["schema_version"] == 2
    assert planning_payload["tool_environment"]["python"]["version"] == [3, 13, 15]
    assert planning_payload["repository_environment"] is None
    assert len(observations) == 1


def test_schema_v2_focused_ty_uses_injected_repository_environment_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    tool_environment = _tool_environment()
    observed: list[ToolEnvironmentObservation] = []

    def observe() -> ToolEnvironmentObservation:
        observed.append(tool_environment)
        return tool_environment

    def execute(
        plan: RunPlan,
        *,
        tool_environment: ToolEnvironmentObservation | None = None,
        runner: ProcessRunner | None = None,
        terminal_writer: TerminalWriter | None = None,
    ) -> RepositoryExecutionResult:
        del runner
        assert terminal_writer is None
        assert tool_environment is observed[0]
        return _successful_ty_execution(plan)

    monkeypatch.setattr("pyrepo_check.cli.observe_tool_environment", observe)
    monkeypatch.setattr("pyrepo_check.cli.execute_plan", execute)

    assert main(("--root", str(tmp_path), "--format", "json", "ty")) == 0
    run_payload = json.loads(capsysbinary.readouterr().out)
    assert run_payload["schema_version"] == 2
    assert run_payload["repository_environment"]["lock"]["status"] == "current"
    assert run_payload["checks"][0]["execution_environment"] == "repository"
    assert len(observed) == 1


def test_missing_lock_is_schema_v2_environment_error_without_spawning(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    runner = RecordingRunner()

    assert main(("--root", str(tmp_path), "--format", "json", "ty"), runner=runner) == 2
    payload = json.loads(capsysbinary.readouterr().out)
    assert runner.calls == []
    assert payload["repository_environment"]["lock"]["status"] == "missing"
    assert payload["repository_environment"]["error"]["code"] == "repository_lock_missing"
    assert payload["checks"][0]["error"]["code"] == "repository_environment_unavailable"


@pytest.mark.parametrize("argv", (("--no-frozen", "ty"), ("--python", "3.14", "ty")))
def test_environment_planning_errors_spawn_nothing(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    runner = RecordingRunner()

    assert main(("--root", str(tmp_path), *argv), runner=runner) == 2
    assert runner.calls == []


def test_invalid_option_like_direct_target_is_typed_and_spawns_nothing(
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    runner = RecordingRunner()

    assert main(
        ("--root", str(tmp_path), "--format", "json", "--", "--exit-zero"),
        runner=runner,
    ) == 2

    payload = json.loads(capsysbinary.readouterr().out)
    assert runner.calls == []
    assert payload["kind"] == "planning_error"
    assert payload["error"]["code"] == "unknown_target"


def test_terminal_planning_error_is_written_to_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(("--root", str(tmp_path), "mypy")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Unknown check(s): mypy\n")


def test_reporting_failure_emits_no_partial_json_and_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    monkeypatch.setattr(
        "pyrepo_check.cli.execute_plan",
        lambda plan, **_kwargs: _successful_ty_execution(plan),
    )
    monkeypatch.setattr(
        "pyrepo_check.cli.build_run_report",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("report broken")),
    )

    assert main(("--root", str(tmp_path), "--format", "json", "ty")) == 2
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"internal reporting error: report broken" in captured.err


@pytest.mark.parametrize("failure", ("write", "flush"))
def test_cli_terminal_output_failure_finishes_execution_then_returns_fallback_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    execution_finished = False
    verification_finished = False
    progress = (
        "==> environment: tool Python 3.13.15 -> "
        "repository Python 3.12.11 (uv, locked)\n"
    )

    def execute(
        plan: RunPlan,
        *,
        tool_environment: ToolEnvironmentObservation | None = None,
        runner: ProcessRunner | None = None,
        terminal_writer: TerminalWriter | None = None,
    ) -> RepositoryExecutionResult:
        nonlocal execution_finished, verification_finished
        del tool_environment, runner
        assert terminal_writer is not None
        terminal_writer(progress)
        terminal_writer("\n==> ty: python -m ty check\n")
        execution_finished = True
        verification_finished = True
        return _successful_ty_execution(plan)

    class FailingStdout:
        def __init__(self) -> None:
            self.writes: list[str] = []
            self.write_calls = 0
            self.flush_calls = 0

        def write(self, text: str) -> int:
            self.write_calls += 1
            if failure == "write":
                raise OSError("stdout write failed")
            self.writes.append(text)
            return len(text)

        def flush(self) -> None:
            self.flush_calls += 1
            if failure == "flush":
                raise OSError("stdout flush failed")

    stdout = FailingStdout()
    monkeypatch.setattr("pyrepo_check.cli.execute_plan", execute)
    monkeypatch.setattr("pyrepo_check.cli.sys.stdout", stdout)
    monkeypatch.setattr(
        "pyrepo_check.cli.build_run_report",
        lambda *_args: (_ for _ in ()).throw(AssertionError("reporting must not run")),
    )

    assert main(("--root", str(tmp_path), "ty")) == 2

    assert execution_finished and verification_finished
    assert stdout.write_calls == 1
    assert stdout.writes == ([] if failure == "write" else [progress])
    assert "==> pyrepo-check summary:" not in "".join(stdout.writes)
    assert "".join(stdout.writes).count("==> environment:") <= 1
    assert "internal reporting error:" in capsys.readouterr().err
