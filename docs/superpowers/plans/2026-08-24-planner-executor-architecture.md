# Planner and Executor Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the existing run-selection policy and subprocess behavior into
pure planning and isolated execution boundaries without changing any observable
CLI behavior.

**Architecture:** `planning.py` will convert immutable user intent plus loaded
project facts into an ordered `RunPlan`. `execution.py` will execute that plan
with the existing banner, continuation, and exit-code behavior. `cli.py` will
remain responsible only for argparse, configuration loading, planning-error
presentation, execution dispatch, and returning the executor's exit code;
`runner.py` will remain as a compatibility facade for existing Python imports.

**Tech Stack:** Python 3.11+, stdlib `argparse`, `dataclasses`, `pathlib`,
`subprocess`, and `typing`; pytest 8; Ruff; ty; Bandit; uv.

**Spec:**
[`docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`](../specs/2026-08-24-agent-guidance-reporting-design.md)

## Global Constraints

- This plan implements **Milestone A only**: behavior-preserving planning and
  execution extraction.
- Do not add `--format`, `--shortcut`, `--coverage`, Agent Report, JSON,
  Coverage.py, or pytest-plugin behavior in this milestone.
- Preserve these check names exactly: `ruff`, `annotations`,
  `annotations-fix`, `ty`, `bandit`, and `pytest`.
- Preserve target-free no-argument and `--all` behavior as the strict aggregate
  Ruff → annotations → ty → Bandit → pytest run.
- Preserve target-only behavior as Ruff → annotations → ty → Bandit, without
  pytest.
- Preserve direct pytest files and node IDs exactly as supplied.
- Preserve `annotations-fix` as explicit-only and exclude it from aggregate
  runs.
- Preserve existing `uv run`, `--frozen`, target precedence, command argv,
  working directory, banner text, and `check=False` subprocess invocation.
- Preserve ordinary nonzero continuation and return the first raw nonzero code,
  including a negative signal-style return code.
- Preserve current spawn behavior: print the command banner, propagate the
  original exception unchanged, and abort without running later checks.
- Keep execution outside the CLI's planning-error `try` block so a runner-raised
  `ValueError` is not converted into CLI exit code `2`.
- Treat the console command and `pyrepo_check.cli.main(argv, *, runner=...)` as
  supported interfaces. Keep legacy `pyrepo_check.runner` imports working
  through a compatibility facade.
- `planning.py` and `execution.py` are internal modules. Do not promise their
  Python APIs as public package compatibility.
- Add no runtime or development dependency.
- Do not include or modify the unrelated untracked `LICENSE` file.
- Run the strict gate after every implementation commit.

## Baseline Status

| State | Evidence |
| --- | --- |
| Implemented | Existing CLI in `cli.py`, project facts in `config.py`, and combined policy/execution in `runner.py`. |
| Verified | `uv run --frozen pyrepo-check --all` passes Ruff, annotations, ty, Bandit, and 26 tests at baseline commit `ced7e62`. |
| Designed only | `planning.py`, `execution.py`, `RunRequest`, `RunPlan`, JSON reporting, Test Shortcuts, and coverage guidance. |
| In this plan | Only `planning.py`, `execution.py`, thin CLI wiring, compatibility facade, and their tests. |

## Plan Publication Gate

Commit this plan and the corrected design-status wording before starting Task
1. That documentation commit records only approved design and planned work; it
must not be presented as runtime implementation. Tasks 1–5 begin from that
committed planning baseline.

---

### Task 1: Characterize Uncovered Compatibility Behavior

**Files:**

- Create: `tests/__init__.py`
- Create: `tests/support.py`
- Create: `tests/test_compatibility.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_runner.py`

**Interfaces:**

- Consumes: current `pyrepo_check.cli.main` and `pyrepo_check.runner` behavior.
- Produces: one reusable `RecordingRunner` and black-box tests that remain valid
  after internal modules move.

- [ ] **Step 1: Add one recording subprocess adapter for tests**

Create `tests/__init__.py` as an empty file. Create `tests/support.py` with this
test-only adapter:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import subprocess  # nosec B404


@dataclass(frozen=True)
class RecordedCall:
    command: tuple[str, ...]
    cwd: Path
    check: bool


