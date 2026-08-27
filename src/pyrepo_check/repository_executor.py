"""Compose repository safety with locked environment preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import secrets
import time

from pyrepo_check.check_launcher import (
    CHECK_MODULE,
    StagedCheckLauncher,
    build_launcher_command,
    ensure_staged_launcher,
    ensure_start_marker_absent,
    stage_check_launcher,
    validate_start_marker,
)
from pyrepo_check.execution import (
    AnalysisPythonAuthorityObservation,
    CheckExecutionFailure,
    CheckExecutionErrorCode,
    DependencyObservation,
    ExecutedProcess,
    PreparedRepositoryEnvironment,
    RepositoryCheckObservation,
    RepositoryExecutionResult,
    RepositoryEnvironmentObservation,
    RepositoryPreparation,
    ProcessRunner,
    TerminalWriter,
    ToolEnvironmentObservation,
    execute_process,
    format_terminal_check_banner,
    format_terminal_environment_line,
    observe_tool_environment,
)
from pyrepo_check import execution_workspace
from pyrepo_check.execution_workspace import VerifiedRunWorkspace
from pyrepo_check.planning import CheckInvocation, RunPlan
from pyrepo_check.pytest_execution import execute_prepared_pytest
from pyrepo_check.repository_environment import (
    DependencyName,
    inspect_repository_lock,
    prepare_repository_environment,
    probe_repository_dependencies,
    unobserved_repository_dependencies,
)
from pyrepo_check.repository_safety import (
    RepositoryStateSnapshot,
    capture_repository_baseline,
    verify_repository_state,
)


@dataclass(frozen=True)
class SafeRepositoryPreparation:
    baseline: RepositoryStateSnapshot | None
    preparation: RepositoryPreparation


def prepare_safe_repository(
    plan: RunPlan,
    *,
    runner: ProcessRunner | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> SafeRepositoryPreparation:
    """Inspect the lock, capture safety state, then prepare without a Check."""
    lock_presence = inspect_repository_lock(plan.root)
    if lock_presence.state != "present":
        preparation = prepare_repository_environment(
            plan,
            lock_presence=lock_presence,
            runner=runner,
            clock_ns=clock_ns,
        )
        return SafeRepositoryPreparation(
            baseline=None,
            preparation=replace(
                preparation,
                observation=replace(
                    preparation.observation,
                    dependencies=unobserved_repository_dependencies(plan),
                ),
            ),
        )

    baseline = capture_repository_baseline(
        plan.root,
        runner=runner,
        clock_ns=clock_ns,
    )
    if baseline.error is not None:
        observation = RepositoryEnvironmentObservation(
            manager_version=None,
            path=None,
            python_selection=plan.repository_python,
            python=None,
            lock_path=lock_presence.path,
            lock_status="unverified",
            mutation_protection="unobserved",
            dependencies=unobserved_repository_dependencies(plan),
            processes=baseline.processes,
            error=baseline.error,
        )
        return SafeRepositoryPreparation(
            baseline=None,
            preparation=RepositoryPreparation(None, observation),
        )

    if baseline.snapshot is None:
        raise RuntimeError("successful repository baseline has no snapshot")
    preparation = prepare_repository_environment(
        plan,
        lock_presence=lock_presence,
        runner=runner,
        clock_ns=clock_ns,
    )
    if preparation.prepared is not None:
        dependencies = probe_repository_dependencies(
            plan,
            preparation.prepared,
            runner=runner,
            clock_ns=clock_ns,
        )
        preparation = replace(
            preparation,
            observation=replace(
                preparation.observation,
                dependencies=dependencies,
            ),
        )
    else:
        preparation = replace(
            preparation,
            observation=replace(
                preparation.observation,
                dependencies=unobserved_repository_dependencies(plan),
            ),
        )
    observation = preparation.observation
    combined = replace(
        preparation,
        observation=replace(
            observation,
            processes=baseline.processes + observation.processes,
        ),
    )
    return SafeRepositoryPreparation(baseline.snapshot, combined)


def execute_invocation(
    invocation: CheckInvocation,
    *,
    prepared: PreparedRepositoryEnvironment,
    workspace: VerifiedRunWorkspace,
    launcher: StagedCheckLauncher,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    capture_output: bool = True,
    terminal_writer: TerminalWriter | None = None,
) -> RepositoryCheckObservation:
    """Execute one real ordinary Check through trusted Repository Python dispatch."""
    marker_path = workspace.workspace.path / f"check-start-{secrets.token_hex(16)}.json"
    try:
        ensure_staged_launcher(launcher, workspace=workspace)
        ensure_start_marker_absent(marker_path, workspace=workspace)
    except OSError as error:
        return _check_failure(
            invocation,
            "check_start_evidence_invalid",
            f"Check start evidence could not be prepared: {error}",
        )
    module = CHECK_MODULE[invocation.name]
    if terminal_writer is not None:
        terminal_writer(
            format_terminal_check_banner(invocation.name, module, invocation.arguments)
        )
    process = execute_process(
        role="primary",
        command=build_launcher_command(
            prepared,
            launcher,
            invocation,
            marker_path,
            use_observed_python_executable=True,
        ),
        cwd=prepared.root,
        capture_output=capture_output,
        runner=runner,
        clock_ns=clock_ns,
        environment=prepared.child_environment,
    )
    start = None
    marker_error: OSError | None = None
    try:
        start = validate_start_marker(
            marker_path,
            workspace=workspace,
            invocation=invocation,
            module=module,
            prepared=prepared,
        )
    except OSError as error:
        marker_error = error

    if process.spawn_error is not None:
        return RepositoryCheckObservation(
            invocation,
            None,
            None,
            None,
            (process,),
            CheckExecutionFailure(
                "spawn_failed",
                f"Check process could not be spawned: {process.spawn_error}",
                None,
            ),
        )

    if start is None:
        error = CheckExecutionFailure(
            "check_start_evidence_invalid",
            f"Check start evidence is invalid: {marker_error}",
            "Retry after verifying the locked Repository Environment.",
        )
        return RepositoryCheckObservation(
            invocation,
            None,
            None,
            None,
            (process,),
            error,
        )

    error = _classify_primary_error(process)
    authority = (
        AnalysisPythonAuthorityObservation()
        if invocation.name in {"ruff", "annotations", "annotations-fix", "ty"}
        and process.spawn_error is None
        and process.returncode in {0, 1}
        else None
    )
    return RepositoryCheckObservation(
        invocation,
        "repository",
        authority,
        start,
        (process,),
        error,
    )


def execute_repository_plan(
    plan: RunPlan,
    *,
    tool_environment: ToolEnvironmentObservation | None = None,
    runner: ProcessRunner | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    terminal_writer: TerminalWriter | None = None,
) -> RepositoryExecutionResult:
    """Compose preparation, dependencies, per-Check workspaces, and verification."""
    observed_tool_environment = tool_environment or observe_tool_environment()
    progress_writer = terminal_writer if plan.output_format == "terminal" else None
    safe = prepare_safe_repository(plan, runner=runner, clock_ns=clock_ns)
    preparation = safe.preparation
    checks: list[RepositoryCheckObservation] = []
    if preparation.prepared is None:
        checks.extend(
            _check_failure(
                invocation,
                "repository_environment_unavailable",
                "Repository Environment is unavailable for this Check.",
            )
            for invocation in plan.checks
        )
    else:
        if progress_writer is not None:
            progress_writer(
                format_terminal_environment_line(
                    observed_tool_environment.python.version,
                    preparation.prepared.python.version,
                )
            )
        dependencies = {
            dependency.name: dependency for dependency in preparation.observation.dependencies
        }
        for invocation in plan.checks:
            dependency = dependencies[_dependency_name(invocation)]
            if invocation.name == "pytest":
                checks.append(
                    _execute_in_workspace(
                        invocation,
                        plan=plan,
                        prepared=preparation.prepared,
                        pytest_dependency=dependency,
                        coverage_dependency=dependencies.get("coverage"),
                        runner=runner,
                        clock_ns=clock_ns,
                        terminal_writer=progress_writer,
                    )
                )
                continue
            if dependency.status != "available":
                checks.append(
                    RepositoryCheckObservation(
                        invocation=invocation,
                        execution_environment=None,
                        analysis_python_authority=None,
                        start=None,
                        processes=(),
                        error=dependency.error
                        or CheckExecutionFailure(
                            "check_dependency_unusable",
                            f"Repository dependency {dependency.name} is unavailable.",
                            None,
                        ),
                    )
                )
                continue
            checks.append(
                _execute_in_workspace(
                    invocation,
                    plan=plan,
                    prepared=preparation.prepared,
                    pytest_dependency=None,
                    coverage_dependency=None,
                    runner=runner,
                    clock_ns=clock_ns,
                    terminal_writer=progress_writer,
                )
            )

    repository_observation = preparation.observation
    if safe.baseline is not None:
        verification = verify_repository_state(
            safe.baseline,
            annotations_fix_selected=any(
                invocation.name == "annotations-fix" for invocation in plan.checks
            ),
            runner=runner,
            clock_ns=clock_ns,
        )
        repository_observation = replace(
            repository_observation,
            processes=repository_observation.processes + verification.processes,
            mutation_protection=verification.mutation_protection,
            error=verification.error or repository_observation.error,
        )
    return RepositoryExecutionResult(
        tool_environment=observed_tool_environment,
        repository_environment=repository_observation,
        checks=tuple(checks),
    )


def _execute_in_workspace(
    invocation: CheckInvocation,
    *,
    plan: RunPlan,
    prepared: PreparedRepositoryEnvironment,
    pytest_dependency: DependencyObservation | None,
    coverage_dependency: DependencyObservation | None,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    terminal_writer: TerminalWriter | None,
) -> RepositoryCheckObservation:
    try:
        run_workspace = execution_workspace.create_run_workspace(prepared.root)
    except OSError as error:
        return _check_failure(
            invocation,
            "cleanup_failed",
            f"workspace creation failed: {type(error).__name__}: {error}",
        )

    verified: VerifiedRunWorkspace | None = None
    observation: RepositoryCheckObservation | None = None
    diagnostics: list[str] = []
    try:
        verified = execution_workspace.open_verified_workspace(run_workspace)
        launcher = stage_check_launcher(verified)
        if invocation.pytest is None:
            observation = execute_invocation(
                invocation,
                prepared=prepared,
                workspace=verified,
                launcher=launcher,
                runner=runner,
                clock_ns=clock_ns,
                capture_output=plan.output_format == "json",
                terminal_writer=terminal_writer,
            )
        else:
            if pytest_dependency is None:
                raise AssertionError("pytest dependency observation is unavailable")
            prepared_pytest = execute_prepared_pytest(
                invocation,
                plan=plan,
                prepared=prepared,
                pytest_dependency=pytest_dependency,
                coverage_dependency=coverage_dependency,
                workspace=verified,
                launcher=launcher,
                output_format=plan.output_format,
                runner=runner,
                clock_ns=clock_ns,
                terminal_writer=terminal_writer,
            )
            observation = RepositoryCheckObservation(
                invocation=invocation,
                execution_environment=(
                    "repository" if prepared_pytest.start is not None else None
                ),
                analysis_python_authority=None,
                start=prepared_pytest.start,
                processes=prepared_pytest.processes,
                error=prepared_pytest.error,
                pytest=prepared_pytest.pytest,
                coverage=prepared_pytest.coverage,
            )
    except OSError as error:
        diagnostics.append(f"workspace setup failed: {type(error).__name__}: {error}")
    finally:
        if verified is not None:
            try:
                verified.close()
            except OSError as error:
                diagnostics.append(
                    f"workspace descriptor close failed: {type(error).__name__}: {error}"
                )
        try:
            cleanup = execution_workspace.remove_run_workspace(
                run_workspace,
                repository_root=prepared.root,
                clock_ns=clock_ns,
            )
        except OSError as error:
            diagnostics.append(f"workspace cleanup failed: {type(error).__name__}: {error}")
        else:
            if cleanup is not None:
                diagnostics.append(execution_workspace._cleanup_diagnostic(cleanup))

    if diagnostics:
        failure = CheckExecutionFailure(
            "cleanup_failed",
            "; ".join(diagnostics),
            "Inspect the retained run path before retrying.",
        )
        if observation is None:
            return RepositoryCheckObservation(
                invocation,
                None,
                None,
                None,
                (),
                failure,
            )
        return replace(observation, error=failure)
    if observation is None:
        return _check_failure(
            invocation,
            "cleanup_failed",
            "workspace execution produced no observation",
        )
    return observation


def _dependency_name(invocation: CheckInvocation) -> DependencyName:
    if invocation.name in {"ruff", "annotations", "annotations-fix"}:
        return "ruff"
    return invocation.name


def _classify_primary_error(process: ExecutedProcess) -> CheckExecutionFailure | None:
    if process.spawn_error is not None or process.returncode is None:
        return CheckExecutionFailure(
            "spawn_failed",
            f"Check process could not be spawned: {process.spawn_error}",
            None,
        )
    if process.returncode < 0:
        return CheckExecutionFailure(
            "terminated_by_signal",
            f"Check process terminated by signal {-process.returncode}.",
            None,
        )
    if process.returncode not in {0, 1}:
        return CheckExecutionFailure(
            "check_execution_failed",
            f"Check process exited with reserved error status {process.returncode}.",
            None,
        )
    return None


def _check_failure(
    invocation: CheckInvocation,
    code: CheckExecutionErrorCode,
    message: str,
) -> RepositoryCheckObservation:
    return RepositoryCheckObservation(
        invocation,
        None,
        None,
        None,
        (),
        CheckExecutionFailure(code, message, None),
    )
