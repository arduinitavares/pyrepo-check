from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess  # nosec B404

import pytest

from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import (
    DependencyObservation,
    EnvironmentFailureObservation,
    RepositoryEnvironmentObservation,
    RepositoryPreparation,
    ToolEnvironmentObservation,
    PythonObservation,
    PreparedRepositoryEnvironment,
)
from pyrepo_check import execution_workspace, repository_executor
from pyrepo_check.planning import CoverageExecutionPlan, RunPlan, build_checks
from pyrepo_check.repository_executor import SafeRepositoryPreparation, prepare_safe_repository
from pyrepo_check.repository_safety import (
    RepositoryStateSnapshot,
    RepositoryVerificationResult,
)
import tests.support as test_support
from tests.support import (
    RecordingRunner,
    available_dependency,
    environment_probe_bytes,
    focused_plan,
    launcher_aware_runner,
    missing_dependency,
    monotonic_clock,
    prepared_repository,
)


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
    )


def _write_locked_project(root: Path) -> Path:
    resolved = root.resolve()
    (resolved / "src").mkdir(parents=True)
    (resolved / ".venv/bin").mkdir(parents=True)
    (resolved / ".venv/bin/python").write_bytes(b"")
    (resolved / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (resolved / "src/example.py").write_text("value = 1\n", encoding="utf-8")
    (resolved / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\nrequires-python='>=3.10'\n",
        encoding="utf-8",
    )
    (resolved / "uv.lock").write_text(
        "version = 1\nrevision = 3\n",
        encoding="utf-8",
    )
    _run_git(resolved, "init", "-q")
    _run_git(resolved, "add", ".")
    _run_git(
        resolved,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return resolved


def _successful_runner(root: Path) -> RecordingRunner:
    stage = _run_git(root, "ls-files", "--stage", "-z", "--", ".").stdout
    environment_root = root / ".venv"
    return RecordingRunner(
        stdout=(
            str(root).encode() + b"\n",
            b"",
            b"",
            stage,
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 13, 15),
                executable=environment_root / "bin/python",
                environment_root=environment_root,
            ),
            _dependency_payload(
                "ruff",
                version="0.15.1",
                origin=environment_root / "lib/python3.13/site-packages/ruff/__init__.py",
            ),
        )
    )