class RecordingRunner:
    def __init__(
        self,
        *,
        returncodes: tuple[int, ...] = (),
        raise_on_call: int | None = None,
        exception: Exception | None = None,
        on_call: Callable[[RecordedCall], None] | None = None,
    ) -> None:
        self.returncodes = returncodes
        self.raise_on_call = raise_on_call
        self.exception = exception
        self.on_call = on_call
        self.calls: list[RecordedCall] = []

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        recorded = RecordedCall(command=command, cwd=cwd, check=check)
        self.calls.append(recorded)
        if self.on_call is not None:
            self.on_call(recorded)
        call_number = len(self.calls)
        if self.raise_on_call == call_number:
            if self.exception is None:
                raise FileNotFoundError(command[0])
            raise self.exception

        returncode_index = call_number - 1
        returncode = (
            self.returncodes[returncode_index]
            if returncode_index < len(self.returncodes)
            else 0
        )
        return subprocess.CompletedProcess(command, returncode=returncode)
```

- [ ] **Step 2: Add black-box compatibility tests before moving code**

Create `tests/test_compatibility.py`. Use `RecordingRunner` to add these exact
tests:

```python
from pathlib import Path

import pytest

from pyrepo_check.cli import main
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
```

Also assert the current banner contract with a focused `ruff` run:

```python
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
```

This pins print/flush ordering on both success and exception paths.

- [ ] **Step 3: Pin the current help surface**

Add a test using `monkeypatch.setattr(sys, "argv", ["pyrepo-check"])`,
`parse_args(["--help"])`, `pytest.raises(SystemExit)`, and `capsys`. Pinning
`sys.argv[0]` makes argparse render the installed console-script name even
when the test itself runs through `python -m pytest`. Assert exit code `0` and
this complete stdout value:

```text
usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen] [checks ...]

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
```

Also assert stderr is empty. This exact snapshot prevents Milestone B/C flags
from leaking into Milestone A.

- [ ] **Step 4: Run the new characterization tests**

Run:

```bash
uv run --frozen python -m pytest tests/test_compatibility.py -q
```

Expected: all characterization tests pass against the existing implementation.
These are GREEN baseline tests, not proof that the extraction is implemented.

- [ ] **Step 5: Run the existing suite and strict gate**

Run:

```bash
uv run --frozen python -m pytest -q
uv run --frozen pyrepo-check --all
```

Expected: the existing 26 tests plus the new compatibility tests pass; Ruff,
annotations, ty, Bandit, and pytest all pass.

- [ ] **Step 6: Commit the characterization boundary**

```bash
git add tests/__init__.py tests/support.py tests/test_compatibility.py
git commit -m "test: characterize CLI execution compatibility"
```

---

### Task 2: Extract Pure Run Planning

**Files:**

- Create: `src/pyrepo_check/planning.py`
- Create: `tests/test_planning.py`
- Modify: `src/pyrepo_check/config.py`
- Modify: `tests/test_config.py`
- Test: `tests/test_runner.py`

**Interfaces:**

- Consumes: `ProjectConfig`, raw positional tokens, `--all`, `--root`, and
  `--no-frozen` intent.
- Produces:
  `RunRequest`, `PlanningFacts`, `PlannedCheck`, `RunPlan`,
  `collect_existing_positionals(...)`, `build_checks(...)`,
  `select_check_names(...)`, `select_checks(...)`, and `plan_run(...)`.

- [ ] **Step 1: Write the failing planner matrix**

Create `tests/test_planning.py`. Import the not-yet-created planning types so
the first run fails during collection:

```python
from pathlib import Path

import pytest

from pyrepo_check.config import ProjectConfig
from pyrepo_check.planning import (
    PlannedCheck,
    PlanningFacts,
    RunPlan,
    RunRequest,
    plan_run,
)


def make_config(
    root: Path,
    *,
    ruff_targets: tuple[str, ...] = ("src", "tests"),
    bandit_targets: tuple[str, ...] = ("src",),
    frozen: bool = False,
) -> ProjectConfig:
    return ProjectConfig(
        root=root,
        ruff_targets=ruff_targets,
        bandit_targets=bandit_targets,
        frozen=frozen,
    )


def command_names(plan: RunPlan) -> tuple[str, ...]:
    return tuple(check.name for check in plan.checks)
