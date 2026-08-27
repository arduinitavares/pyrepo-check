# Repository Environment Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an install-once Python 3.13.15+ pyrepo-check controller run and
prove every selected Check inside one uv-managed Repository Environment on
CPython 3.10 through 3.13.

**Architecture:** Planning becomes environment-neutral and records only root,
Repository Python selection, Check arguments, pytest intent, and Coverage intent.
A deep `repository_executor.py` module owns uv preparation, repository safety,
dependency proof, secure launcher staging, execution, and typed observations;
`reporting.py` projects those observations through one schema-v2 model. Existing
pytest and Coverage modules keep their specialized artifact protocols but consume
the already-proved Repository Environment.

**Tech Stack:** Python `>=3.13.15` controller; Repository CPython 3.10-3.13;
stdlib `argparse`, `dataclasses`, `hashlib`, `importlib.metadata`, `json`,
`pathlib`, `runpy`, `subprocess`, and descriptor-relative filesystem APIs; uv
`0.10.12` in CI; repository-owned Ruff `>=0.15,<1`, Ty `>=0.0.35,<0.1`, Bandit
`>=1.9,<2`, pytest `>=8,<9`, and Coverage.py `>=7.15,<8`.

**Spec:**
[`docs/superpowers/specs/2026-08-26-tool-repository-environments-design.md`](../specs/2026-08-26-tool-repository-environments-design.md)

## Global Constraints

- pyrepo-check itself continues to require Python `>=3.13.15` and has no runtime
  dependencies.
- One run proves exactly one uv-selected Repository CPython: 3.10, 3.11, 3.12,
  or 3.13. Python 3.14 and non-CPython implementations are not supported here.
- Every executable selected Check runs in the Repository Environment. The
  Repository Environment never imports pyrepo-check.
- A present, current `uv.lock` is mandatory. pyrepo-check uses `uv run --locked`
  and never writes `pyproject.toml`, `uv.lock`, or dependency configuration.
- uv may create or synchronize only a safe ignored, untracked, non-symlink
  `.venv`; its repository-controlled default dependency selection remains intact.
- Do not guess dependency groups, extras, package-manager plugins, or tool
  installation commands. Missing/incompatible dependencies become typed evidence.
- Preserve exact Check order: Ruff, annotations, Ty, Bandit, pytest.
  `annotations-fix` remains explicit-only and intentionally mutating.
- Preserve targets, Test Shortcuts, annotation policy, Ty policy, Bandit policy,
  pytest evidence, Coverage scope/guidance, and one-pytest-run behavior.
- Static Checks retain repository-owned Ruff/Ty Analysis Python configuration;
  pyrepo-check supplies no target-version override.
- Schema version 2 is the only emitted schema after the reporting task lands.
  Do not retain a schema-v1 compatibility mode.
- Public exits are stable: complete pass `0`, complete findings `1`, any typed
  planning/environment/dependency/execution/evidence error `2`.
- Use argument vectors, never shell command strings. Preserve bounded process
  capture, JSON isolation, artifact limits, no-follow reads, and cleanup proofs.
- Every production change starts with a failing test, receives a focused GREEN
  run, and ends in a coherent commit suitable for fresh independent review.
- After each task's focused checks, run `uv run --frozen pyrepo-check --all` and
  require a pass before committing that task.
- Do not merge, push, publish, remove the worktree, or update personal Codex or
  Antigravity Skill copies without separate user approval.

## Baseline

| Item | Evidence |
| --- | --- |
| Branch | `dev/controller-repository-environments` |
| Approved design commit | `db4f05f` |
| Strict gate | Ruff, annotations, Ty, Bandit, and pytest passed |
| Test count | 1,314 passed |
| Native coverage | 86.64%, above the configured 86.01% minimum |
| Current limitation | Plans embed uv commands and pytest/Coverage reject Repository Python below 3.13.15 |

## File Structure

- `src/pyrepo_check/execution_workspace.py`: shared implementation for per-Check
  exclusive run directories,
  held-descriptor identity checks, quarantine cleanup, and cleanup observations.
- `src/pyrepo_check/planning.py`: pure Repository Python selection and semantic
  `CheckInvocation` plans.
- `src/pyrepo_check/config.py`: repository targets, Test Shortcuts, and native
  Coverage configuration only; no frozen/unfrozen decision.
- `src/pyrepo_check/execution.py`: immutable Tool, Repository Environment, process,
  dependency, Check-start, Check, and run observations; bounded subprocess adapter;
  and the stable `execute_plan` seam. Its Repository Executor import remains local
  to that function, preventing a runtime cycle.
- `src/pyrepo_check/repository_environment.py`: child-environment sanitization,
  environment/dependency probe source, strict parsers, supported dependency table,
  and immutable environment observations.
- `src/pyrepo_check/repository_safety.py`: Git/non-Git `.venv` safety,
  protected-file digests, tracked-file snapshots, and mutation comparison.
- `src/pyrepo_check/_check_launcher.py`: copied standalone Python 3.10-compatible
  launcher; imports only the standard library.
- `src/pyrepo_check/check_launcher.py`: controller-side launcher staging, marker
  command construction, digest binding, validation, and cleanup observations.
- `src/pyrepo_check/repository_executor.py`: deep orchestration seam for
  preparation, dependency probes, Check execution, continuation, and final state
  verification.
- `src/pyrepo_check/pytest_execution.py`: pytest reporter/artifact coordinator that
  consumes a prepared Repository Environment and shared workspace.
- `src/pyrepo_check/coverage_execution.py`: Coverage run/JSON artifact coordinator;
  no Python/dependency preflight ownership.
- `src/pyrepo_check/reporting_schema.py`: schema-v2 dataclasses, exact JSON payload
  construction, structural validation, and schema constants.
- `src/pyrepo_check/reporting.py`: report composition, cross-field validation,
  terminal projection, advisories, and stable public exit selection.
- `tests/test_execution_workspace.py`, `tests/test_repository_environment.py`,
  `tests/test_repository_safety.py`, `tests/test_check_launcher.py`,
  `tests/test_repository_executor.py`, and `tests/test_reporting_schema_v2.py`:
  focused fast contracts for the new modules.
- `tests/test_repository_integration.py` and
  `tests/test_repository_python_matrix.py`: real uv and 3.10-3.13 boundary proof.
- `.github/workflows/repository-python-matrix.yml`: pinned-uv compatibility jobs.
- `docs/reference/agent-report-schema-v2.md`: public machine-readable contract.

## Dependency Graph

```text
Task 1 shared secure workspace
  -> Task 6 launcher execution
  -> Task 7 pytest/Coverage adaptation

Task 2 intent-only planning
  -> Task 3 Repository Environment preparation
  -> Task 5 dependency selection
  -> Task 6 Check command construction

Task 3 locked preparation service
  -> Task 4 repository-state protection and internal executor
  -> Task 5 dependency probes

Tasks 1-5
  -> Task 6 ordinary Check execution
  -> Task 7 pytest/Coverage execution

Tasks 2-7
  -> Task 8A internal schema-v2 models and validation
  -> Task 8B atomic executor/schema-v2/CLI cutover
  -> Task 9 real uv and Python matrix

Tasks 1-9
  -> Task 10 documentation and completion gate
```

## Plan Publication Gate

This plan's documentation-only publication commit also records the design status as
“Approved.” That checkpoint must exist before Task 1 and is not runtime
implementation.

---

### Task 1: Extract the Shared Secure Execution Workspace

**Files:**
- Create: `src/pyrepo_check/execution_workspace.py`
- Create: `tests/test_execution_workspace.py`
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `tests/test_pytest_execution.py`

**Interfaces:**
- Produces `RunWorkspace(path: Path, identity: tuple[int, int], parent_identity: tuple[int, int])`.
- Produces `VerifiedRunWorkspace(workspace, parent_descriptor, descriptor)` with
  `verify(gate: str) -> None` and `close() -> None`.
- Produces `create_run_workspace(repository_root: Path) -> RunWorkspace`,
  `open_verified_workspace(workspace: RunWorkspace) -> VerifiedRunWorkspace`, and
  `remove_run_workspace(workspace, *, repository_root, clock_ns) -> CleanupObservation | None`.
- Preserves all existing directory ownership, descriptor, entry/depth/time budget,
  quarantine, swap-detection, retained-path, and platform-capability behavior.

- [ ] **Step 1: Write the RED shared-workspace contract**

Create `tests/test_execution_workspace.py` with imports from the new module and
start with these boundary tests:

```python
from pathlib import Path

from pyrepo_check.execution_workspace import (
    create_run_workspace,
    open_verified_workspace,
    remove_run_workspace,
)


def test_workspace_is_exclusive_and_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    workspace = create_run_workspace(repository)
    verified = open_verified_workspace(workspace)
    try:
        verified.verify("test boundary")
        assert not workspace.path.is_relative_to(repository)
        assert workspace.path.is_dir()
    finally:
        verified.close()
        assert remove_run_workspace(
            workspace,
            repository_root=repository,
            clock_ns=lambda: 0,
        ) is None


def test_workspace_cleanup_never_traverses_replaced_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    workspace = create_run_workspace(repository)
    original = workspace.path.with_name(f"{workspace.path.name}-original")
    workspace.path.rename(original)
    workspace.path.symlink_to(outside, target_is_directory=True)

    observation = remove_run_workspace(
        workspace,
        repository_root=repository,
        clock_ns=lambda: 0,
    )

    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert protected.read_text(encoding="utf-8") == "keep"
```

Run:
`uv run --frozen python -m pytest tests/test_execution_workspace.py -vv`

Expected: FAIL because `pyrepo_check.execution_workspace` does not exist.

- [ ] **Step 2: Move the existing workspace implementation without semantic changes**

Move the run-directory dataclasses, descriptor verification, platform capability
checks, creation/opening, manifest walk, quarantine removal, cleanup budget, and
retained-path proof from `pytest_execution.py` into `execution_workspace.py`.
Rename only the exported seam:

```python
@dataclass(frozen=True)
class RunWorkspace:
    path: Path
    identity: tuple[int, int]
    parent_identity: tuple[int, int]


@dataclass
class VerifiedRunWorkspace:
    workspace: RunWorkspace
    parent_descriptor: int
    descriptor: int
```

`VerifiedRunWorkspace.verify(gate: str) -> None` performs the existing complete
identity proof, and `close() -> None` closes both held descriptors with the existing
error precedence.

Keep cleanup limit values exactly: 4,096 entries, depth 64, and
5,000,000,000 nanoseconds. Update pytest execution to consume the new seam and
move the existing cleanup/swap/capability tests to `test_execution_workspace.py`
without weakening their assertions.

- [ ] **Step 3: Prove extraction parity**

Run:

```bash
uv run --frozen python -m pytest tests/test_execution_workspace.py tests/test_pytest_execution.py -q
uv run --frozen python -m ruff check src/pyrepo_check/execution_workspace.py src/pyrepo_check/pytest_execution.py tests/test_execution_workspace.py tests/test_pytest_execution.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
```

Expected: all pass; pytest process commands, artifacts, and cleanup diagnostics are
unchanged.

- [ ] **Step 4: Commit the behavior-preserving extraction**

```bash
git add src/pyrepo_check/execution_workspace.py src/pyrepo_check/pytest_execution.py tests/test_execution_workspace.py tests/test_pytest_execution.py
git commit -m "refactor: share secure execution workspace"
```

### Task 2: Make Planning Environment-Neutral