def test_missing_lock_returns_canonical_failure_before_git_or_uv(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    runner = RecordingRunner()

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "repository_lock_missing"
    assert observation.lock_status == "missing"
    assert observation.processes == ()
    assert len(observation.dependencies) == 1
    dependency = observation.dependencies[0]
    assert (
        dependency.name,
        dependency.module,
        dependency.required,
        dependency.status,
        dependency.version,
        dependency.origin,
        dependency.process,
        dependency.error,
    ) == ("ruff", "ruff", ">=0.15,<1", "unobserved", None, None, None, None)
    assert runner.calls == []


def test_unsafe_lock_returns_canonical_failure_before_git_or_uv(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    target = root / "lock-target"
    target.write_text("version = 1\n", encoding="utf-8")
    (root / "uv.lock").symlink_to(target)
    runner = RecordingRunner()

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "unsafe_repository_environment"
    assert observation.processes == ()
    assert runner.calls == []


def test_unsafe_venv_stops_before_git_or_uv(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\nrevision = 3\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (root / ".venv").symlink_to(outside, target_is_directory=True)
    runner = RecordingRunner()

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "unsafe_repository_environment"
    assert observation.processes == ()
    assert runner.calls == []
    assert list(outside.iterdir()) == []


def test_failed_preparation_retains_baseline_process_order(tmp_path: Path) -> None:
    root = _write_locked_project(tmp_path)
    stage = _run_git(root, "ls-files", "--stage", "-z", "--", ".").stdout
    runner = RecordingRunner(
        returncodes=(0, 0, 0, 0, 1),
        stdout=(str(root).encode() + b"\n", b"", b"", stage, b""),
    )

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is not None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "uv_unavailable"
    assert tuple(process.role for process in observation.processes) == (
        "repository_git_root",
        "repository_venv_tracked",
        "repository_venv_ignored",
        "repository_tracked_snapshot",
        "uv_version",
    )
    assert all("ruff" not in call.command for call in runner.calls)


def test_safe_preparation_runs_dependency_probe_before_any_check(tmp_path: Path) -> None:
    root = _write_locked_project(tmp_path)
    runner = _successful_runner(root)

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is not None
    assert result.preparation.prepared is not None
    observation = result.preparation.observation
    assert observation.error is None
    assert tuple(process.role for process in observation.processes) == (
        "repository_git_root",
        "repository_venv_tracked",
        "repository_venv_ignored",
        "repository_tracked_snapshot",
        "uv_version",
        "environment_probe",
    )
    assert observation.processes[3].command[-5:] == (
        "ls-files",
        "--stage",
        "-z",
        "--",
        ".",
    )
    assert observation.processes[4].command == ("uv", "--version")
    assert observation.mutation_protection == "unobserved"
    assert tuple(dependency.name for dependency in observation.dependencies) == ("ruff",)
    assert observation.dependencies[0].status == "available"
    assert observation.dependencies[0].process is not None
    assert observation.dependencies[0].process.role == "dependency_probe"
    assert all("-m" not in call.command for call in runner.calls)
    assert len(runner.calls) == 7


def test_dependency_probes_continue_in_first_required_order_after_errors(
    tmp_path: Path,
) -> None:
    root = _write_locked_project(tmp_path)
    stage = _run_git(root, "ls-files", "--stage", "-z", "--", ".").stdout
    environment_root = root / ".venv"
    plan = focused_plan(root, "ruff")
    checks = build_checks(
        ProjectConfig(root=root, ruff_targets=(), bandit_targets=())
    )
    pytest_check = checks["pytest"]
    assert pytest_check.pytest is not None
    plan = replace(
        plan,
        checks=(
            checks["ruff"],
            checks["annotations"],
            checks["ty"],
            checks["bandit"],
            replace(
                pytest_check,
                pytest=replace(
                    pytest_check.pytest,
                    coverage=CoverageExecutionPlan(root / "pyproject.toml", 80),
                ),
            ),
        ),
        planned_coverage_scope="complete",
    )
    runner = RecordingRunner(
        stdout=(
            str(root).encode() + b"\n",
            b"",
            b"",
            stage,
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 13, 15),
                executable=environment_root / "bin/python",
                environment_root=environment_root,
            ),
            _dependency_payload("ruff", status="missing", diagnostic="missing"),
            _dependency_payload("ty", version="0.0.35", origin=environment_root / "ty.py"),
            _dependency_payload(
                "bandit", status="unusable", version="1.9.2", diagnostic="broken"
            ),
            _dependency_payload(
                "pytest", version="8.4.2", origin=environment_root / "pytest.py"
            ),
            _dependency_payload(
                "coverage", status="missing", diagnostic="missing"
            ),
        )
    )

    result = prepare_safe_repository(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.preparation.prepared is not None
    dependencies = result.preparation.observation.dependencies
    assert tuple(dependency.name for dependency in dependencies) == (
        "ruff",
        "ty",
        "bandit",
        "pytest",
        "coverage",
    )
    assert tuple(dependency.status for dependency in dependencies) == (
        "missing",
        "available",
        "unusable",
        "available",
        "missing",
    )
    dependency_calls = [
        call for call in runner.calls if "dependency_probe" not in call.command
        and "-c" in call.command
        and call.command[-3] in {"ruff", "ty", "bandit", "pytest", "coverage"}
    ]
    assert tuple(call.command[-3] for call in dependency_calls) == (
        "ruff",
        "ty",
        "bandit",
        "pytest",
        "coverage",
    )
    assert len(runner.calls) == 11
    assert all("-m" not in call.command for call in runner.calls)


def _dependency_payload(
    name: str,
    *,
    status: str = "available",
    version: str | None = None,
    origin: Path | None = None,
    diagnostic: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "distribution": name,
            "module": name,
            "status": status,
            "version": version,
            "origin": None if origin is None else str(origin),
            "diagnostic": diagnostic,
        },
        separators=(",", ":"),
    ).encode()


def _internal_plan(root: Path, *names: str) -> RunPlan:
    checks = build_checks(ProjectConfig(root=root, ruff_targets=(), bandit_targets=()))
    plan = focused_plan(root, "ruff")
    return replace(plan, checks=tuple(checks[name] for name in names))


def _safe_preparation(
    plan: RunPlan,
    *,
    dependencies: tuple[DependencyObservation, ...],
    baseline: RepositoryStateSnapshot | None = None,
    prepared_environment: PreparedRepositoryEnvironment | None = None,
) -> SafeRepositoryPreparation:
    selected = prepared_environment or prepared_repository(plan.root, (3, 12, 11))
    observation = RepositoryEnvironmentObservation(
        manager_version="0.10.12",
        path=selected.path,
        python_selection=plan.repository_python,
        python=selected.python,
        lock_path=plan.root / "uv.lock",
        lock_status="current",
        mutation_protection="unobserved",
        dependencies=dependencies,
        processes=(),
        error=None,
    )
    return SafeRepositoryPreparation(
        baseline,
        RepositoryPreparation(selected, observation),
    )


def test_execute_repository_plan_keeps_dependency_errors_local_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ruff", "ty", "bandit", "pytest")
    safe = _safe_preparation(
        plan,
        dependencies=(
            missing_dependency("ruff"),
            available_dependency("ty", "0.0.35"),
            available_dependency("bandit", "1.9.2"),
            available_dependency("pytest", "8.4.2"),
        ),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    runner = launcher_aware_runner(
        publish_valid_marker=True,
        returncodes=(0, 1, 0),
    )

    result = repository_executor.execute_repository_plan(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert tuple(check.invocation.name for check in result.checks) == (
        "ruff",
        "ty",
        "bandit",
        "pytest",
    )
    assert result.checks[0].processes == ()
    assert result.checks[0].error == missing_dependency("ruff").error
    assert all(check.start is not None for check in result.checks[1:])
    primary_calls = [call for call in runner.calls if "--evidence" in call.command]
    assert tuple(call.command[call.command.index("--module") + 1] for call in primary_calls) == (
        "ty",
        "bandit",
        "pytest",
    )


def test_missing_pytest_blocks_only_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ty", "pytest")
    safe = _safe_preparation(
        plan,
        dependencies=(
            available_dependency("ty", "0.0.35"),
            missing_dependency("pytest"),
        ),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    runner = launcher_aware_runner(returncode=0, publish_valid_marker=True)

    result = repository_executor.execute_repository_plan(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    ty_check, pytest_check = result.checks
    assert ty_check.invocation.name == "ty"
    assert ty_check.start is not None
    assert pytest_check.invocation.name == "pytest"
    assert pytest_check.error == missing_dependency("pytest").error
    assert pytest_check.processes == ()
    primary_calls = [call for call in runner.calls if "--evidence" in call.command]
    assert len(primary_calls) == 1
    assert primary_calls[0].command[primary_calls[0].command.index("--module") + 1] == "ty"


def test_missing_coverage_is_retained_but_plain_pytest_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "pytest")
    invocation = plan.checks[0]
    assert invocation.pytest is not None
    plan = replace(
        plan,
        checks=(
            replace(
                invocation,
                pytest=replace(
                    invocation.pytest,
                    coverage=CoverageExecutionPlan(
                        config_path=tmp_path / "pyproject.toml",
                        fail_under=None,
                    ),
                ),
            ),
        ),
        planned_coverage_scope="complete",
    )
    safe = _safe_preparation(
        plan,
        dependencies=(
            available_dependency("pytest", "8.4.2"),
            missing_dependency("coverage"),
        ),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    marker_runner = launcher_aware_runner(returncode=0, publish_valid_marker=True)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        completed = marker_runner(
            command,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            env=env,
        )
        if "--module" in command:
            module_index = command.index("--module")
            separator_index = command.index("--", module_index + 2)
            logical = (
                "python",
                "-m",
                command[module_index + 1],
                *command[separator_index + 1 :],
            )
            if "-p" in logical:
                test_support._publish_pytest_artifact(  # noqa: SLF001
                    logical,
                    env,
                    completed.returncode,
                )
        return completed

    result = repository_executor.execute_repository_plan(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert tuple(
        (dependency.name, dependency.status)
        for dependency in result.repository_environment.dependencies
    ) == (("pytest", "available"), ("coverage", "missing"))
    check = result.checks[0]
    assert check.invocation.name == "pytest"
    assert check.start is not None
    assert check.error is None
    assert check.pytest is not None
    assert check.pytest.artifact.state == "snapshot"
    assert check.coverage is not None
    assert check.coverage.preflight.classification == "module_unavailable"
    assert check.coverage.artifact.state == "not_attempted"
    primary_calls = [call for call in marker_runner.calls if "--evidence" in call.command]
    assert len(primary_calls) == 1
    command = primary_calls[0].command
    assert command[command.index("--module") + 1] == "pytest"
    assert "coverage" not in command


def test_missing_coverage_does_not_suppress_a_later_independent_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "pytest", "ty")
    pytest_invocation, ty_invocation = plan.checks
    assert pytest_invocation.pytest is not None
    pytest_invocation = replace(
        pytest_invocation,
        pytest=replace(
            pytest_invocation.pytest,
            coverage=CoverageExecutionPlan(
                config_path=tmp_path / "pyproject.toml",
                fail_under=None,
            ),
        ),
    )
    plan = replace(
        plan,
        checks=(pytest_invocation, ty_invocation),
        planned_coverage_scope="complete",
    )
    safe = _safe_preparation(
        plan,
        dependencies=(
            available_dependency("pytest", "8.4.2"),
            available_dependency("ty", "0.0.35"),
            missing_dependency("coverage"),
        ),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    runner = launcher_aware_runner(returncode=0, publish_valid_marker=True)

    result = repository_executor.execute_repository_plan(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    pytest_check, ty_check = result.checks
    assert pytest_check.pytest is not None
    assert pytest_check.coverage is not None
    assert pytest_check.coverage.preflight.classification == "module_unavailable"
    assert ty_check.invocation.name == "ty"
    assert ty_check.start is not None
    assert ty_check.error is None
    primaries = [call for call in runner.calls if "--evidence" in call.command]
    assert [call.command[call.command.index("--module") + 1] for call in primaries] == [
        "pytest",
        "ty",
    ]


@pytest.mark.parametrize("returncode", (2, 120))
def test_prepared_pytest_launcher_failure_does_not_suppress_later_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "pytest", "ty")
    pytest_invocation, ty_invocation = plan.checks
    assert pytest_invocation.pytest is not None
    pytest_invocation = replace(
        pytest_invocation,
        pytest=replace(
            pytest_invocation.pytest,
            coverage=CoverageExecutionPlan(
                config_path=tmp_path / "pyproject.toml",
                fail_under=None,
            ),
        ),
    )
    plan = replace(
        plan,
        checks=(pytest_invocation, ty_invocation),
        planned_coverage_scope="complete",
    )
    safe = _safe_preparation(
        plan,
        dependencies=(
            available_dependency("pytest", "8.4.2"),
            available_dependency("ty", "0.0.35"),
            available_dependency("coverage", "7.15.2"),
        ),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    marker_runner = launcher_aware_runner(
        returncodes=(returncode, 0),
        publish_valid_marker=True,
    )

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        completed = marker_runner(
            command,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            env=env,
        )
        if "--module" in command:
            module_index = command.index("--module")
            separator_index = command.index("--", module_index + 2)
            logical = (
                "python",
                "-m",
                command[module_index + 1],
                *command[separator_index + 1 :],
            )
            if "pytest" in logical:
                test_support._publish_pytest_artifact(  # noqa: SLF001
                    logical,
                    env,
                    completed.returncode,
                )
                test_support._publish_coverage_artifact(logical)  # noqa: SLF001
        return completed

    result = repository_executor.execute_repository_plan(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    pytest_check, ty_check = result.checks
    assert pytest_check.start is not None
    assert pytest_check.error is not None
    assert pytest_check.error.code == "check_execution_failed"
    assert [process.role for process in pytest_check.processes] == ["primary"]
    assert pytest_check.coverage is not None
    assert pytest_check.coverage.artifact.state == "data_missing"
    assert ty_check.start is not None
    assert ty_check.error is None
    assert len(marker_runner.calls) == 2


def test_execute_repository_plan_attaches_workspace_setup_failure_to_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ty")
    safe = _safe_preparation(
        plan,
        dependencies=(available_dependency("ty", "0.0.35"),),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    monkeypatch.setattr(
        execution_workspace,
        "create_run_workspace",
        lambda root: (_ for _ in ()).throw(OSError("setup blocked")),
    )

    result = repository_executor.execute_repository_plan(
        plan,
        runner=RecordingRunner(),
        clock_ns=monotonic_clock(),
    )

    check = result.checks[0]
    assert check.error is not None
    assert check.error.code == "cleanup_failed"
    assert check.processes == ()


@pytest.mark.parametrize("failure_point", ("verified-open", "staging"))
def test_verified_open_or_staging_failure_attaches_to_owning_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ty")
    safe = _safe_preparation(
        plan,
        dependencies=(available_dependency("ty", "0.0.35"),),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    if failure_point == "verified-open":
        monkeypatch.setattr(
            execution_workspace,
            "open_verified_workspace",
            lambda workspace: (_ for _ in ()).throw(OSError("verified open blocked")),
        )
    else:
        monkeypatch.setattr(
            repository_executor,
            "stage_check_launcher",
            lambda workspace: (_ for _ in ()).throw(OSError("staging blocked")),
        )

    result = repository_executor.execute_repository_plan(
        plan,
        runner=RecordingRunner(),
        clock_ns=monotonic_clock(),
    )

    check = result.checks[0]
    assert check.invocation.name == "ty"
    assert check.error is not None
    assert check.error.code == "cleanup_failed"
    assert failure_point.replace("-", " ") in check.error.message
    assert check.processes == ()


def test_cleanup_failure_after_missing_marker_retains_failed_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ty")
    safe = _safe_preparation(
        plan,
        dependencies=(available_dependency("ty", "0.0.35"),),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    created: list[execution_workspace.RunWorkspace] = []
    original_create = execution_workspace.create_run_workspace
    original_remove = execution_workspace.remove_run_workspace

    def record_create(root: Path) -> execution_workspace.RunWorkspace:
        workspace = original_create(root)
        created.append(workspace)
        return workspace

    monkeypatch.setattr(execution_workspace, "create_run_workspace", record_create)
    monkeypatch.setattr(
        execution_workspace,
        "remove_run_workspace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup blocked")),
    )
    try:
        result = repository_executor.execute_repository_plan(
            plan,
            runner=RecordingRunner(returncodes=(2,)),
            clock_ns=monotonic_clock(),
        )
    finally:
        if created:
            observation = original_remove(created[0], repository_root=plan.root)
            assert observation is None

    check = result.checks[0]
    assert check.error is not None
    assert check.error.code == "cleanup_failed"
    assert check.start is None
    assert check.execution_environment is None
    assert check.analysis_python_authority is None
    assert len(check.processes) == 1
    assert check.processes[0].returncode == 2


def test_cleanup_failure_retains_real_primary_start_and_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ty")
    safe = _safe_preparation(
        plan,
        dependencies=(available_dependency("ty", "0.0.35"),),
    )
    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    created: list[execution_workspace.RunWorkspace] = []
    original_create = execution_workspace.create_run_workspace
    original_remove = execution_workspace.remove_run_workspace

    def record_create(root: Path) -> execution_workspace.RunWorkspace:
        workspace = original_create(root)
        created.append(workspace)
        return workspace

    monkeypatch.setattr(execution_workspace, "create_run_workspace", record_create)
    monkeypatch.setattr(
        execution_workspace,
        "remove_run_workspace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup blocked")),
    )
    try:
        result = repository_executor.execute_repository_plan(
            plan,
            runner=launcher_aware_runner(returncode=1, publish_valid_marker=True),
            clock_ns=monotonic_clock(),
        )
    finally:
        if created:
            observation = original_remove(created[0], repository_root=plan.root)
            assert observation is None

    check = result.checks[0]
    assert check.error is not None
    assert check.error.code == "cleanup_failed"
    assert check.start is not None
    assert check.processes[0].returncode == 1
    assert check.execution_environment == "repository"
    assert check.analysis_python_authority is not None


def test_final_repository_verification_runs_whenever_baseline_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _internal_plan(tmp_path.resolve(), "ruff")
    baseline = RepositoryStateSnapshot(None, (), ())
    unavailable = RepositoryEnvironmentObservation(
        manager_version=None,
        path=None,
        python_selection=plan.repository_python,
        python=None,
        lock_path=plan.root / "uv.lock",
        lock_status="unverified",
        mutation_protection="unobserved",
        dependencies=(missing_dependency("ruff"),),
        processes=(),
        error=EnvironmentFailureObservation(
            "repository_environment_failed",
            "preparation failed",
            None,
        ),
    )
    safe = SafeRepositoryPreparation(
        baseline,
        RepositoryPreparation(None, unavailable),
    )
    called = False

    def verify(*args: object, **kwargs: object) -> RepositoryVerificationResult:
        nonlocal called
        called = True
        return RepositoryVerificationResult((), "protected_files", None)

    monkeypatch.setattr(repository_executor, "prepare_safe_repository", lambda *args, **kwargs: safe)
    monkeypatch.setattr(repository_executor, "verify_repository_state", verify)
    tool = ToolEnvironmentObservation(
        "test",
        PythonObservation("cpython", (3, 13, 15), Path("/tool/python")),
    )

    result = repository_executor.execute_repository_plan(
        plan,
        tool_environment=tool,
        runner=RecordingRunner(),
        clock_ns=monotonic_clock(),
    )

    assert called
    assert result.tool_environment is tool
    assert result.repository_environment.mutation_protection == "protected_files"
    assert result.checks[0].error is not None
    assert result.checks[0].error.code == "repository_environment_unavailable"