```

Add table-driven cases with these exact expectations:

| Request | Existing positional facts | Expected mode | Expected checks |
| --- | --- | --- | --- |
| no positionals | none | `strict_aggregate` | Ruff, annotations, ty, Bandit, pytest |
| target-free `--all` | none | `strict_aggregate` | Ruff, annotations, ty, Bandit, pytest |
| `ty` | none | `focused` | ty only |
| `bandit ruff ruff` | none | `focused` | Ruff then Bandit, once each |
| existing `api.py` only | `api.py` | `focused` | Ruff, annotations, ty, Bandit |
| `--all api.py` | `api.py` | `focused` | Ruff, annotations, ty, Bandit, pytest |
| `annotations-fix` | none | `focused` | annotations-fix only |
| `pytest tests/test_cli.py::test_name` | none | `focused` | pytest only, node ID unchanged |
| `pytest missing.py` | none | `focused` | pytest only, missing target forwarded |
| `ruff missing.py` | none | `focused` | Ruff only, missing target forwarded |
| `--all missing.py` | none | `focused` | Ruff, annotations, ty, Bandit, pytest; missing target forwarded |
| `--all ty` | none | `strict_aggregate` | Ruff, annotations, ty, Bandit, pytest |
| existing `a.py b.py` | `a.py`, `b.py` | `focused` | Four file checks; target order remains `a.py`, `b.py` |
| existing `a.py a.py` | `a.py` | `focused` | Four file checks; duplicate targets remain duplicated |
| existing absolute path only | exact absolute token | `focused` | Four file checks; absolute token unchanged |
| missing `z.py a.py` only | none | planning error | `Unknown check(s): a.py, z.py` |

For the command matrix, assert:

- strict Ruff and annotations target `.`;
- strict Bandit uses `-r .`;
- strict ty and pytest receive no target;
- explicit targets override configured targets;
- focused Ruff and Bandit use configured targets when no direct target exists;
- direct Bandit targets do not use `-r`;
- frozen commands begin `uv run --frozen python -m`;
- unfrozen commands begin `uv run python -m`.

In `tests/test_config.py`, import `collect_existing_positionals` and add:

```python
def test_collects_existing_relative_and_absolute_positionals(tmp_path: Path) -> None:
    relative = tmp_path / "api.py"
    relative.write_text("", encoding="utf-8")
    absolute = tmp_path / "outside.py"
    absolute.write_text("", encoding="utf-8")

    result = collect_existing_positionals(
        tmp_path,
        ("api.py", str(absolute), "missing.py", "tests/test_x.py::test_name"),
    )

    assert result == frozenset(("api.py", str(absolute)))
```

- [ ] **Step 2: Verify the planner tests fail for the intended reason**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_config.py::test_collects_existing_relative_and_absolute_positionals \
  -q
uv run --frozen python -m pytest tests/test_planning.py -q
```

Expected: the focused config test fails because
`collect_existing_positionals` does not exist, and planner collection fails
with `ModuleNotFoundError: No module named 'pyrepo_check.planning'`.

- [ ] **Step 3: Add immutable request, fact, command, and plan types**

Create `src/pyrepo_check/planning.py` with these public internal shapes:

```python
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pyrepo_check.config import ProjectConfig


CheckName = Literal[
    "ruff",
    "annotations",
    "annotations-fix",
    "ty",
    "bandit",
    "pytest",
]
RunMode = Literal["focused", "strict_aggregate"]

CHECK_ORDER: tuple[CheckName, ...] = (
    "ruff",
    "annotations",
    "ty",
    "bandit",
    "pytest",
)
SELECTABLE_CHECK_ORDER: tuple[CheckName, ...] = (*CHECK_ORDER, "annotations-fix")
TARGET_DEFAULT_CHECKS: tuple[CheckName, ...] = (
    "ruff",
    "annotations",
    "ty",
    "bandit",
)


@dataclass(frozen=True)
class RunRequest:
    root: Path
    positionals: tuple[str, ...]
    all_selected: bool
    no_frozen: bool


@dataclass(frozen=True)
class PlanningFacts:
    existing_positionals: frozenset[str]


@dataclass(frozen=True)
class PlannedCheck:
    name: CheckName
    command: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class RunPlan:
    mode: RunMode
    targets: tuple[str, ...]
    checks: tuple[PlannedCheck, ...]
```

Do not add future output-format, shortcut, coverage-request, report, or outcome
fields in this milestone.

- [ ] **Step 4: Keep filesystem inspection in the project-facts adapter**