**Files:**
- Modify: `src/pyrepo_check/config.py`
- Modify: `src/pyrepo_check/planning.py`
- Modify: `src/pyrepo_check/cli.py`
- Modify: `src/pyrepo_check/execution.py`
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `src/pyrepo_check/coverage_execution.py`
- Modify: `src/pyrepo_check/reporting.py`
- Modify: `src/pyrepo_check/runner.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_planning.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_runner.py`

**Interfaces:**
- Adds `--python REQUEST`; accepted requests are `3.10`, `3.11`, `3.12`,
  `3.13`, or an exact three-part patch within those minors.
- Keeps `RunRequest.no_frozen` only as transition intent that always raises
  `unsafe_unlocked_execution`; it never reaches configuration or execution.
- Produces `DefaultRepositoryPython`, `ExplicitRepositoryPython`,
  `RepositoryPythonSelection`, `CheckInvocation`, and the new `RunPlan` below.
- Removes `ProjectConfig.frozen`, the `no_frozen` parameter from
  `load_project_config`,
  `PlannedCheck.command`, `PlannedCheck.cwd`, both `consumer_python` fields, and
  `_uv_consumer_python`.
- Preserves the two-field `runner.Check` facade and its four importable names.
  `build_checks` continues to create canonical locked command vectors;
  `run_checks` preserves exact caller-supplied raw vectors, cwd, injected-runner
  behavior, and integer exit semantics through a narrow
  `execution.execute_legacy_commands` helper. This undocumented compatibility
  facade emits no Agent Report and is never called by the CLI/`RunPlan` path.

- [ ] **Step 1: Write RED Python-selection and safe-unlocked tests**

Add these planning contracts in `tests/test_planning.py`:

```python
@pytest.mark.parametrize(
    "request",
    ("3.10", "3.10.19", "3.11", "3.12.11", "3.13", "3.13.15"),
)
def test_plans_one_explicit_repository_python(tmp_path: Path, request: str) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("ty",), False, False, repository_python=request),
        make_config(tmp_path),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert plan.repository_python == ExplicitRepositoryPython(request)
    assert plan.root == tmp_path.resolve()


@pytest.mark.parametrize("request", ("3.9", "3.14", "3", "3.12.x", "pypy3.12"))
def test_rejects_unsupported_repository_python_without_a_plan(
    tmp_path: Path,
    request: str,
) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("ty",), False, False, repository_python=request),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "invalid_arguments"


def test_no_frozen_is_a_stable_zero_process_planning_error(tmp_path: Path) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("ty",), False, True),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "unsafe_unlocked_execution"
    assert str(raised.value) == (
        "--no-frozen is incompatible with repository-safe execution."
    )
    assert raised.value.hint == (
        "Update uv.lock explicitly, then rerun without --no-frozen."
    )
```

Add CLI tests that `--python` works before or after Check tokens, missing
`pyproject.toml` yields `uv_project_required`, and both planning errors call the
injected runner zero times.

Run:
`uv run --frozen python -m pytest tests/test_planning.py tests/test_cli.py -q`

Expected: FAIL because the request, facts, error codes, and parser option do not
exist.

- [ ] **Step 2: Replace command plans with semantic intent**

Implement these exact public-internal types in `planning.py`:

```python
@dataclass(frozen=True)
class DefaultRepositoryPython:
    kind: Literal["default"] = "default"


@dataclass(frozen=True)
class ExplicitRepositoryPython:
    request: str
    kind: Literal["explicit"] = "explicit"


RepositoryPythonSelection = DefaultRepositoryPython | ExplicitRepositoryPython


@dataclass(frozen=True)
class CoverageExecutionPlan:
    config_path: Path
    fail_under: int | float | None
    artifact_protocol: Literal["coverage_v1"] = "coverage_v1"


@dataclass(frozen=True)
class PytestExecutionPlan:
    pytest_args: tuple[str, ...]
    artifact_protocol: Literal["pytest_v1"] = "pytest_v1"
    coverage: CoverageExecutionPlan | None = None


@dataclass(frozen=True)
class CheckInvocation:
    name: CheckName
    arguments: tuple[str, ...]
    pytest: PytestExecutionPlan | None = None


@dataclass(frozen=True)
class RunPlan:
    root: Path
    repository_python: RepositoryPythonSelection
    mode: RunMode
    targets: tuple[str, ...]
    checks: tuple[CheckInvocation, ...]
    output_format: OutputFormat = "terminal"
    test_shortcut: str | None = None
    pytest_args: tuple[str, ...] | None = None
    planned_test_scope: PlannedTestScope = "not_selected"
    planned_coverage_scope: PlannedCoverageScope = "not_requested"
```

Check arguments begin after the module name: Ruff uses
`("check", *ruff_targets)`, Ty uses `("check", *explicit_targets)`, Bandit uses
`("-c", "pyproject.toml", *bandit_target_args)`, and pytest uses its exact
selector tuple. Preserve target order and duplicates.

- [ ] **Step 3: Move temporary command expansion into execution**

Until the Repository Executor replaces it in Tasks 3-7, keep the suite green by
building one locked uv command inside `execution.py`, never in planning:

```python
CHECK_MODULES: Mapping[CheckName, str] = {
    "ruff": "ruff",
    "annotations": "ruff",
    "annotations-fix": "ruff",
    "ty": "ty",
    "bandit": "bandit",
    "pytest": "pytest",
}


def locked_module_command(plan: RunPlan, check: CheckInvocation) -> tuple[str, ...]:
    selector = (
        ()
        if isinstance(plan.repository_python, DefaultRepositoryPython)
        else ("--python", plan.repository_python.request)
    )
    return (
        "uv",
        "run",
        "--locked",
        *selector,
        "python",
        "-m",
        CHECK_MODULES[check.name],
        *check.arguments,
    )
```

Adapt pytest/Coverage command helpers to accept a supplied locked prefix and
`plan.root`. Update reporting observation matching to use `CheckInvocation`.
Remove raw-command tests from planning and assert semantic arguments there; move
exact process argv assertions to `test_execution.py`.

- [ ] **Step 4: Remove frozen configuration and preserve the bounded runner facade**

Make `ProjectConfig` contain only root, targets, Test Shortcuts, and Coverage
configuration. Add `PlanningFacts.pyproject_exists` and make `plan_run` check it
before selection. Preserve `Check(name, command)`, `build_checks`, `select_checks`,
and `run_checks`. Move its existing raw-command loop and first-positive return rule
unchanged into `execution.execute_legacy_commands`; `runner.py` remains a thin
adapter and the current manual-`Check` delegation test stays behaviorally equivalent.
Add a negative assertion that no CLI or `RunPlan` code references this helper. The
new Repository Environment and schema contracts apply only to semantic plans; this
compatibility escape hatch neither claims nor emits that evidence.

- [ ] **Step 5: Run the migrated planner/executor contract**

```bash
uv run --frozen python -m pytest tests/test_config.py tests/test_planning.py tests/test_execution.py tests/test_cli.py tests/test_runner.py -q
uv run --frozen python -m ruff check src/pyrepo_check/config.py src/pyrepo_check/planning.py src/pyrepo_check/cli.py src/pyrepo_check/execution.py src/pyrepo_check/runner.py tests/test_config.py tests/test_planning.py tests/test_execution.py tests/test_cli.py tests/test_runner.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
```

Expected: pass. Plans contain no uv command, executable, `cwd`, or frozen state;
execution owns the temporary locked command expansion.

- [ ] **Step 6: Commit the intent-only boundary**

```bash
git add src/pyrepo_check/config.py src/pyrepo_check/planning.py src/pyrepo_check/cli.py src/pyrepo_check/execution.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/reporting.py src/pyrepo_check/runner.py tests/test_config.py tests/test_planning.py tests/test_execution.py tests/test_cli.py tests/test_runner.py
git commit -m "refactor: make check plans environment neutral"
```

### Task 3: Prepare and Observe the Locked Repository Environment

**Files:**
- Create: `src/pyrepo_check/repository_environment.py`
- Modify: `src/pyrepo_check/execution.py`
- Create: `tests/test_repository_environment.py`
- Modify: `tests/support.py`
- Modify: `tests/test_execution.py`

**Interfaces:**
- `execute_process` remains the only production subprocess adapter.
- `inspect_repository_lock(root) -> RepositoryLockPresence` performs the direct,
  zero-process presence check. `prepare_repository_environment(plan, *,
  lock_presence=None, runner, clock_ns) -> RepositoryPreparation` validates or
  obtains that observation, returns typed missing/unsafe evidence without a process,
  or performs the uv probe, one locked environment probe, and exact Repository
  Python validation.
- `RepositoryPreparation.prepared` is either one immutable
  `PreparedRepositoryEnvironment` or `None`; its observation is always present.
- This task does not route the public CLI through preparation. The cutover waits
  for Task 8B, after safety and every Check path are complete, so no intermediate
  commit exposes partial Repository Environment behavior.

- [ ] **Step 1: Write RED child-environment sanitation tests**

Create `tests/test_repository_environment.py` with a table that proves every
selection override is removed and every approved operational variable survives:

```python
def test_sanitized_environment_keeps_only_approved_uv_controls() -> None:
    source = {
        "PATH": "/bin",
        "APP_TOKEN": "repository-secret",
        "PYTHONPATH": "/controller/src",
        "VIRTUAL_ENV": "/controller/.venv",
        "UV_PROJECT": "/wrong",
        "UV_PYTHON": "3.14",
        "UV_GROUP": "wrong",
        "UV_INDEX_URL": "https://index.example/simple",
        "UV_INDEX_PRIVATE_USERNAME": "agent",
        "UV_INDEX_PRIVATE_PASSWORD": "secret",
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": "/cache",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON_INSTALL_DIR": "/pythons",
    }

    cleaned = sanitized_repository_environment(source)

    assert cleaned["PATH"] == "/bin"
    assert cleaned["APP_TOKEN"] == "repository-secret"
    assert "PYTHONPATH" not in cleaned
    assert "VIRTUAL_ENV" not in cleaned
    assert "UV_PROJECT" not in cleaned
    assert "UV_PYTHON" not in cleaned
    assert "UV_GROUP" not in cleaned
    assert cleaned["UV_INDEX_URL"] == source["UV_INDEX_URL"]
    assert cleaned["UV_INDEX_PRIVATE_USERNAME"] == "agent"
    assert cleaned["UV_INDEX_PRIVATE_PASSWORD"] == "secret"
    assert cleaned["UV_OFFLINE"] == "1"
    assert cleaned["UV_CACHE_DIR"] == "/cache"
    assert cleaned["UV_PYTHON_DOWNLOADS"] == "never"
    assert cleaned["UV_PYTHON_INSTALL_DIR"] == "/pythons"
```