Add this fact collector to `config.py`:

```python
def collect_existing_positionals(
    root: Path,
    positionals: Sequence[str],
) -> frozenset[str]:
    return frozenset(
        token
        for token in positionals
        if _target_exists(root, token)
    )


def _target_exists(root: Path, target: str) -> bool:
    path = Path(target)
    return path.exists() if path.is_absolute() else (root / path).exists()
```

Import `Sequence` from `collections.abc`. The CLI constructs
`PlanningFacts(existing_positionals=collect_existing_positionals(...))` before
calling the planner. `plan_run(request, config, facts)` must not read files,
environment variables, clocks, or subprocesses.

- [ ] **Step 5: Implement deterministic planning policy**

Implement the policy in `planning.py`:

```python
def plan_run(
    request: RunRequest,
    config: ProjectConfig,
    facts: PlanningFacts,
) -> RunPlan:
    requested, targets = _split_positionals(request.positionals)
    if targets and not requested and not request.all_selected:
        missing = tuple(
            target
            for target in targets
            if target not in facts.existing_positionals
        )
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Unknown check(s): {names}")
        requested = TARGET_DEFAULT_CHECKS

    strict_all = not targets and (
        request.all_selected or not request.positionals
    )
    available = build_checks(config, targets=targets, strict_all=strict_all)
    selected = select_checks(
        available,
        requested=requested,
        all_selected=request.all_selected,
    )
    return RunPlan(
        mode="strict_aggregate" if strict_all else "focused",
        targets=targets,
        checks=selected,
    )


def _split_positionals(
    positionals: Sequence[str],
) -> tuple[tuple[CheckName, ...], tuple[str, ...]]:
    check_names = set(SELECTABLE_CHECK_ORDER)
    requested = tuple(
        cast(CheckName, token)
        for token in positionals
        if token in check_names
    )
    targets = tuple(token for token in positionals if token not in check_names)
    return requested, targets


def build_checks(
    config: ProjectConfig,
    *,
    targets: Sequence[str] = (),
    strict_all: bool = False,
) -> dict[str, PlannedCheck]:
    prefix = _uv_python_prefix(config)
    explicit_targets = tuple(targets)
    strict_targets = (".",) if strict_all and not explicit_targets else ()
    ruff_targets = explicit_targets or strict_targets or config.ruff_targets
    bandit_targets = explicit_targets or strict_targets or config.bandit_targets

    return {
        "ruff": PlannedCheck(
            name="ruff",
            command=(*prefix, "ruff", "check", *ruff_targets),
            cwd=config.root,
        ),
        "annotations": PlannedCheck(
            name="annotations",
            command=(
                *prefix,
                "ruff",
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--output-format",
                "concise",
            ),
            cwd=config.root,
        ),
        "annotations-fix": PlannedCheck(
            name="annotations-fix",
            command=(
                *prefix,
                "ruff",
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--fix",
                "--unsafe-fixes",
            ),
            cwd=config.root,
        ),
        "ty": PlannedCheck(
            name="ty",
            command=(*prefix, "ty", "check", *explicit_targets),
            cwd=config.root,
        ),
        "bandit": PlannedCheck(
            name="bandit",
            command=(
                *prefix,
                "bandit",
                "-c",
                "pyproject.toml",
                *_bandit_target_args(
                    bandit_targets,
                    recursive=not explicit_targets,
                ),
            ),
            cwd=config.root,
        ),
        "pytest": PlannedCheck(
            name="pytest",
            command=(*prefix, "pytest", *explicit_targets),
            cwd=config.root,
        ),
    }


def select_checks(
    checks: Mapping[str, PlannedCheck],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[PlannedCheck, ...]:
    selected_names = select_check_names(
        checks.keys(),
        requested=requested,
        all_selected=all_selected,
    )
    return tuple(checks[name] for name in selected_names)


def select_check_names(
    available_names: Collection[str],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[CheckName, ...]:
    if all_selected or not requested:
        return CHECK_ORDER

    unknown = sorted(set(requested) - set(available_names))
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(f"Unknown check(s): {names}")

    requested_names = set(requested)
    return tuple(
        name
        for name in SELECTABLE_CHECK_ORDER
        if name in requested_names
    )


def _uv_python_prefix(config: ProjectConfig) -> tuple[str, ...]:
    if config.frozen:
        return ("uv", "run", "--frozen", "python", "-m")
    return ("uv", "run", "python", "-m")


def _bandit_target_args(
    targets: Sequence[str],
    *,
    recursive: bool,
) -> tuple[str, ...]:
    if recursive:
        return ("-r", *targets)
    return tuple(targets)
```

Inside `plan_run`:

1. Classify every positional equal to a selectable check name as a requested
   check; preserve every other token as a direct target.
2. When there are targets, no selected checks, and no `--all`, require every
   target to appear in `facts.existing_positionals`. Sort missing tokens in the
   exact `Unknown check(s): ...` error.
3. For a valid target-only request, select `TARGET_DEFAULT_CHECKS`.
4. Set strict aggregate only when there are no targets and either `--all` is
   selected or there are no positionals.
5. Build exact commands using effective `config.frozen`.
6. Select aggregate checks in `CHECK_ORDER`; select explicit checks in
   `SELECTABLE_CHECK_ORDER`, deduplicated and canonicalized.
7. Set `mode="strict_aggregate"` only for the target-free aggregate; otherwise
   set `mode="focused"`.
8. Do not add report artifacts or evidence-contribution fields; those belong to
   the milestone that consumes them.

- [ ] **Step 6: Run planner and existing runner tests**

Run:

```bash
uv run --frozen python -m pytest tests/test_planning.py tests/test_runner.py -q
```

Expected: planner matrix passes; existing runner tests remain green because CLI
and runner wiring have not moved yet.

- [ ] **Step 7: Run typing-focused checks and the strict gate**

Run:

```bash
pyrepo-check annotations ty src/pyrepo_check/planning.py tests/test_planning.py
uv run --frozen pyrepo-check --all
```

Expected: annotation policy, ty, Ruff, Bandit, and the complete pytest suite
pass.

- [ ] **Step 8: Commit pure planning**

```bash
git add \
  src/pyrepo_check/config.py \
  src/pyrepo_check/planning.py \
  tests/test_config.py \
  tests/test_planning.py
git commit -m "refactor: extract pure run planning"
```

---

### Task 3: Isolate Process Execution

**Files:**

- Create: `src/pyrepo_check/execution.py`
- Create: `tests/test_execution.py`
- Test helper: `tests/support.py`

**Interfaces:**

- Consumes: immutable `RunPlan` and an injected `ProcessRunner`.
- Produces: immutable `ExecutedCheck`, `ExecutionResult`, and
  `execute_plan(plan, *, runner=...)`.

- [ ] **Step 1: Write failing executor tests**

Create `tests/test_execution.py` with a two-check `RunPlan`. Add exact tests for:

- zero return codes produce exit `0`;
- commands run in plan order with exact argv, cwd, and `check=False`;
- each banner is `\n==> <name>: <shlex-joined-command>\n` before its spawn;
- ordinary failures continue and the first positive nonzero wins;
- a first negative nonzero remains the final exit code while later commands run;
- `FileNotFoundError` is propagated by identity and later checks do not run;
- an injected `ValueError` is propagated by identity.

Use this plan shape in the tests:

```python
plan = RunPlan(
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
```

- [ ] **Step 2: Verify executor tests fail for the intended reason**

Run:

```bash
uv run --frozen python -m pytest tests/test_execution.py -q
```

Expected: collection fails with
`ModuleNotFoundError: No module named 'pyrepo_check.execution'`.

- [ ] **Step 3: Implement the minimal executor types**

Create `src/pyrepo_check/execution.py` with these interfaces:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import shlex
import subprocess  # nosec B404

from pyrepo_check.planning import PlannedCheck, RunPlan


ProcessRunner = Callable[
    ...,
    subprocess.CompletedProcess[tuple[str, ...]],
]


@dataclass(frozen=True)
class ExecutedCheck:
    planned: PlannedCheck
    returncode: int


@dataclass(frozen=True)
class ExecutionResult:
    checks: tuple[ExecutedCheck, ...]
    exit_code: int
```

- [ ] **Step 4: Move current execution semantics without improving them**

Implement:

```python
def execute_plan(
    plan: RunPlan,
    *,
    runner: ProcessRunner = subprocess.run,
) -> ExecutionResult:
    executed: list[ExecutedCheck] = []
    exit_code = 0

    for check in plan.checks:
        print(f"\n==> {check.name}: {shlex.join(check.command)}", flush=True)
        completed = runner(check.command, cwd=check.cwd, check=False)
        executed.append(ExecutedCheck(planned=check, returncode=completed.returncode))
        if completed.returncode != 0 and exit_code == 0:
            exit_code = completed.returncode

    return ExecutionResult(checks=tuple(executed), exit_code=exit_code)