Parametrize the remaining exact allowlist from the spec, including removal of
all `UV_*` values not explicitly allowed. Add a second table for
`UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_CACHE_DIR`, and
`UV_PYTHON_BIN_DIR`: an absolute external directory is accepted, while a relative
path, the repository root, a lexical descendant of the repository, or an external
symlink resolving into the repository is rejected before any subprocess. Exercise
the same containment rule for each documented uv storage destination derived from
`HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, or `XDG_BIN_HOME` when no explicit
storage override replaces it. The fixture must prove that a rejected value cannot
create or change repository bytes. Add a conditional case-insensitive-filesystem
case: construct a differently cased path naming the same repository, confirm that
`os.path.samefile(alias, root)` is true, point one storage destination beneath that
alias, and require rejection. Skip only when the filesystem proves the alias does
not exist or is not the same directory.

Run:
`uv run --frozen python -m pytest tests/test_repository_environment.py -k sanitized -vv`

Expected: FAIL because the module does not exist.

- [ ] **Step 2: Add immutable execution observations and exact sanitization**

Define these internal observations beside existing `CapturedBytes` and
`ExecutedProcess` in `execution.py`. Use `TYPE_CHECKING` imports for specialized
pytest/Coverage observation types and keep Repository Executor imports function-local,
so producers can import observations without a runtime cycle:

```python
PythonVersion = tuple[int, int, int]
LockStatus = Literal["current", "missing", "unverified"]
MutationProtection = Literal["unobserved", "protected_files", "tracked_files"]
EnvironmentErrorCode = Literal[
    "repository_lock_missing",
    "uv_unavailable",
    "repository_environment_failed",
    "repository_python_unsupported",
    "unsafe_repository_environment",
    "environment_evidence_invalid",
    "repository_state_changed",
]

CheckExecutionErrorCode = Literal[
    "spawn_failed",
    "terminated_by_signal",
    "pytest_preflight_failed",
    "pytest_evidence_error",
    "coverage_preflight_failed",
    "missing_primary_process",
    "cleanup_failed",
    "repository_environment_unavailable",
    "check_dependency_missing",
    "check_dependency_incompatible",
    "check_dependency_shadowed",
    "check_dependency_unusable",
    "check_start_evidence_invalid",
    "check_execution_failed",
]


@dataclass(frozen=True)
class PythonObservation:
    implementation: str
    version: PythonVersion
    executable: Path


@dataclass(frozen=True)
class ToolEnvironmentObservation:
    pyrepo_check_version: str
    python: PythonObservation


@dataclass(frozen=True)
class EnvironmentFailureObservation:
    code: EnvironmentErrorCode
    message: str
    hint: str | None


DependencyStatus = Literal[
    "available", "missing", "incompatible", "shadowed", "unusable", "unobserved"
]


@dataclass(frozen=True)
class CheckExecutionFailure:
    code: CheckExecutionErrorCode
    message: str
    hint: str | None


CheckModule = Literal["ruff", "ty", "bandit", "pytest", "coverage"]


@dataclass(frozen=True)
class CheckStartObservation:
    schema_version: Literal[1]
    check: CheckName
    module: CheckModule
    arguments_sha256: str
    python: PythonObservation


@dataclass(frozen=True)
class AnalysisPythonAuthorityObservation:
    authority: Literal["repository_tool"] = "repository_tool"
    pyrepo_check_override: None = None


@dataclass(frozen=True)
class DependencyObservation:
    name: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    module: str
    required: str
    status: DependencyStatus
    version: str | None
    origin: str | None
    process: ExecutedProcess | None
    error: CheckExecutionFailure | None


@dataclass(frozen=True)
class PreparedRepositoryEnvironment:
    root: Path
    path: Path
    python: PythonObservation
    python_selection: RepositoryPythonSelection
    manager_version: str
    child_environment: Mapping[str, str]


@dataclass(frozen=True)
class RepositoryEnvironmentObservation:
    manager_version: str | None
    path: Path | None
    python_selection: RepositoryPythonSelection
    python: PythonObservation | None
    lock_path: Path
    lock_status: LockStatus
    mutation_protection: MutationProtection
    dependencies: tuple[DependencyObservation, ...]
    processes: tuple[ExecutedProcess, ...]
    error: EnvironmentFailureObservation | None


@dataclass(frozen=True)
class RepositoryPreparation:
    prepared: PreparedRepositoryEnvironment | None
    observation: RepositoryEnvironmentObservation


@dataclass(frozen=True)
class RepositoryLockPresence:
    path: Path
    state: Literal["missing", "present", "unsafe"]
    diagnostic: str | None


@dataclass(frozen=True)
class RepositoryCheckObservation:
    invocation: CheckInvocation
    execution_environment: Literal["repository"] | None
    analysis_python_authority: AnalysisPythonAuthorityObservation | None
    start: CheckStartObservation | None
    processes: tuple[ExecutedProcess, ...]
    error: CheckExecutionFailure | None
    pytest: PytestExecutionObservation | None = None
    coverage: CoverageExecutionObservation | None = None


@dataclass(frozen=True)
class RepositoryExecutionResult:
    tool_environment: ToolEnvironmentObservation
    repository_environment: RepositoryEnvironmentObservation
    checks: tuple[RepositoryCheckObservation, ...]


```

Also define `observe_tool_environment() -> ToolEnvironmentObservation` in this
module from `sys.implementation.name`, `sys.version_info[:3]`, normalized absolute
`sys.executable`, and `pyrepo_check.__version__`. It runs no subprocess and reads no
target repository state. In Task 8B the CLI calls it exactly once before planning and
passes the same observation to either planning-error reporting or execution. Direct
internal executor callers may omit it and obtain one local observation. Tests inject
an exact value rather than depending on the host patch version.

`inspect_repository_lock` uses `lstat`, so a dangling symlink or non-regular entry
is not mistaken for an absent lock. Only `FileNotFoundError` produces `missing`;
symlink, directory, permission, and other inspection failures produce `unsafe` and
later `unsafe_repository_environment`, all without spawning a process.

Add these reusable scripted-test helpers to `tests/support.py` in the same task:

```python
def monotonic_clock() -> Callable[[], int]:
    values = iter(range(0, 10_000_000_000, 1_000_000))
    return lambda: next(values)


def environment_probe_bytes(
    *,
    version: tuple[int, int, int],
    executable: Path,
    environment_root: Path,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "implementation": "cpython",
            "version": list(version),
            "executable": str(executable),
            "environment_root": str(environment_root),
        },
        separators=(",", ":"),
    ).encode()


def write_minimal_uv_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\nrequires-python='>=3.10'\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\nrevision = 3\n", encoding="utf-8")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").write_bytes(b"")
```

`focused_plan(root, check, repository_python=None)` is a test factory that returns
the final Task 2 `RunPlan` with one canonical `CheckInvocation`, resolved root,
focused mode, JSON output, and either `DefaultRepositoryPython()` or
`ExplicitRepositoryPython(repository_python)`.

Use an immutable `MappingProxyType` for `child_environment`; never place its
values in public evidence. Add
`validate_uv_storage_boundaries(root, child_environment) -> EnvironmentFailureObservation | None`.
For every effective explicit storage path, first require an absolute value; a
relative value is immediately `unsafe_repository_environment`. For every
unoverridden path, calculate the documented uv `0.10.12` platform destination from
the applicable HOME/XDG input, requiring each supplied base itself to be absolute.
Compare both the normalized lexical destination and its `resolve(strict=False)` form
against the already-resolved project root, and inspect existing ancestors without
following a loop. For every existing destination prefix, use `lstat` plus
device/inode identity (`os.path.samefile` where available) to detect a case-folded,
mount, or other filesystem alias equal to the repository root; a later component
below that prefix is therefore contained even when path strings differ. Equality or
containment by string, resolved path, or filesystem identity is unsafe. Run this
zero-process validation only after the direct lock-presence check, but before
`uv --version`, so preserved uv controls can never redirect cache, managed-Python
install, cache, or bin writes into tracked repository state.

- [ ] **Step 3: Write RED locked-preparation ordering and evidence tests**

Use `RecordingRunner` to cover these exact paths in
`tests/test_repository_environment.py`:

```python
def test_missing_lock_stops_before_every_process(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert runner.calls == []
    assert preparation.prepared is None
    assert preparation.observation.lock_status == "missing"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "repository_lock_missing"


def test_locked_probe_selects_one_repository_python(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 12, 11),
                executable=tmp_path / ".venv/bin/python",
                environment_root=tmp_path / ".venv",
            ),
        )
    )

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty", repository_python="3.12"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert tuple(call.command for call in runner.calls) == (
        ("uv", "--version"),
        (
            "uv", "run", "--locked", "--python", "3.12",
            "python", "-c", ENVIRONMENT_PROBE_SOURCE,
        ),
    )
    assert preparation.observation.lock_status == "current"
    assert preparation.prepared is not None
    assert preparation.prepared.python.version == (3, 12, 11)
```

Also cover missing/malformed `uv --version`, locked-probe nonzero, spawn, signal,
truncated output, duplicate/unknown keys, trailing data, non-CPython, unsupported
versions, unsafe/symlinked `.venv`, executable lexical containment,
environment-root equality, executable/root coherence, and explicit-request
contradiction. The only valid `environment_root` is the normalized absolute
`plan.root / ".venv"`; that directory itself must be real and non-symlink. The
normalized absolute executable path reported by Python must be lexically contained
beneath that exact root. Do **not** resolve the executable target for containment:
ordinary uv virtual environments legitimately use `.venv/bin/python` symlinks to
uv's external managed-Python installation. Reject an external root, a nested fake
root, or a lexical root/executable disagreement. An external reported environment
root or a directly observed symlinked `.venv` is
`unsafe_repository_environment`; a nested fake root or executable/root contradiction
with an otherwise safe exact `.venv` is `environment_evidence_invalid`. Reject a
supplied `RepositoryLockPresence` whose resolved path does not exactly equal
`plan.root / "uv.lock"`, and prove a missing observation spawns no process.

Run:
`uv run --frozen python -m pytest tests/test_repository_environment.py -q`

Expected: FAIL because preparation and probe parsing are absent.

- [ ] **Step 4: Implement the standalone environment probe and parser**

Set `ENVIRONMENT_PROBE_SOURCE` to Python 3.10-compatible stdlib source that emits
exactly one compact object:

```python
import json
import os
import sys

record = {
    "schema_version": 1,
    "implementation": sys.implementation.name,
    "version": list(sys.version_info[:3]),
    "executable": os.path.abspath(os.path.normpath(sys.executable)),
    "environment_root": os.path.abspath(os.path.normpath(sys.prefix)),
}
print(json.dumps(record, separators=(",", ":")))
```

Parse at most 65,536 bytes through `artifact_safety.load_bounded_json`, require
exact keys/types and no trailing non-whitespace, then require CPython 3.10-3.13
and exact environment-root/executable lexical coherence in the non-symlink project
`.venv` as specified in Step 3. Preserve a normal `.venv/bin/python` symlink whose
target is an external uv-managed interpreter.

- [ ] **Step 5: Implement the preparation service without public routing**

Before spawning, test `uv.lock` directly. Capture `uv --version`, then run exactly
one locked environment probe from `plan.root`. After the lock exists, validate every
effective uv writable-storage path before `uv --version`; a rejected boundary returns
typed unsafe evidence with no process. Use the explicit request only for the locked
probe. After success, build every subsequent uv prefix with the observed absolute
executable:

```python
def locked_repository_prefix(
    prepared: PreparedRepositoryEnvironment,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--locked",
        "--python",
        str(prepared.python.executable),
        "python",
    )
```

Return a `RepositoryPreparation` for every path. Do not yet change
`execution.execute_plan` or the CLI: Task 4 first proves `.venv` and repository
state safety, Tasks 5-7 complete Check execution, and Task 8B makes the atomic public
cutover. This temporary internal service is exercised directly by its focused tests
and has no alternate public execution path.

- [ ] **Step 6: Run focused preparation checks and commit**

```bash
uv run --frozen python -m pytest tests/test_repository_environment.py tests/test_execution.py -q
uv run --frozen python -m ruff check src/pyrepo_check/execution.py src/pyrepo_check/repository_environment.py tests/support.py tests/test_repository_environment.py tests/test_execution.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add src/pyrepo_check/execution.py src/pyrepo_check/repository_environment.py tests/support.py tests/test_repository_environment.py tests/test_execution.py
git commit -m "feat: prepare locked repository environment"
```

### Task 4: Prove Repository State Safety

**Files:**
- Create: `src/pyrepo_check/repository_safety.py`
- Create: `src/pyrepo_check/repository_executor.py`
- Create: `tests/test_repository_safety.py`
- Create: `tests/test_repository_executor.py`
- Modify: `src/pyrepo_check/repository_environment.py`
- Modify: `tests/test_compatibility.py`

**Interfaces:**
- Produces `capture_repository_baseline(root, *, runner, clock_ns,
  controller_environment=None) -> RepositoryBaselineResult`.
- Produces `verify_repository_state(snapshot, *, annotations_fix_selected,
  runner, clock_ns) -> RepositoryVerificationResult`.
- Uses the exact sanitized Git environment: remove all `GIT_*`, then set only
  `GIT_OPTIONAL_LOCKS=0` and `LC_ALL=C`.
- `mutation_protection` is `tracked_files` only after valid before/after Git
  snapshots, `protected_files` only for valid non-Git dependency-file digests,
  and `unobserved` otherwise.
- `RepositoryBaselineResult` contains one `RepositoryStateSnapshot | None`, its
  ordered safety processes, and one environment failure or null.
- `RepositoryVerificationResult` contains the final ordered safety processes,
  mutation-protection value, and one `repository_state_changed` failure or null.
- `repository_executor.prepare_safe_repository(plan, *, runner=None, clock_ns) ->
  SafeRepositoryPreparation` composes the safety baseline and Task 3 preparation
  without executing a Check. It prepends baseline safety processes to the
  preparation observation in actual execution order. The existing public path
  remains unchanged.

Use these exact immutable result shapes:

```python
@dataclass(frozen=True)
class ProtectedFileSnapshot:
    path: str
    kind: Literal["regular", "missing", "unsafe"]
    mode: int | None
    sha256: str | None


@dataclass(frozen=True)
class TrackedFileSnapshot:
    path: str
    index_mode: str
    index_object: str
    working_tree_kind: Literal["regular", "symlink", "missing", "gitlink", "other"]
    working_tree_mode: int | None
    sha256: str | None


@dataclass(frozen=True)
class RepositoryStateSnapshot:
    git_root: Path | None
    protected_files: tuple[ProtectedFileSnapshot, ...]
    tracked_files: tuple[TrackedFileSnapshot, ...]


@dataclass(frozen=True)
class RepositoryBaselineResult:
    snapshot: RepositoryStateSnapshot | None
    processes: tuple[ExecutedProcess, ...]
    error: EnvironmentFailureObservation | None


@dataclass(frozen=True)
class RepositoryVerificationResult:
    processes: tuple[ExecutedProcess, ...]
    mutation_protection: MutationProtection
    error: EnvironmentFailureObservation | None


@dataclass(frozen=True)
class SafeRepositoryPreparation:
    baseline: RepositoryStateSnapshot | None
    preparation: RepositoryPreparation