```

Do not catch subprocess exceptions. Spawn-error continuation is Milestone B
behavior and is explicitly excluded here.

- [ ] **Step 5: Run executor and compatibility tests**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_execution.py \
  tests/test_compatibility.py \
  tests/test_runner.py \
  -q
```

Expected: executor tests pass; current CLI compatibility remains green while
the CLI still uses the legacy runner implementation.

- [ ] **Step 6: Run typing-focused checks and the strict gate**

Run:

```bash
pyrepo-check annotations ty src/pyrepo_check/execution.py tests/test_execution.py
uv run --frozen pyrepo-check --all
```

Expected: all checks pass.

- [ ] **Step 7: Commit isolated execution**

```bash
git add src/pyrepo_check/execution.py tests/test_execution.py
git commit -m "refactor: isolate planned check execution"
```

---

### Task 4: Route the CLI Through the Plan and Preserve Runner Imports

**Files:**

- Modify: `src/pyrepo_check/cli.py`
- Modify: `src/pyrepo_check/runner.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_runner.py`
- Test: `tests/test_compatibility.py`
- Test: `tests/test_planning.py`
- Test: `tests/test_execution.py`

**Interfaces:**

- Consumes: `RunRequest`, `collect_existing_positionals`, `plan_run`,
  `execute_plan`, and existing argparse inputs.
- Produces: a thin `main(argv, *, runner=...)` and legacy runner facade with
  `Check`, `build_checks`, `select_checks`, and `run_checks` still importable.

- [ ] **Step 1: Add a failing thin-CLI integration test**

Add a test that monkeypatches `pyrepo_check.cli.plan_run` and
`pyrepo_check.cli.execute_plan` with recording functions:

```python
def test_cli_builds_request_and_executes_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_plan = RunPlan(mode="focused", targets=(), checks=())
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
        return ExecutionResult(checks=(), exit_code=7)

    monkeypatch.setattr(
        "pyrepo_check.cli.plan_run",
        fake_plan_run,
        raising=False,
    )
    monkeypatch.setattr(
        "pyrepo_check.cli.execute_plan",
        fake_execute_plan,
        raising=False,
    )

    result = main(
        ["--root", str(tmp_path), "--no-frozen", "ty"],
        runner=injected_runner,
    )

    assert result == 7
    assert planned_requests == [
        RunRequest(
            root=tmp_path,
            positionals=("ty",),
            all_selected=False,
            no_frozen=True,
        )
    ]
    assert executed_plans == [expected_plan]
```

Import `ProcessRunner`, `ExecutionResult`, the planning types, `ProjectConfig`,
and `RecordingRunner` in the test module. This pins the adapter boundary while
leaving command policy to planner tests.

- [ ] **Step 2: Verify the integration test fails before CLI rewiring**

Run:

```bash
uv run --frozen python -m pytest tests/test_cli.py -q
```

Expected: the monkeypatches succeed because `raising=False`, but the new
integration test fails because legacy `cli.main` ignores those injected names,
still calls `build_checks`, `select_checks`, and `run_checks` directly, and
therefore does not return the fake executor's exit code `7`.

- [ ] **Step 3: Rewire `cli.main` while preserving its signature**

Keep the signature:

```python
def main(
    argv: Sequence[str] | None = None,
    *,
    runner: ProcessRunner = subprocess.run,
) -> int:
```

Implement the body in this order:

```python
args = parse_args(argv)
request = RunRequest(
    root=Path(args.root),
    positionals=tuple(args.checks),
    all_selected=args.all,
    no_frozen=args.no_frozen,
)

try:
    config = load_project_config(request.root, no_frozen=request.no_frozen)
    facts = PlanningFacts(
        existing_positionals=collect_existing_positionals(
            config.root,
            request.positionals,
        )
    )
    plan = plan_run(request, config, facts)
except ValueError as error:
    print(error, file=sys.stderr)
    return 2

result = execute_plan(plan, runner=runner)
return result.exit_code
```

Execution must remain after the `except` block. Do not catch runner exceptions.