```

- [ ] **Step 1: Write RED `.venv` and Git-safety tests**

Create cases for ignored/untracked `.venv`, tracked `.venv`, unignored `.venv`,
symlinked `.venv`, Git marker with missing Git executable, non-Git roots, and an
unmerged index. Include this inherited-environment assertion:

```python
def test_git_probes_ignore_all_inherited_git_redirection(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    runner = RecordingRunner()
    controller = {
        "PATH": "/bin:/usr/bin",
        "GIT_DIR": "/wrong/git-dir",
        "GIT_WORK_TREE": "/wrong/worktree",
        "GIT_INDEX_FILE": "/wrong/index",
        "GIT_CONFIG_COUNT": "1",
    }

    capture_repository_baseline(
        root,
        runner=runner,
        clock_ns=monotonic_clock(),
        controller_environment=controller,
    )

    git_environments = [call.env for call in runner.calls if call.command[0] == "git"]
    assert git_environments
    assert all(environment is not None for environment in git_environments)
    assert all(environment["GIT_OPTIONAL_LOCKS"] == "0" for environment in git_environments)
    assert all(environment["LC_ALL"] == "C" for environment in git_environments)
    assert all("GIT_DIR" not in environment for environment in git_environments)
    assert all("GIT_WORK_TREE" not in environment for environment in git_environments)
    assert all("GIT_INDEX_FILE" not in environment for environment in git_environments)
    assert all("GIT_CONFIG_COUNT" not in environment for environment in git_environments)
```

Run:
`uv run --frozen python -m pytest tests/test_repository_safety.py -vv`

Expected: FAIL because the safety module does not exist.

The `initialize_git_fixture(root)` test helper creates `src/example.py`,
`pyproject.toml`, `uv.lock`, and `.gitignore` containing `.venv/`; then runs
`git init -q`, `git add .`, and a local-identity `git commit` with
`user.name=Fixture` and `user.email=fixture@example.invalid`. It returns the
resolved root and never mutates global Git configuration.

- [ ] **Step 2: Implement exact repository snapshots**

Run the four specified Git command shapes and parse NUL-delimited output without
locale-dependent prose. Define immutable snapshot entries containing repository-
relative path, index mode/object, working-tree kind/mode, and SHA-256 of regular
bytes or symlink-target bytes. Fingerprint regular non-symlink
`pyproject.toml` and `uv.lock` in Git and non-Git projects.

Reject initial nonzero index stages before uv. Rebuild the tracked list after the
Checks. Compare bytes even when the working tree was already dirty before the run.
Record Gitlinks by index object without descending into submodules.

- [ ] **Step 3: Write RED mutation comparison tests**

Cover unchanged clean state, unchanged already-dirty state, initially dirty file
changed again, mode change, symlink-target change, deletion, added tracked path,
unmerged stage appearing after execution, protected-file changes, Gitlink content
exclusion, and the `annotations-fix` exemption:

```python
def test_annotations_fix_exempts_source_bytes_but_never_dependency_files(
    tmp_path: Path,
) -> None:
    root = initialize_git_fixture(tmp_path)
    baseline = capture_repository_baseline(root, runner=None, clock_ns=monotonic_clock())
    assert baseline.snapshot is not None
    (root / "src/example.py").write_text("fixed = True\n", encoding="utf-8")

    allowed = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert allowed.error is None

    (root / "uv.lock").write_text("changed\n", encoding="utf-8")
    rejected = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert rejected.error is not None
    assert rejected.error.code == "repository_state_changed"
```

Then parametrize `annotations_fix_selected=True` cases proving the exemption is
exactly tracked regular-file or symlink-target **content equality**. A source-content
change with unchanged kind/mode is allowed; a mode change, regular-to-symlink or
symlink-to-regular change, deletion, newly tracked path, newly nonzero index stage,
Gitlink/index-object change, or any `pyproject.toml`/`uv.lock` fingerprint change
still returns `repository_state_changed`. Assert the final snapshot processes remain
present for every rejected case, so the flag cannot bypass verification wholesale.

- [ ] **Step 4: Compose safety and preparation without a partial Check executor**

Call `inspect_repository_lock` before any Git or uv process. A missing lock delegates
to Task 3's canonical missing-lock observation and returns with an empty process
tuple; an unsafe lock entry returns `unsafe_repository_environment` the same way.
Only when the lock is present may the executor capture the baseline before
`uv --version`; do not prepare an unsafe root. Then call Task 3 preparation with the
same root/path observation and return `SafeRepositoryPreparation`. Add
focused tests for missing lock, unsafe `.venv`, failed preparation, and zero Check
starts. Exercise
`verify_repository_state` separately to prove the post-run command is last and a
mismatch yields `repository_state_changed`. Do not add a partial Check executor and
do not change `execution.execute_plan`, `runner.py`, or the CLI in this task.

- [ ] **Step 5: Verify and commit repository safety**

```bash
uv run --frozen python -m pytest tests/test_repository_safety.py tests/test_repository_executor.py tests/test_compatibility.py -q
uv run --frozen python -m ruff check src/pyrepo_check/repository_safety.py src/pyrepo_check/repository_executor.py src/pyrepo_check/repository_environment.py tests/test_repository_safety.py tests/test_repository_executor.py tests/test_compatibility.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add src/pyrepo_check/repository_safety.py src/pyrepo_check/repository_executor.py src/pyrepo_check/repository_environment.py tests/test_repository_safety.py tests/test_repository_executor.py tests/test_compatibility.py
git commit -m "feat: protect repository state during checks"
```

### Task 5: Verify Repository-Owned Check Dependencies

**Files:**
- Modify: `src/pyrepo_check/repository_environment.py`
- Modify: `src/pyrepo_check/repository_executor.py`
- Modify: `tests/test_repository_environment.py`
- Modify: `tests/test_repository_executor.py`
- Modify: `tests/support.py`

**Interfaces:**
- Adds immutable `CheckDependency(name, module, minimum, maximum)` and populates
  the `DependencyObservation` model introduced by Task 3.
- Dependency states are exactly `available`, `missing`, `incompatible`,
  `shadowed`, `unusable`, and `unobserved`.
- Probes run once per unique selected dependency in first-required Check order.
- The supported table is Ruff `>=0.15,<1`, Ty `>=0.0.35,<0.1`, Bandit
  `>=1.9,<2`, pytest `>=8,<9`, and Coverage.py `>=7.15,<8`.

`CheckDependency.minimum` is an inclusive numeric tuple and `maximum` is an
exclusive numeric tuple. Test helpers `available_dependency(name, version)` and
`missing_dependency(name)` return complete `DependencyObservation` values using
the canonical module/range table; they never omit required fields. The helper does
not reorder an arbitrary Check sequence; normal plans already supply canonical Check
order.

- [ ] **Step 1: Write RED selection/order/range tests**

```python
@pytest.mark.parametrize(
    ("checks", "coverage", "expected"),
    (
        (("ruff", "annotations"), False, ("ruff",)),
        (("ty", "ruff", "bandit"), False, ("ty", "ruff", "bandit")),
        (("pytest",), False, ("pytest",)),
        (("pytest",), True, ("pytest", "coverage")),
    ),
)
def test_required_dependencies_are_unique_and_canonical(
    checks: tuple[str, ...],
    coverage: bool,
    expected: tuple[str, ...],
) -> None:
    assert tuple(
        dependency.name for dependency in required_dependencies(checks, coverage=coverage)
    ) == expected
```

Add stable numeric boundary cases at each inclusive minimum and exclusive maximum;
reject absent, unparsable, prerelease, postrelease, dev, and local versions.

- [ ] **Step 2: Write RED provenance and import classification tests**

Feed strict probe payloads for ordinary metadata-backed installs, missing
distribution/module, module shadowing by a repository file, missing `files`
metadata, editable/local `direct_url.json`, a distribution-recorded module file that
is a symlink into the repository, a symlinked parent package directory, ordinary
import failure, malformed JSON, process nonzero, signal, and spawn failure. Assert
the exact state, typed error code, version/origin nullability, process retention, and
remediation hint. Both symlink escapes must be `shadowed` and the repository payload
must prove it was never imported.

Run:
`uv run --frozen python -m pytest tests/test_repository_environment.py -k dependency -vv`

Expected: FAIL because dependency probes are absent.

- [ ] **Step 3: Implement the standalone dependency probe**

The Python 3.10-compatible source uses `importlib.util.find_spec` and
`importlib.metadata.distribution`. It emits exactly:

```json
{"schema_version":1,"distribution":"pytest","module":"pytest","status":"available","version":"8.4.2","origin":"/project/.venv/lib/python3.12/site-packages/pytest/__init__.py","diagnostic":null}
```

Normalize distribution/file names, reject editable or local-path
`direct_url.json`, require the module origin in the distribution's installed file
set, then import the module. Before import, require the reported origin to be a
regular non-symlink file; `lstat` every existing component from the resolved
distribution installation root through the module origin; require lexical and
resolved containment in both that distribution root and the prepared `.venv`; and
reject any symlink, special file, escape, or root disagreement as `shadowed`.
Resolve metadata entries without following a candidate module symlink before this
decision. Expected dependency states exit zero with valid JSON; probe
execution/evidence failures become `unobserved`.

- [ ] **Step 4: Continue independent dependency probes after errors**

Use one dependency result for all Ruff-backed Checks and probe every independently
required dependency even after an earlier missing, incompatible, shadowed, unusable,
or unobserved result. Return observations in canonical first-required order. Record
missing Coverage for Task 7's plain-pytest fallback without starting a Check here.
Task 6 maps these observations to Check-local errors and proves Check continuation.

- [ ] **Step 5: Keep specialized execution behind the later cutover boundary**

Pass dependency observations only through the internal Repository Executor seam in
this task. Do not yet remove the controller-era pytest/Coverage preflights or alter
the public `execute_pytest` path; Task 7 adds prepared-environment execution beside
it, and Task 8B removes the old path atomically. This avoids a broken intermediate
CLI while keeping dependency selection/provenance policy in one new module.

- [ ] **Step 6: Verify and commit dependency proof**

```bash
uv run --frozen python -m pytest tests/test_repository_environment.py tests/test_repository_executor.py -q
uv run --frozen python -m ruff check src/pyrepo_check/repository_environment.py src/pyrepo_check/repository_executor.py tests/support.py tests/test_repository_environment.py tests/test_repository_executor.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add src/pyrepo_check/repository_environment.py src/pyrepo_check/repository_executor.py tests/support.py tests/test_repository_environment.py tests/test_repository_executor.py
git commit -m "feat: verify repository check dependencies"
```

### Task 6: Launch Ordinary Checks with Trusted Start Evidence

**Files:**
- Create: `src/pyrepo_check/_check_launcher.py`
- Create: `src/pyrepo_check/check_launcher.py`
- Create: `tests/test_check_launcher.py`
- Modify: `src/pyrepo_check/repository_executor.py`
- Modify: `tests/test_repository_executor.py`
- Modify: `tests/support.py`

**Interfaces:**
- Produces `StagedCheckLauncher(path: Path, digest: FileDigest)` and
  `CheckStartObservation(check, module, arguments_sha256, python)`.
- Produces `stage_check_launcher(workspace) -> StagedCheckLauncher`,
  `build_launcher_command(prepared, staged, invocation, marker_path) -> tuple[str, ...]`,
  and `validate_start_marker(marker_path, *, workspace, invocation,
  module, prepared) -> CheckStartObservation`.
- `repository_executor.execute_invocation(invocation, *, prepared, workspace,
  launcher, runner, clock_ns) -> RepositoryCheckObservation` is the actual
  one-Check implementation used by orchestration and focused tests; there is no
  private twin or test-only pass-through.
- Every ordinary primary uses the observed Repository Python and the launcher.
- A valid marker proves the launcher reached the module-dispatch boundary; only
  marker plus exit `0` or `1` permits pass/findings classification.
- Produces internal `execute_repository_plan(plan, *, tool_environment=None,
  runner=None, clock_ns) -> RepositoryExecutionResult`; omission observes the Tool
  Environment locally, while Task 8B's CLI supplies its one pre-planning observation.
  The public `execution.execute_plan` still does not delegate to it until Task 8B.
- The internal executor composes Task 4 safe preparation, Task 5 dependency probes,
  one Task 1 workspace/launcher per executable Check, canonical ordinary Check
  execution, final state verification, and cleanup. It retains every observation on
  errors and runs final verification whenever a baseline exists. Workspace setup or
  cleanup failure attaches to that Check as `cleanup_failed`; there is no ownerless
  run-global cleanup outcome.

Reuse `RepositoryCheckObservation` and `RepositoryExecutionResult` from Task 3's
`execution.py`. `check_launcher.py` imports observation types and never
imports `repository_executor.py`. Its own staging shape is:

```python
@dataclass(frozen=True)
class StagedCheckLauncher:
    path: Path
    digest: FileDigest
```

- [ ] **Step 1: Write RED marker-binding and classification tests**

Create `tests/test_check_launcher.py` with exact cases for the five marker fields,
4,096-byte bound, pre-spawn absence, exclusive regular-file creation,
descriptor-relative identity/snapshot checks, duplicate/unknown keys,
argument digest, Check/module mismatch, Repository Python mismatch, path swap,
cleanup, and missing marker:

```python
def test_outer_uv_exit_one_without_marker_is_not_a_ruff_finding(
    tmp_path: Path,
) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))
    runner = RecordingRunner(returncodes=(1,))

    with test_workspace(tmp_path) as workspace:
        launcher = stage_check_launcher(workspace)
        observation = execute_invocation(
            CheckInvocation("ruff", ("check", ".")),
            prepared=prepared,
            workspace=workspace,
            launcher=launcher,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observation.start is None
    assert observation.error is not None
    assert observation.error.code == "check_start_evidence_invalid"


def test_valid_marker_and_exit_one_is_a_completed_finding(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))
    runner = launcher_aware_runner(returncode=1, publish_valid_marker=True)

    with test_workspace(tmp_path) as workspace:
        launcher = stage_check_launcher(workspace)
        observation = execute_invocation(
            CheckInvocation("ty", ("check",)),
            prepared=prepared,
            workspace=workspace,
            launcher=launcher,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observation.start is not None
    assert observation.error is None
    assert observation.processes[0].returncode == 1
```

Parametrize exits `0`, `1`, `2`, `120`, negative signal, spawn failure, workspace
setup failure, and workspace cleanup failure with and without a valid marker. Prove
the same Check retains real primary/start evidence when its cleanup later fails.

Also prove a Ruff dependency error yields one Check observation with no primary,
while selected Ty, Bandit, and pytest Checks with available dependencies still
execute in canonical order. Missing pytest blocks only pytest; missing Coverage is
retained for Task 7's plain-pytest fallback.

`prepared_repository(root, python)` is a complete environment factory.
`test_workspace(root)` is a context manager over the Task 1 workspace seam; it
always closes held descriptors and performs bounded cleanup on exit.
`launcher_aware_runner` extends `RecordingRunner`: when it observes launcher argv,
it reads the `--evidence`, `--check`, and `--module` operands and writes the exact
marker with the expected argument digest and prepared Python only when
`publish_valid_marker=True`.

Run:
`uv run --frozen python -m pytest tests/test_check_launcher.py tests/test_repository_executor.py -k 'marker or outer_uv or finding' -vv`

Expected: FAIL because launcher staging and start evidence do not exist.

- [ ] **Step 2: Implement the standalone launcher**

`_check_launcher.py` must remain import-free from pyrepo-check and valid on Python
3.10. Its operational core is:

```python
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import traceback


def argument_digest(arguments: list[str]) -> str:
    digest = hashlib.sha256()
    for argument in arguments:
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def publish_start(path: Path, check: str, module: str, arguments: list[str]) -> None:
    payload = {
        "schema_version": 1,
        "check": check,
        "module": module,
        "arguments_sha256": argument_digest(arguments),
        "python": {
            "implementation": sys.implementation.name,
            "version": list(sys.version_info[:3]),
            "executable": os.path.abspath(os.path.normpath(sys.executable)),
        },
    }
    content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(content) > 4_096:
        raise RuntimeError("start evidence exceeds 4096 bytes")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("start evidence write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def dispatch(evidence: Path, check: str, module: str, arguments: list[str]) -> int:
    sys.path[0] = os.getcwd()
    sys.argv = [module, *arguments]
    sys.orig_argv = [sys.executable, "-m", module, *arguments]
    publish_start(evidence, check, module, arguments)
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 120
    return 0
```

Add strict argument parsing for
`--evidence PATH --check NAME --module MODULE -- ARGUMENTS`; malformed launcher
syntax exits `120` without a valid marker.

- [ ] **Step 3: Stage and validate the launcher safely**

Copy the packaged launcher into the held run workspace with the existing
digest-validated, exclusive regular-file helper. Generate one unpredictable marker
basename, prove it absent relative to the held workspace descriptor immediately
before spawn, and build this actual process shape:

```text
uv run --locked --python <observed-executable> python <launcher> \
  --evidence <marker> --check <name> --module <module> -- <arguments>
```

After the process, revalidate the workspace identity, open the marker basename
descriptor-relatively with no-follow semantics, require a regular mode-0600 file
owned by the effective user, capture its device/inode, read at most 4,096 bytes,
and prove the same identity/size/metadata still exists before accepting it. A
missing, pre-existing, replaced, or changed marker fails closed. Compute the
expected length-prefixed digest in controller code independently from the
standalone launcher.

- [ ] **Step 4: Prove native `python -m` startup parity**

Run the launcher and direct `python -m` against a temporary probe module under
Repository Python 3.10, 3.11, 3.12, and 3.13 when selected by the matrix. Compare
exact recorded `sys.path[0]`, `sys.argv`, `sys.orig_argv`, and
`__main__.__spec__`. Add both direct pytest arguments and
`coverage run -m pytest` arguments.

- [ ] **Step 5: Execute Ruff, annotations, Ty, and Bandit through the launcher**

Map Check names centrally:

```python
CHECK_MODULE: Mapping[CheckName, str] = {
    "ruff": "ruff",
    "annotations": "ruff",
    "annotations-fix": "ruff",
    "ty": "ty",
    "bandit": "bandit",
    "pytest": "pytest",
}
```

Terminal banners show logical `python -m <module> <arguments>`; JSON keeps the real uv and
launcher argv. Set Analysis Python authority only for Ruff/annotations/Ty with a
valid marker and exit `0` or `1`. Signals and other positive errors retain
Repository Environment attribution but null Analysis Python authority.

- [ ] **Step 6: Verify and commit trusted Check launch**

```bash
uv run --frozen python -m pytest tests/test_check_launcher.py tests/test_repository_executor.py -q
uv run --frozen python -m ruff check src/pyrepo_check/_check_launcher.py src/pyrepo_check/check_launcher.py src/pyrepo_check/repository_executor.py tests/support.py tests/test_check_launcher.py tests/test_repository_executor.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add src/pyrepo_check/_check_launcher.py src/pyrepo_check/check_launcher.py src/pyrepo_check/repository_executor.py tests/support.py tests/test_check_launcher.py tests/test_repository_executor.py
git commit -m "feat: prove repository check dispatch"
```

### Task 7: Run Pytest and Coverage in the Prepared Environment

**Files:**
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `src/pyrepo_check/pytest_evidence.py`
- Modify: `src/pyrepo_check/coverage_execution.py`
- Modify: `src/pyrepo_check/coverage_evidence.py`
- Modify: `src/pyrepo_check/repository_executor.py`
- Modify: `src/pyrepo_check/check_launcher.py`
- Modify: `tests/test_pytest_execution.py`
- Modify: `tests/test_pytest_evidence.py`
- Modify: `tests/test_coverage_execution.py`
- Modify: `tests/test_coverage_evidence.py`
- Modify: `tests/test_repository_executor.py`
- Modify: `tests/test_compatibility.py`

**Interfaces:**
- `execute_prepared_pytest(check, *, plan, prepared, pytest_dependency,
  coverage_dependency, workspace, launcher, output_format, runner, clock_ns) ->
  PreparedPytestExecution` consumes the authoritative environment and dependencies.
  `repository_executor.py` alone wraps that specialized result as a
  `RepositoryCheckObservation`, avoiding a module cycle.
- Keep the controller-era `execute_pytest` adapter and its preflights only until
  Task 8B so the current public path remains green between commits.
- Pytest/Coverage result builders receive repository dependency versions instead
  of inferring them from duplicated preflights.
- Missing Coverage executes pytest exactly once without instrumentation, retains
  real pytest evidence, and produces a typed Coverage error.

Use this specialized return shape in `pytest_execution.py`:

```python
@dataclass(frozen=True)
class PreparedPytestExecution:
    processes: tuple[ExecutedProcess, ...]
    start: CheckStartObservation | None
    error: CheckExecutionFailure | None
    pytest: PytestExecutionObservation | None
    coverage: CoverageExecutionObservation | None
```

`run_prepared_pytest_fixture` is a test-only wrapper in
`test_pytest_execution.py`; it creates one pytest `CheckInvocation`, opens the Task
1 workspace, stages the Task 6 launcher, calls `execute_prepared_pytest` with the
supplied dependencies, and always closes and removes the workspace in `finally`.

- [ ] **Step 1: Write RED prepared-environment execution tests**

Add these contracts:

```python
def test_pytest_uses_prepared_repository_python_and_no_controller_pythonpath(
    tmp_path: Path,
) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 10, 19))
    environment = dict(prepared.child_environment)
    environment["PYTHONPATH"] = "/controller/source"
    runner = launcher_aware_runner(returncode=0, publish_pytest_artifact=True)

    result = run_prepared_pytest_fixture(
        prepared=replace(prepared, child_environment=MappingProxyType(environment)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=None,
        runner=runner,
    )

    primary = next(process for process in result.processes if process.role == "primary")
    assert primary.command[:6] == (
        "uv", "run", "--locked", "--python", str(prepared.python.executable), "python"
    )
    primary_call = next(call for call in runner.calls if call.command == primary.command)
    assert primary_call.env is not None
    assert "/controller/source" not in primary_call.env["PYTHONPATH"]
```

Also prove the only controller-supplied `PYTHONPATH` entry is the invocation-owned
pytest reporter directory and the target repository still cannot import
`pyrepo_check`.

- [ ] **Step 2: Write RED missing-Coverage fallback tests**

```python
def test_missing_coverage_runs_plain_pytest_once_and_keeps_coverage_error(
    tmp_path: Path,
) -> None:
    runner = launcher_aware_runner(returncode=0, publish_pytest_artifact=True)

    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=missing_dependency("coverage"),
        coverage_requested=True,
        runner=runner,
    )

    primaries = [process for process in result.processes if process.role == "primary"]
    assert len(primaries) == 1
    assert "pytest" in primaries[0].command
    assert "coverage" not in primaries[0].command
    assert result.coverage is not None
    assert result.coverage.artifact.state == "not_attempted"
```

Cover missing pytest preventing both pytest and Coverage, Coverage present with
pytest missing, Coverage primary failure, Coverage JSON helper failure, and
independent later-Check continuation.

- [ ] **Step 3: Add the prepared path and inject authoritative versions**

Implement `execute_prepared_pytest` without calling the old Python-floor or
module/version probes. Keep `PytestExecutionObservation` and
`CoverageExecutionObservation`, but construct the prepared-path readiness evidence
from the authoritative dependency observation. Ensure pytest artifact version equals
the dependency version exactly; do the same for Coverage JSON metadata. Leave the
old adapter/preflight code temporarily reachable only from the unchanged public path;
Task 8B deletes it during cutover.

- [ ] **Step 4: Route both pytest primary forms through the launcher**

Plain pytest dispatches module `pytest` with reporter arguments. Instrumented
pytest dispatches module `coverage` with:

```python
(
    "run",
    f"--rcfile={config_path}",
    f"--data-file={workspace.path / '.coverage'}",
    "-m",
    "pytest",
    "-p",
    plugin_module,
    *pytest_args,
)
```

Coverage JSON generation remains a pinned helper process through
`uv run --locked --python <observed-executable> python -m coverage json <arguments>`; it
does not receive primary start evidence. Preserve the single pytest invocation,
data snapshot, digest, shard, threshold-neutralization, and cleanup contracts.

- [ ] **Step 5: Run the complete pytest/Coverage regression surface**

```bash
uv run --frozen python -m pytest tests/test_pytest_execution.py tests/test_pytest_evidence.py tests/test_coverage_execution.py tests/test_coverage_evidence.py tests/test_repository_executor.py tests/test_compatibility.py -q
uv run --frozen python -m ruff check src/pyrepo_check/pytest_execution.py src/pyrepo_check/pytest_evidence.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/coverage_evidence.py src/pyrepo_check/repository_executor.py tests/test_pytest_execution.py tests/test_pytest_evidence.py tests/test_coverage_execution.py tests/test_coverage_evidence.py tests/test_repository_executor.py tests/test_compatibility.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
```

Expected: pass; the prepared Repository Environment path has no Python 3.13.15
floor. The isolated legacy adapter remains covered only until Task 8B's atomic
cutover and deletion.

- [ ] **Step 6: Commit prepared pytest/Coverage execution**

```bash
git add src/pyrepo_check/pytest_execution.py src/pyrepo_check/pytest_evidence.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/coverage_evidence.py src/pyrepo_check/repository_executor.py src/pyrepo_check/check_launcher.py tests/test_pytest_execution.py tests/test_pytest_evidence.py tests/test_coverage_execution.py tests/test_coverage_evidence.py tests/test_repository_executor.py tests/test_compatibility.py
git commit -m "refactor: run test evidence in repository environment"
```

### Task 8A: Build Schema v2 Internally Without Public Cutover

**Files:**
- Create: `src/pyrepo_check/reporting_schema.py`
- Create: `tests/test_reporting_schema_v2.py`
- Modify: `src/pyrepo_check/reporting.py`
- Modify: `tests/test_reporting.py`

**Interfaces:**
- Defines `PlanningErrorReportV2`, `RunReportV2`, and `AgentReportV2` internally;
  the CLI continues emitting schema v1 throughout this task.
- Moves the exact retained Selection, ProcessResult, Advisory, captured-output, and
  shared enum definitions from `reporting.py` into `reporting_schema.py`; that module
  imports `PytestResult` and `CoverageResult` from their evidence modules and never
  imports `reporting.py`. `reporting.py` imports and re-exports retained names, then
  supplies v2 cross-field validation without defining a second model copy. Public
  v2 composition and rendering wait for Task 8B.
- Moves `ReportingError` into `reporting_schema.py`. Its dependency-free
  `validate_report_structure_v2` checks field types/enums/order-local nullability;
  `reporting.validate_report_v2` calls it, then enforces cross-field execution
  invariants. Both names are re-exported from `reporting.py` for callers/tests.
- Adds internal `validate_report_v2(report) -> None` without changing
  `serialize_json`, `render_terminal`, CLI exits, or any v1 public builder. Task 8B
  performs that public replacement atomically.

- [ ] **Step 1: Write RED exact schema-v2 planning-error test**

```python
def test_schema_v2_planning_error_contains_tool_environment() -> None:
    report = PlanningErrorReportV2(
        schema_version=2,
        kind="planning_error",
        overall_status="error",
        complete=False,
        tool_environment=tool_environment_evidence(python=(3, 13, 15)),
        repository_environment=None,
        error=PlanningErrorV2(
            code="unsafe_unlocked_execution",
            message="--no-frozen is incompatible with repository-safe execution.",
            hint="Update uv.lock explicitly, then rerun without --no-frozen.",
        ),
    )

    validate_report_v2(report)
    payload = asdict(report)
    assert tuple(payload) == (
        "schema_version",
        "kind",
        "overall_status",
        "complete",
        "tool_environment",
        "repository_environment",
        "error",
    )
    assert payload["schema_version"] == 2
    assert payload["repository_environment"] is None
    assert payload["tool_environment"]["python"]["version"] == (3, 13, 15)
```

Build Tool Environment evidence from an injected observation in tests rather than
depending on the host patch version. JSON serialization is tested only in Task 8B,
when v2 becomes public.

- [ ] **Step 2: Add the exact v2 dataclasses and structural validator**

Implement the spec's fields in this order:

```python
@dataclass(frozen=True)
class PythonEvidence:
    implementation: str
    version: tuple[int, int, int]
    executable: str


@dataclass(frozen=True)
class ToolEnvironmentEvidence:
    pyrepo_check_version: str
    python: PythonEvidence


@dataclass(frozen=True)
class RepositoryPythonSelectionEvidence:
    kind: Literal["default", "explicit"]
    request: str | None


@dataclass(frozen=True)
class LockEvidence:
    path: str
    status: Literal["current", "missing", "unverified"]


@dataclass(frozen=True)
class EnvironmentError:
    code: EnvironmentErrorCode
    message: str
    hint: str | None


@dataclass(frozen=True)
class CheckErrorV2:
    code: CheckErrorCodeV2
    message: str
    hint: str | None


@dataclass(frozen=True)
class DependencyEvidence:
    name: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    module: str
    required: str
    status: DependencyStatus
    version: str | None
    origin: str | None
    process: ProcessResult | None
    error: CheckErrorV2 | None


@dataclass(frozen=True)
class RepositoryEnvironmentEvidence:
    manager: Literal["uv"]
    manager_version: str | None
    path: str | None
    python_selection: RepositoryPythonSelectionEvidence
    python: PythonEvidence | None
    lock: LockEvidence
    dependency_selection: Literal["default"]
    mutation_protection: MutationProtection
    dependencies: tuple[DependencyEvidence, ...]
    processes: tuple[ProcessResult, ...]
    error: EnvironmentError | None


@dataclass(frozen=True)
class AnalysisPythonAuthorityEvidence:
    authority: Literal["repository_tool"]
    pyrepo_check_override: None


@dataclass(frozen=True)
class CheckStartEvidence:
    schema_version: Literal[1]
    check: CheckName
    module: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    arguments_sha256: str
    python: PythonEvidence


@dataclass(frozen=True)
class CheckResultV2:
    name: CheckName
    status: CheckStatus
    execution_environment: Literal["repository"] | None
    analysis_python_authority: AnalysisPythonAuthorityEvidence | None
    start_evidence: CheckStartEvidence | None
    processes: tuple[ProcessResult, ...]
    error: CheckErrorV2 | None


@dataclass(frozen=True)
class PlanningErrorReportV2:
    schema_version: Literal[2]
    kind: Literal["planning_error"]
    overall_status: Literal["error"]
    complete: Literal[False]
    tool_environment: ToolEnvironmentEvidence
    repository_environment: None
    error: PlanningErrorV2


@dataclass(frozen=True)
class RunReportV2:
    schema_version: Literal[2]
    kind: Literal["run"]
    project_root: str
    mode: RunMode
    overall_status: OverallStatus
    complete: bool
    tool_environment: ToolEnvironmentEvidence
    repository_environment: RepositoryEnvironmentEvidence
    selection: Selection
    checks: tuple[CheckResultV2, ...]
    pytest: PytestResult | None
    coverage: CoverageResult | None
    advisories: tuple[Advisory, ...]