- [ ] **Step 4: Convert `runner.py` into a compatibility facade**

Replace duplicated planning and execution policy with this compatibility
facade:

```python
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess  # nosec B404
from typing import cast

from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import execute_plan
from pyrepo_check.planning import (
    CHECK_ORDER as CHECK_ORDER,
    SELECTABLE_CHECK_ORDER as SELECTABLE_CHECK_ORDER,
    CheckName,
    PlannedCheck,
    RunPlan,
    build_checks as build_planned_checks,
    select_check_names,
)


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]


def build_checks(
    config: ProjectConfig,
    *,
    targets: Sequence[str] = (),
    strict_all: bool = False,
) -> dict[str, Check]:
    planned = build_planned_checks(
        config,
        targets=targets,
        strict_all=strict_all,
    )
    return {
        name: Check(name=check.name, command=check.command)
        for name, check in planned.items()
    }


def select_checks(
    checks: Mapping[str, Check],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[Check, ...]:
    selected_names = select_check_names(
        checks.keys(),
        requested=requested,
        all_selected=all_selected,
    )
    return tuple(checks[name] for name in selected_names)


def run_checks(
    checks: Sequence[Check],
    *,
    cwd: Path,
    runner: Callable[
        ...,
        subprocess.CompletedProcess[tuple[str, ...]],
    ] = subprocess.run,
) -> int:
    prepared = tuple(
        PlannedCheck(
            name=cast(CheckName, check.name),
            command=check.command,
            cwd=cwd,
        )
        for check in checks
    )
    plan = RunPlan(mode="focused", targets=(), checks=prepared)
    return execute_plan(plan, runner=runner).exit_code
```

This retains the original two-field `runner.Check` rather than aliasing it to a
new dataclass. Add facade tests that assert:

```python
assert tuple(inspect.signature(Check).parameters) == ("name", "command")
assert tuple(field.name for field in dataclasses.fields(Check)) == (
    "name",
    "command",
)
assert repr(Check("ruff", ("uv",))) == "Check(name='ruff', command=('uv',))"
assert isinstance(build_checks(config)["ruff"], Check)
```

Also assert that `select_checks` returns the original `Check` objects from its
mapping in canonical order. The facade delegates ordering to
`select_check_names`, command construction to `build_planned_checks`, and
execution to `execute_plan`; it contains no duplicated policy loop.

- [ ] **Step 5: Move policy assertions out of CLI tests**

Keep CLI tests for:

- argparse help and syntax;
- construction of `RunRequest`;
- root and `--no-frozen` configuration loading;
- planning-error stderr and exit code `2`;
- no subprocess call after a planning error;
- returned execution exit code;
- runner exception propagation.

Keep planner command-policy assertions in `tests/test_planning.py`, executor
semantics in `tests/test_execution.py`, and black-box compatibility checks in
`tests/test_compatibility.py`. Reduce `tests/test_runner.py` to facade tests that
prove the four legacy names remain importable and delegate without changing
return values or banners.

Use this one-to-one migration map. Do not delete or weaken an existing test
until its named replacement is GREEN with the same exact argv, ordering,
target, error, or return-value assertion:

| Existing assertion | Required replacement home |
| --- | --- |
| `test_cli_runs_selected_check_from_root` | Planner case: focused Ruff with exact configured-target argv and cwd |
| `test_cli_runs_annotations_check_from_root` | Planner case: focused annotations with exact argv |
| `test_cli_runs_annotations_fix_from_root` | Planner case: explicit annotations-fix with exact argv |
| `test_cli_passes_file_target_to_selected_check` | Planner case: Ruff with one direct target and exact argv |
| `test_cli_passes_file_target_to_annotations_check` | Planner case: annotations with one direct target and exact argv |
| `test_cli_passes_file_target_to_annotations_fix` | Planner case: annotations-fix with one direct target and exact argv |
| `test_cli_runs_file_checks_against_file_target_when_no_check_is_named` | Planner case: target-only four-check order with all four exact commands |
| `test_cli_runs_all_checks_against_file_target_with_all_flag` | Planner case: `--all` plus target with five exact commands |
| `test_cli_all_includes_annotations_but_not_annotations_fix` | Planner aggregate case: exact five-check order and commands; annotations-fix absent |
| `test_cli_no_args_uses_strict_repository_targets` | Planner no-positionals case: repository-root strict commands, ignoring configured narrow targets |
| `test_cli_returns_two_for_unknown_check` | Planner exact sorted error plus CLI stderr/exit-code/no-spawn adapter test |
| `test_cli_no_frozen_flag_overrides_lock` | Config effective-frozen test plus planner exact unfrozen argv |
| `test_builds_uv_frozen_commands` | Planner frozen command matrix for all six selectable checks |
| `test_builds_unfrozen_commands` | Planner exact unfrozen prefix assertion |
| `test_builds_strict_all_commands_against_repository_root` | Planner strict aggregate exact command matrix |
| `test_explicit_targets_override_strict_all_targets` | Planner direct-target precedence with exact Ruff and Bandit argv |
| `test_selects_all_when_requested_list_is_empty` | Planner `select_check_names` aggregate-order assertion |
| `test_selects_annotations_fix_when_requested` | Planner explicit annotations-fix selection assertion |
| `test_selects_all_without_annotations_fix` | Planner aggregate exclusion assertion |
| `test_rejects_unknown_check` | Planner exact unknown-check error assertion |
| `test_runs_all_checks_and_returns_first_failing_exit_code` | Executor order, continuation, and first-raw-nonzero assertions |

The CLI adapter tests remain responsible for proving that parsed arguments,
loaded facts, planner errors, executor results, and injected runner exceptions
cross the adapter boundary unchanged. The compatibility tests remain
responsible for end-to-end banners and direct pytest node forwarding.

- [ ] **Step 6: Run the focused architecture suite**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_cli.py \
  tests/test_config.py \
  tests/test_planning.py \
  tests/test_execution.py \
  tests/test_runner.py \
  tests/test_compatibility.py \
  -q
```

Expected: all focused architecture and compatibility tests pass.

- [ ] **Step 7: Run the strict gate**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: Ruff, annotations, ty, Bandit, and all tests pass. The command help,
tool order, subprocess banners, and CLI exit behavior are unchanged.

- [ ] **Step 8: Commit the CLI integration and compatibility facade**

```bash
git add \
  src/pyrepo_check/cli.py \
  src/pyrepo_check/runner.py \
  tests/test_cli.py \
  tests/test_runner.py \
  tests/test_compatibility.py
git commit -m "refactor: route CLI through run plans"
```

---

### Task 5: Verify and Record the Runtime Milestone Honestly

**Files:**

- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Test: complete repository

**Interfaces:**

- Consumes: the merged behavior-preserving architecture from Tasks 1–4.
- Produces: explicit designed/implemented/verified status for Milestones A–D.

- [ ] **Step 1: Run all focused tests from a clean process**

Run:

```bash
uv run --frozen python -m pytest -q
```

Expected: every test passes.

- [ ] **Step 2: Run the strict repository gate**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: Ruff, annotations, ty, Bandit, and pytest pass.

- [ ] **Step 3: Verify formatting and repository scope**

Run:

```bash
git diff --check
git status --short
```

Expected: no diff-check errors. The unrelated untracked `LICENSE` may remain;
it must not be staged or committed.

- [ ] **Step 4: Update the design delivery-status table only after GREEN proof**

Record these exact states in the design document:

| Milestone | State | Evidence |
| --- | --- | --- |
| A — planning and execution extraction | Implemented and verified | Pure planner tests, executor tests, compatibility tests, and strict gate pass. |
| B — Agent Report and JSON | Designed; not implemented | No `reporting.py` or JSON CLI format exists. |
| C — Test Shortcuts, pytest evidence, coverage | Designed; not implemented | No shortcut parser, pytest evidence plugin, or coverage execution exists. |
| D — repository coverage adoption | Designed; not implemented | No locked Coverage.py dependency or native coverage policy exists. |

Do not mark Milestone A implemented if any required test or gate is skipped or
failing.

- [ ] **Step 5: Commit verified status documentation**

```bash
git add docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "docs: record planner executor milestone status"
```

- [ ] **Step 6: Report the final state with exact labels**

The implementation handoff must state:

- **Implemented:** files and commits that actually changed runtime behavior or
  architecture.
- **Verified:** exact focused and strict commands with their results.
- **Designed only:** Agent Report, JSON, Test Shortcuts, pytest evidence,
  coverage guidance, and coverage adoption.
- **Untouched:** the unrelated untracked `LICENSE` and all later-milestone
  behavior.