```

`PlanningErrorV2` contains `code`, `message`, and nullable `hint` in that order.
Use the complete planning, Check, and environment error-code literals from the
approved spec. Dataclass field order is the future serialization order; Task 8B adds
the public serializer and proves the one-document-plus-newline byte contract. Extend
`ProcessRole` with `repository_safety`, `uv_version`, `environment_probe`, and
`dependency_probe`.

- [ ] **Step 3: Write RED environment/check invariant mutation tests**

Starting from one valid builder report, use `dataclasses.replace` to mutate every
required field, enum, nullability pair, process role, dependency order, start
digest, selection match, lock state, and completion/status relation. At minimum
assert these concrete contradictions:

```python
def test_schema_v2_rejects_repository_attribution_without_start(
    valid_report: RunReportV2,
) -> None:
    check = replace(
        valid_report.checks[0],
        execution_environment="repository",
        start_evidence=None,
    )
    malformed = replace(valid_report, checks=(check, *valid_report.checks[1:]))

    with pytest.raises(ReportingError):
        validate_report_v2(malformed)


def test_schema_v2_rejects_current_lock_without_successful_probe(
    valid_report: RunReportV2,
) -> None:
    environment = valid_report.repository_environment
    malformed_environment = replace(
        environment,
        lock=replace(environment.lock, status="current"),
        processes=tuple(
            process
            for process in environment.processes
            if process.role != "environment_probe"
        ),
    )

    with pytest.raises(ReportingError):
        validate_report_v2(
            replace(valid_report, repository_environment=malformed_environment)
        )
```

- [ ] **Step 4: Verify and commit the internal schema**

```bash
uv run --frozen python -m pytest tests/test_reporting_schema_v2.py tests/test_reporting.py -q
uv run --frozen python -m ruff check src/pyrepo_check/reporting_schema.py src/pyrepo_check/reporting.py tests/test_reporting_schema_v2.py tests/test_reporting.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add src/pyrepo_check/reporting_schema.py src/pyrepo_check/reporting.py tests/test_reporting_schema_v2.py tests/test_reporting.py
git commit -m "refactor: define agent report schema v2"
```

Expected: the existing CLI still emits schema version 1 after this commit; all v2
models and validators are exercised only through internal tests.

### Task 8B: Cut Over Atomically to Repository Execution and Schema v2

**Files:**
- Modify: `src/pyrepo_check/execution.py`
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `src/pyrepo_check/coverage_execution.py`
- Modify: `src/pyrepo_check/reporting_schema.py`
- Modify: `src/pyrepo_check/reporting.py`
- Modify: `src/pyrepo_check/cli.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_pytest_execution.py`
- Modify: `tests/test_coverage_execution.py`
- Modify: `tests/test_reporting_schema_v2.py`
- Modify: `tests/test_reporting.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_repository_executor.py`

**Interfaces:**
- `serialize_json(report) -> bytes`, `render_terminal(report) -> str`, and
  `select_exit_code(report) -> Literal[0, 1, 2]` become the sole public projection
  path for `AgentReportV2`. Serialization follows dataclass field order and emits
  exactly one UTF-8 JSON document plus one newline.
- Removes all V1 aliases, validators, payload builders, and tests.
- Atomically changes `execution.execute_plan` to delegate to the complete internal
  Repository Executor and re-exports `RepositoryExecutionResult` as
  `ExecutionResult`. The isolated raw-command compatibility helper is unchanged.

- [ ] **Step 1: Write RED public cutover tests**

Add CLI tests for one planning error and one scripted successful focused Ty run.
Before implementation, both must fail these exact assertions:

```python
assert planning_payload["schema_version"] == 2
assert planning_payload["tool_environment"]["python"]["version"] == [3, 13, 15]
assert planning_payload["repository_environment"] is None

assert run_payload["schema_version"] == 2
assert run_payload["repository_environment"]["lock"]["status"] == "current"
assert run_payload["checks"][0]["execution_environment"] == "repository"
```

Inject Tool Environment and scripted Repository Environment evidence. Assert the
old public `execution.execute_plan` and schema-v1 serializer are reached before the
GREEN change, making the failure attributable to the missing atomic cutover.

Run:
`uv run --frozen python -m pytest tests/test_cli.py tests/test_execution.py -k 'schema_v2 or repository_environment' -vv`

Expected: FAIL on schema/public-executor assertions.

- [ ] **Step 2: Project observations and make the atomic public cutover**

Map Tool and Repository Environment observations without reopening the environment.
Environment-wide failure accounts for every selected Check with
`repository_environment_unavailable`; dependency failures retain their exact local
error; ordinary pass/findings require valid marker plus exit `0`/`1`; pytest and
Coverage retain structured specialized evidence. Preserve post-execution Check
results when `repository_state_changed` makes the overall report incomplete/error.
In the same GREEN change, make the CLI capture Tool Environment once before
planning, pass it to `build_planning_error_report` or `execution.execute_plan`, make
`execution.execute_plan` delegate to `execute_repository_plan`, adapt run-report
construction, and update the preserved
CLI path. Delete the controller-era pytest/Coverage Python-floor and dependency
preflights, their classifiers, Task 2's temporary command expansion, and obsolete
tests. Add tests proving no public path uses those temporary adapters after cutover
and no half-built Repository Executor was exposed earlier.

- [ ] **Step 3: Implement terminal evidence and stable public exits**

After valid preparation, terminal mode emits exactly:

```text
==> environment: tool Python 3.13.15 -> repository Python 3.12.11 (uv, locked)
```

Successful dependency details stay hidden. Dependency errors include Check,
required range, installed version/origin when known, and remediation. JSON stdout
contains preparation/Check streams only inside the report. `select_exit_code`
returns `2` for any error, else `1` for completed findings, else `0`. Change CLI's
reporting fallback to `2`; delete first-positive fallback behavior.

- [ ] **Step 4: Remove v1 and prove exact public output**

Delete `PlanningErrorReportV1`, `RunReportV1`, `AgentReportV1`,
`validate_report_v1`, and every v1-only validator/payload test. Add exact JSON
snapshots for planning error, missing lock, dependency error plus independent
failure, successful focused Ty, successful strict aggregate, pytest without
Coverage dependency, and repository state change.

- [ ] **Step 5: Verify and commit the public cutover**

```bash
uv run --frozen python -m pytest tests/test_reporting_schema_v2.py tests/test_reporting.py tests/test_cli.py tests/test_execution.py tests/test_pytest_execution.py tests/test_coverage_execution.py tests/test_repository_executor.py -q
uv run --frozen python -m ruff check src/pyrepo_check/reporting_schema.py src/pyrepo_check/reporting.py src/pyrepo_check/execution.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/cli.py tests/test_reporting_schema_v2.py tests/test_reporting.py tests/test_cli.py tests/test_execution.py tests/test_pytest_execution.py tests/test_coverage_execution.py tests/test_repository_executor.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add src/pyrepo_check/reporting_schema.py src/pyrepo_check/reporting.py src/pyrepo_check/execution.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/cli.py tests/test_reporting_schema_v2.py tests/test_reporting.py tests/test_cli.py tests/test_execution.py tests/test_pytest_execution.py tests/test_coverage_execution.py tests/test_repository_executor.py
git commit -m "feat: report repository environment evidence"
```

### Task 9: Prove Real uv Boundaries and the Python Matrix

**Files:**
- Create: `tests/test_repository_integration.py`
- Create: `tests/test_repository_python_matrix.py`
- Create: `.github/workflows/repository-python-matrix.yml`
- Modify: `tests/support.py`
- Modify: `tests/test_compatibility.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Fast scripted tests remain deterministic and runner-driven.
- Real uv fixtures create their own target `pyproject.toml`, `uv.lock`, Git
  repository, and ignored `.venv`; they never install pyrepo-check into the target.
- `PYREPO_CHECK_REPOSITORY_PYTHON` selects exactly one matrix case per CI job.
- CI pins uv `0.10.12`; normal runtime behavior remains capability-based.

- [ ] **Step 1: Add the real target-repository fixture helper**

In `tests/support.py`, add a helper that writes this complete fixture policy:

```toml
[project]
name = "repository-environment-fixture"
version = "0.0.0"
requires-python = ">=3.10,<3.14"
dependencies = []

[dependency-groups]
dev = [
    "bandit>=1.9,<2",
    "coverage[toml]>=7.15,<8",
    "pytest>=8,<9",
    "ruff>=0.15,<1",
    "ty>=0.0.35,<0.1",
]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.coverage.run]
branch = true
source = ["src/fixture_package"]
parallel = false

[tool.coverage.report]
fail_under = 100

[tool.ruff]
target-version = "py310"

[tool.ty.environment]
python-version = "3.10"

[tool.bandit]
exclude_dirs = [".venv", "tests"]
```

Write a fully annotated package plus tests that assert Repository Python evidence,
native module startup state, controller `PYTHONPATH` absence, and
`importlib.util.find_spec("pyrepo_check") is None`. Write `.gitignore` with
`.venv/`, lock with `uv lock --python <selected>`, initialize Git, add all fixture
files, and commit them before invoking pyrepo-check.

- [ ] **Step 2: Write RED real uv preparation/integrity cases**

`tests/test_repository_integration.py` covers, under the host's selected 3.13
Repository Python:

- missing `.venv` rebuilt from a current lock;
- the rebuilt ordinary uv `.venv` is accepted when its reported executable is
  lexically under `.venv` but the executable symlink resolves to uv's external
  managed-Python installation;
- absent lock short-circuits before uv;
- changed `pyproject.toml` after locking yields `repository_environment_failed`,
  `lock.status == "unverified"`, bounded uv diagnostics, and no Check start;
- inherited controller Python/uv selection variables do not change selection;
- each preserved uv storage variable and each HOME/XDG-derived storage base is
  rejected with zero uv/Check processes when its lexical or resolved path enters the
  repository, including an external symlink into the repository;
- default repository groups supply all Check Dependencies;
- `.venv` tracked, unignored, or symlinked is rejected;
- a real committed Git fixture run with a child `PATH` exposing the resolved uv
  executable but no `git` executable reports `unsafe_repository_environment`,
  retains bounded `repository_safety` spawn-failure evidence, and starts neither uv
  preparation nor a Check;
- a real Git fixture with deliberately created nonzero index stages is rejected as
  unsafe before `uv --version` or any Check start;
- initially dirty tracked bytes remain acceptable when unchanged;
- a test that mutates a tracked file yields `repository_state_changed`;
- an allowed genuinely missing interpreter download uses invocation-owned temporary
  `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_CACHE_DIR`, `UV_PYTHON_BIN_DIR`, and
  `UV_CACHE_DIR` outside the repository, creates no tracked bytes, and reports the
  downloaded exact Python;
- the same isolated acquisition setup with `UV_PYTHON_DOWNLOADS=never` fails during
  locked preparation with bounded uv diagnostics and no Check start;
- missing Ruff does not suppress Ty, Bandit, or pytest;
- missing Coverage runs one plain pytest and leaves Coverage/overall in error;
- an outer uv proxy that returns exactly `1` after real preparation but before
  launcher dispatch has no marker and is an execution error, never a Ruff finding;
- proxy modes that append, truncate, or contradict real environment/dependency probe
  stdout are rejected as typed evidence errors with bounded raw process evidence;
- repository-controlled pytest fixtures that replace, truncate, or contradict their
  artifact/start evidence are rejected while their real process output is retained;
- no target process can import pyrepo-check; and
- valid start markers exist for all executed primaries.

Make the acquisition cases independent of whatever Python happens to be installed on
the developer machine. `missing_download_candidate` tries the pinned-uv advertised
exact requests `("3.12.12", "3.12.11", "3.12.10")` in order under one fresh
external storage layout and a fixture `PATH` containing only the resolved real uv
executable. For each candidate it first runs real
`uv python list --all-versions <candidate>` and requires a matching
`<download available>` entry, then runs real `uv python find <candidate>` with
`UV_PYTHON_DOWNLOADS=never`. Select only a candidate whose find exits nonzero; fail
the test with the precondition evidence if no candidate qualifies—never skip or
pretend acquisition was exercised. Run the allowed and forbidden pyrepo-check cases
with separate fresh external storage layouts but the same exact selected request.
The allowed case must show a new managed installation under its invocation-owned
install directory; the forbidden case must leave its directory empty, fail during
preparation, and show no Check start. This prevents an existing host `python3.13`
or another discovered interpreter from satisfying either case accidentally.

For the outer-exit case, prepend a fixture-owned `uv` proxy to `PATH`. It delegates
`--version`, preparation, and dependency probes to the resolved real uv binary, but
returns exactly `1` without delegation when argv contains the staged launcher. This
keeps real uv environment setup while proving that an outer pre-launch exit cannot
be misclassified as a tool finding. The proxy path and real uv path are both confined
to the temporary fixture and never replace the user's executable.

Create the Git-unavailable fixture with real Git first, then execute pyrepo-check
with a fresh `bin/` containing only a symlink to the already-resolved real uv binary.
Invoke the controller by absolute `sys.executable`, so removing Git from child
`PATH` cannot remove the controller. Create the unmerged fixture without a merge
race: write three fixture blobs with `git hash-object -w --stdin`, then feed explicit
stage 1/2/3 `100644 <object> <stage>\tconflict.py` records to
`git update-index --index-info`. Assert the pre-run index really has nonzero stages
before invoking pyrepo-check, and compare the recorded process-role sequence to prove
both cases stop before `uv_version`.

Run:
`uv run --frozen python -m pytest tests/test_repository_integration.py -q`

Expected before fixture support is complete: FAIL on the first repository-
environment assertion, never a false pass.

- [ ] **Step 3: Add one explicit matrix test**

Implement this outer contract in `tests/test_repository_python_matrix.py`:

```python
def test_global_controller_runs_complete_gate_on_selected_repository_python(
    tmp_path: Path,
) -> None:
    request = os.environ["PYREPO_CHECK_REPOSITORY_PYTHON"]
    repository = write_locked_repository_fixture(tmp_path, python=request)

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pyrepo_check.cli",
            "--root",
            str(repository),
            "--python",
            request,
            "--format",
            "json",
            "--all",
        ),
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    report = json.loads(completed.stdout)
    assert report["schema_version"] == 2
    assert report["tool_environment"]["python"]["version"] == [3, 13, 15]
    assert report["repository_environment"]["python"]["version"][:2] == [
        int(piece) for piece in request.split(".")
    ][:2]
    assert report["repository_environment"]["lock"]["status"] == "current"
    assert report["repository_environment"]["mutation_protection"] == "tracked_files"
    assert all(
        check["execution_environment"] == "repository"
        for check in report["checks"]
    )
```

The test additionally asserts all five dependency records are available, Ruff/Ty
Analysis Python authority is repository-owned, pytest/Coverage evidence is complete,
and fixture tracked bytes are unchanged.

- [ ] **Step 4: Add one controller gate plus the pinned four-job matrix**

Create `.github/workflows/repository-python-matrix.yml`:

Both `uses:` references below are immutable full commit SHAs resolved from the
official repositories; the comments retain their reviewed major tags. The
`setup-uv` `version` input separately pins the installed uv binary to `0.10.12`.

```yaml
name: repository-python-matrix

on:
  pull_request:
  push:
    branches: [main]

jobs:
  controller-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
      - uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78 # v7
        with:
          version: "0.10.12"
          enable-cache: true
      - name: Install controller Python
        run: uv python install 3.13.15
      - name: Synchronize controller environment
        run: uv sync --locked --python 3.13.15
      - name: Run strict gate including real uv integration
        run: uv run --locked --python 3.13.15 pyrepo-check --all

  repository-python:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        repository-python: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
      - uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78 # v7
        with:
          version: "0.10.12"
          enable-cache: true
      - name: Install controller and repository Pythons
        run: uv python install 3.13.15 ${{ matrix.repository-python }}
      - name: Synchronize controller environment
        run: uv sync --locked --python 3.13.15
      - name: Verify one Repository Python
        env:
          PYREPO_CHECK_REPOSITORY_PYTHON: ${{ matrix.repository-python }}
          UV_PYTHON_DOWNLOADS: never
        run: >-
          uv run --locked --python 3.13.15 python -m pytest
          tests/test_repository_python_matrix.py -q
```

- [ ] **Step 5: Register the integration marker without hiding default proof**

Add `integration` to pytest markers only for diagnostics and selection. Do not add
default `-m 'not integration'`; the repository strict gate must still execute the
host-3.13 real uv tests. The explicit 3.10-3.13 matrix file may skip only when
`PYREPO_CHECK_REPOSITORY_PYTHON` is absent, and every CI matrix job sets it.

- [ ] **Step 6: Run all four local integration proofs and commit**

```bash
uv run --frozen python -m pytest tests/test_repository_integration.py -q
uv python install 3.10 3.11 3.12 3.13
PYREPO_CHECK_REPOSITORY_PYTHON=3.10 UV_PYTHON_DOWNLOADS=never uv run --frozen python -m pytest tests/test_repository_python_matrix.py -q
PYREPO_CHECK_REPOSITORY_PYTHON=3.11 UV_PYTHON_DOWNLOADS=never uv run --frozen python -m pytest tests/test_repository_python_matrix.py -q
PYREPO_CHECK_REPOSITORY_PYTHON=3.12 UV_PYTHON_DOWNLOADS=never uv run --frozen python -m pytest tests/test_repository_python_matrix.py -q
PYREPO_CHECK_REPOSITORY_PYTHON=3.13 UV_PYTHON_DOWNLOADS=never uv run --frozen python -m pytest tests/test_repository_python_matrix.py -q
uv run --frozen python -m ruff check tests/support.py tests/test_repository_integration.py tests/test_repository_python_matrix.py
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
git add tests/support.py tests/test_compatibility.py tests/test_repository_integration.py tests/test_repository_python_matrix.py .github/workflows/repository-python-matrix.yml pyproject.toml
git commit -m "test: prove repository Python boundaries"
```

### Task 10: Publish Usage, Skill, Schema, and Completion Evidence

**Files:**
- Create: `docs/reference/agent-report-schema-v2.md`
- Modify: `README.md`
- Modify: `AGENT_PROMPT.md`
- Modify: `.agents/skills/pyrepo-check/SKILL.md`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Modify: `docs/superpowers/specs/2026-08-26-tool-repository-environments-design.md`
- Modify: `tests/test_compatibility.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- README and Agent Skill teach install-once controller use, repository-owned uv
  dependencies, default Repository Python selection, explicit `--python`, focused
  commands, JSON schema v2, and strict final verification.
- CLI help is the command source of truth.
- Personal Skill installations remain untouched.

- [ ] **Step 1: Write RED documentation-contract tests**

Add assertions that README, Agent Skill, schema reference, and help all contain:

```text
pyrepo-check --all
pyrepo-check --python 3.12 --all
pyrepo-check --format json --all
Repository Environment
schema version 2
```

Assert the repository Skill no longer says schema version 1 or first-positive child
exit, and asserts that missing/incompatible repository Check Dependencies are fixed
in the target repository rather than installed automatically by the agent.

Run:
`uv run --frozen python -m pytest tests/test_cli.py tests/test_compatibility.py -k 'help or documentation or skill or schema' -vv`

Expected: FAIL until the public docs are synchronized.

- [ ] **Step 2: Publish the human usage contract**

Update README and `AGENT_PROMPT.md` with these canonical examples:

```bash
# Repository-native default Python and locked default dependencies
pyrepo-check --all

# One explicit Repository Python for a CI job
pyrepo-check --python 3.12 --all

# Focused typing under the same Repository Environment
pyrepo-check --python 3.12 annotations ty src/

# Complete Environment Evidence for agents
pyrepo-check --python 3.12 --format json --all
```

Explain that global installation controls only the controller; the target must own a
current `uv.lock` and compatible Ruff, Ty, Bandit, pytest, and requested Coverage
dependencies in uv's default selection. Explain safe `.venv` reconstruction and the
`--no-frozen` rejection.

- [ ] **Step 3: Publish the schema-v2 reference**

`docs/reference/agent-report-schema-v2.md` contains the complete discriminated
planning/run shapes, every enum/error code, field order, nullability and invariant,
one planning error, one missing-lock run, one dependency-error continuation run,
and one successful strict aggregate example. Copy the numeric version ranges and
Environment Evidence definitions exactly from the approved design.

- [ ] **Step 4: Update the repository Agent Skill**

Teach agents to:

- run focused checks during editing and `--all` before completion;
- use `--python` only when CI or the user selects one Repository Python;
- read `tool_environment`, `repository_environment`, dependency states,
  `execution_environment`, `analysis_python_authority`, pytest, and Coverage;
- fix missing/out-of-range dependencies in the repository lock/config with user
  authority, never by asking pyrepo-check to inject them; and
- treat `scope=partial`, `status=guidance`, and `gate_eligible=false` as guidance,
  not a complete coverage gate.

Do not copy this Skill to `~/.agents`, `~/.codex`, or Antigravity.

- [ ] **Step 5: Close the design/status trail**

Change the older design's successor note to implemented and superseded for the
listed contracts. Change the new design status to “Implemented and verified” only
after all fast, integration, matrix, and strict gates have actual results. Record
the final test count, coverage result, and matrix job evidence; do not reuse the
baseline numbers as completion evidence. Report the final commit after the
documentation commit exists.

- [ ] **Step 6: Run final independent reviews**

Use `superpowers:requesting-code-review` for two fresh reviews:

1. correctness/security: environment isolation, evidence truthfulness, mutation
   safety, start-marker classification, and schema invariants;
2. architecture/lean: intent-only planning, Repository Executor depth, no duplicate
   preflights/workspace logic, and no unnecessary abstraction/dependency.

Resolve every actionable finding and rerun affected focused tests before the final
gate.

- [ ] **Step 7: Run the final repository gate**

```bash
uv run --frozen python -m pytest -q
uv run --frozen pyrepo-check --all
uv run --frozen pyrepo-check --format json --all
git diff --check
```

Parse the final JSON and assert schema version 2, current lock, exact Tool and
Repository Python, all required dependencies available, all executed Checks
attributed to `repository`, complete pytest/Coverage evidence, and no environment
error. Record the fresh test count and Coverage percentage.

- [ ] **Step 8: Commit documentation and completion evidence**

```bash
git add README.md AGENT_PROMPT.md .agents/skills/pyrepo-check/SKILL.md docs/reference/agent-report-schema-v2.md docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md docs/superpowers/specs/2026-08-26-tool-repository-environments-design.md tests/test_compatibility.py tests/test_cli.py
git commit -m "docs: publish repository environment workflow"
```

After this commit, report branch, HEAD, clean/dirty status, exact verification, and
review verdicts. Merge, push, release, worktree cleanup, and personal Skill
deployment remain separately approved actions.
