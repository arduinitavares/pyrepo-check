# Agent Report and JSON Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one versioned Agent Report with a streamed terminal projection
and a deterministic JSON projection, while preserving focused checks and
making process failures actionable for agents.

**Architecture:** `planning.py` carries output-format intent and typed semantic
planning failures. `execution.py` records one primary process observation per
planned check, including time, raw captured output, exit/signal/spawn outcome,
and continuation. New `reporting.py` converts the immutable plan plus execution
observations into one validated report, then renders terminal or JSON output.
`cli.py` remains the adapter that parses, plans, executes, constructs, renders,
and selects the final exit code. `runner.py` remains a terminal-only legacy
facade.

**Tech Stack:** Python 3.11+, stdlib `argparse`, `dataclasses`, `json`,
`pathlib`, `re`, `subprocess`, `time`, and `typing`; pytest 8; Ruff; ty;
Bandit; uv.

**Spec:**
[`docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`](../specs/2026-08-24-agent-guidance-reporting-design.md)

## Global Constraints

- This plan implements **Milestone B only**: Agent Report, terminal summary,
  JSON output, process capture, and structured planning/run errors.
- Do not add `--shortcut`, `--coverage`, Coverage.py, a pytest evidence plugin,
  coverage configuration, dependency changes, or Milestone C/D behavior.
- Preserve the six check names, exact command argv, working directory, target
  spelling/order, canonical check order, `uv run`/`--frozen` policy, direct
  pytest files/node IDs, and `annotations-fix` opt-in behavior.
- Terminal remains the default. Primary processes inherit stdout/stderr and
  keep the existing pre-spawn banner. The summary is added only after all
  planned checks have terminal observations.
- JSON stdout is exactly one compact UTF-8 JSON document followed by one
  newline. It has no banners or tool bytes beside the document.
- Catch only `OSError` subclasses from process invocation as `spawn_failed`.
  Continue later independent checks. Other execution-adapter/programming
  exceptions, including an injected runner `ValueError`, still propagate
  unchanged and are never mislabeled as planning failures. Expected config
  `ValueError` and typed `PlanningFailure` become planning reports; unexpected
  config/facts/planner `Exception` values become `internal_planning_error`.
  `KeyboardInterrupt` and `SystemExit` are not caught.
- Classify a negative child return code as `signaled`, store
  `signal=abs(returncode)`, set `exit_code=null`, mark the check/error evidence
  incomplete, and continue. This is an explicit Milestone B behavior change.
- Final CLI exit precedence is: first positive process exit in planned order;
  otherwise `1` for overall failed; otherwise `2` for overall error; otherwise
  `0`. A reporting failure preserves a first positive process exit if present,
  otherwise returns `2`.
- Captured streams retain the final 65,536 raw bytes in the report. The bound
  is on retained/serialized output; the stdlib subprocess adapter may
  transiently buffer a process stream before report construction.
- Tail retention occurs before UTF-8 decoding. Decode with `errors="replace"`,
  then remove complete ECMA-48 CSI sequences and OSC sequences terminated by
  BEL or ST. Incomplete escape fragments remain diagnostic text.
- Milestone B keeps top-level `pytest` and `coverage` null. A selected pytest
  command remains fully visible in `selection` and `checks`; structured pytest
  evidence begins in Milestone C.
- Implement only B-emittable report models. Do not pre-build unused pytest and
  coverage model hierarchies merely to reserve future schema members.
- The producer validates its own schema version and invariants before JSON
  serialization. Do not create a general public report parser in this phase.
- Build terminal text or JSON UTF-8 bytes completely before writing. A
  construction or serialization failure writes no JSON bytes and one plain
  fallback diagnostic to stderr. An error from the final stdout write is
  outside this guarantee.
- Preserve `pyrepo_check.cli.main(argv, *, runner=...)` and the two-field
  `pyrepo_check.runner.Check` facade. Existing injected runners keep the exact
  terminal call signature; JSON calls additionally receive
  `capture_output=True`.
- Add no runtime or development dependency.
- Do not include or modify the unrelated untracked `LICENSE` file in the main
  checkout.
- Use `apply_patch` for source/docs edits. Run the strict gate after every
  implementation commit.

## Resolved Contract Decisions

| Question | Milestone B decision |
| --- | --- |
| Selected pytest before the evidence plugin exists | `pytest` remains null; selection/check process fields are authoritative until Milestone C. The spec records this seam. |
| Negative subprocess return code | It is signal/error evidence. Later checks continue. Final exit follows positive-first/report precedence rather than returning a negative integer. |
| Spawn failure boundary | Catch `OSError` only. Other runner exceptions propagate. |
| Reporting failure exit | Preserve the first positive process exit, or return `2` when none exists. |
| Terminal wording | Exact snapshots below are the contract for this milestone. |
| Capture meaning | Retained report output is bounded to the final 65,536 raw bytes per stream; peak adapter buffering is not claimed. |
| ANSI sanitizer | Strip complete CSI and BEL/ST-terminated OSC after tailing and UTF-8 replacement. Preserve incomplete fragments. |
| Truncation identity | Advisory message includes check name, 1-based process index, role, stream, and exact omitted-byte count. |
| Version rejection | Internal producer validation rejects non-1 reports; no consumer API is introduced. |
| Missing primary result | Retain the selected check in planned order with `missing_primary_process`, no processes, incomplete/error. Extra or duplicate executor results are internal reporting errors. |

## Baseline Status

| State | Evidence |
| --- | --- |
| Implemented | Milestone A planner/executor boundaries at commit `63654c0`. |
| Verified | `uv run --frozen python -m pytest -q` passes 60 tests and `uv run --frozen pyrepo-check --all` passes Ruff, annotations, ty, Bandit, and pytest in the isolated worktree. |
| Designed only | Agent Report models, terminal summary, JSON rendering/capture, typed process errors, and `--format`. |
| Explicitly deferred | Test Shortcuts, pytest structured evidence, Coverage.py execution/guidance, and repository coverage adoption. |

## Plan Publication Gate

Commit this plan and the contract-seam clarifications in the design spec
before runtime Task 1. That documentation commit is approved/planned work only;
it is not runtime implementation.

---

### Task 1: Add Internal Output Intent and Typed Planning Failures

**Files:**

- Modify: `src/pyrepo_check/planning.py`
- Modify: `tests/test_planning.py`

**Interfaces:**

- Add `OutputFormat = Literal["terminal", "json"]`.
- Add trailing defaulted `output_format: OutputFormat = "terminal"` to
  `RunRequest` and `RunPlan` so existing positional construction remains valid.
- Add `PlanningErrorCode` and `PlanningFailure(ValueError)` with public `code`,
  inherited message, and nullable `hint` attributes.
- Do not expose `--format` through argparse until Task 5 atomically wires
  capture, report construction, serialization, and output.

- [ ] **Step 1: Write failing output-intent planner tests**

Add planner tests proving:

```python
terminal = RunRequest(tmp_path, ("ty",), False, False)
machine = RunRequest(tmp_path, ("ty",), False, False, "json")
assert terminal.output_format == "terminal"
assert plan_run(machine, config, facts).output_format == "json"
```

Assert that `plan_run` copies JSON intent from `RunRequest` to `RunPlan`, while
all existing requests still compare with `output_format="terminal"` by
default. Existing CLI/help compatibility tests remain unchanged, which proves
JSON is not publicly exposed at this intermediate commit.

Run:

```bash
uv run --frozen python -m pytest tests/test_planning.py -q
```

Expected: RED because the internal output-intent fields do not exist.

- [ ] **Step 2: Implement output intent without changing plan policy**

Use trailing defaults:

```python
OutputFormat = Literal["terminal", "json"]


@dataclass(frozen=True)
class RunRequest:
    root: Path
    positionals: tuple[str, ...]
    all_selected: bool
    no_frozen: bool
    output_format: OutputFormat = "terminal"


@dataclass(frozen=True)
class RunPlan:
    mode: RunMode
    targets: tuple[str, ...]
    checks: tuple[PlannedCheck, ...]
    output_format: OutputFormat = "terminal"
```

`plan_run` sets `output_format=request.output_format`. Do not modify `cli.py` in
this task; every public CLI invocation still creates the default terminal
request.

- [ ] **Step 3: Write failing typed-failure tests**

Add tests that:

- `select_check_names(... requested=("mypy",) ...)` raises a
  `PlanningFailure` that is still a `ValueError`, with `code="unknown_check"`;
- missing path-like target-only tokens such as `a.py` use
  `code="unknown_target"` and preserve the current sorted message;
- a bare unknown token such as `mypy` uses `unknown_check`;
- known direct check plus missing target remains allowed exactly as today.

Use a path-like classifier only for the all-unknown target-only ambiguity:
tokens containing `/`, `\\`, `::`, or a filename suffix are targets; otherwise
they are check names. Mixed unknown tokens choose `unknown_target` when every
token is path-like, otherwise `unknown_check`. No filesystem policy changes.

- [ ] **Step 4: Implement `PlanningFailure`**

Use:

```python
PlanningErrorCode = Literal[
    "invalid_arguments",
    "invalid_project_config",
    "unknown_check",
    "unknown_target",
    "internal_planning_error",
]


class PlanningFailure(ValueError):
    def __init__(
        self,
        code: PlanningErrorCode,
        message: str,
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
```

Give unknown checks the deterministic hint
`Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest`.
Give unknown targets the hint `Check the target path or select a check name.`

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_planning.py -q
uv run --frozen pyrepo-check --all
git diff --check
```

Expected: all focused tests and the strict gate pass.

Commit:

```bash
git add src/pyrepo_check/planning.py tests/test_planning.py
git commit -m "feat: carry report output intent"
```

---

### Task 2: Record Report-Ready Process Outcomes

**Files:**

- Modify: `src/pyrepo_check/execution.py`
- Modify: `tests/support.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_compatibility.py`
- Test: `tests/test_runner.py`

**Interfaces:**

- Extend `ExecutedCheck` with `duration_ms`, raw nullable `stdout`/`stderr`,
  and nullable `spawn_error` while preserving `planned` and `returncode`.
- `returncode` is nullable only for spawn failure. A negative value remains raw
  in the execution observation and is classified by reporting.
- Add `clock_ns: Callable[[], int] = time.monotonic_ns` to `execute_plan` for
  deterministic duration tests.
- Terminal runner calls remain exactly `(command, cwd=..., check=False)`.
- JSON runner calls add only `capture_output=True`; production capture is bytes.

- [ ] **Step 1: Extend the recording adapter and write failing outcome tests**

Let `RecordingRunner` accept keyword-only `capture_output: bool = False` and
record it. Add per-call `stdout` and `stderr` byte tuples so JSON tests can
return arbitrary captured data.

Add tests for:

- terminal calls have `capture_output=False` and banners appear before spawn;
- JSON calls have `capture_output=True` and print no banner;
- returned bytes are stored without decoding in `ExecutedCheck`;
- duration boundaries use `(elapsed_ns + 500_000) // 1_000_000`, covering
  `499_999 ns -> 0`, `500_000 ns -> 1`, and `1_500_000 ns -> 2`;
- positive failures continue and the first positive code wins;
- `FileNotFoundError`/`PermissionError` become nullable-returncode observations
  and later checks run;
- an injected `ValueError` propagates by identity and aborts;
- negative return codes are recorded and later checks run;
- if there is no positive exit, any spawn/negative outcome makes legacy
  `ExecutionResult.exit_code == 2`; a later first positive exit still wins.

Run:

```bash
uv run --frozen python -m pytest tests/test_execution.py tests/test_compatibility.py -q
```

Expected: RED because execution has no capture/time/spawn observation.

- [ ] **Step 2: Implement raw process observations**

Use an immutable shape equivalent to:

```python
@dataclass(frozen=True)
class ExecutedCheck:
    planned: PlannedCheck
    returncode: int | None
    duration_ms: int
    stdout: bytes | None
    stderr: bytes | None
    spawn_error: str | None
```

For each check:

1. print/flush the existing banner only in terminal mode;
2. read `clock_ns()` immediately before invocation;
3. invoke the runner with terminal or JSON kwargs;
4. catch only `OSError` and store
   `f"{type(error).__name__}: {error}"`;
5. read `clock_ns()` in a `finally` path so spawn failures are timed;
6. append exactly one `ExecutedCheck`; and
7. continue.

Normalize captured `str` from custom runners to UTF-8 bytes for compatibility,
but production JSON calls request bytes. Ignore any fake `stdout`/`stderr` in
terminal mode and store `None` so the report truthfully says not captured.

- [ ] **Step 3: Update the intentional compatibility assertions**

Replace the old negative-return test. Exact examples:

```text
[-15, 7, 0, 0, 0] -> final 7
[-15, 0, 0, 0, 0] -> final 2
spawn, 0 -> final 2
spawn, 7 -> final 7
```

Keep the injected `ValueError` identity test unchanged. Update legacy
`runner.py` fixtures only where `RunPlan.output_format` default/equality makes
the new field visible.

- [ ] **Step 4: Verify and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_execution.py tests/test_compatibility.py tests/test_runner.py -q
uv run --frozen pyrepo-check --all
git diff --check
```

Commit:

```bash
git add src/pyrepo_check/execution.py tests/support.py \
  tests/test_execution.py tests/test_compatibility.py tests/test_runner.py
git commit -m "feat: record process outcomes for reports"
```

---

### Task 3: Build and Validate the Milestone B Agent Report

**Files:**

- Create: `src/pyrepo_check/reporting.py`
- Create: `tests/test_reporting.py`
- Test: `src/pyrepo_check/planning.py`
- Test: `src/pyrepo_check/execution.py`

**Interfaces:**

- Add frozen B-emittable types: `PlanningError`, `PlanningErrorReportV1`,
  `CapturedText`, `ProcessResult`, `CheckError`, `CheckResult`, `Selection`,
  `Advisory`, and `RunReportV1`.
- Add `AgentReportV1 = PlanningErrorReportV1 | RunReportV1`.
- Add `build_planning_error_report(...)`,
  `build_run_report(project_root, plan, execution)`, and
  `validate_report_v1(report)`.
- Keep `pytest: None` and `coverage: None` required fields on
  `RunReportV1` in this milestone.

- [ ] **Step 1: Write exact model/builder tests before the module exists**

Create fixtures for passed, failed, signaled, spawn-failed, and missing-primary
runs. Assert recursively exact field values and these rules:

- resolved absolute native `project_root` and process `cwd`;
- `selection.checks` and `checks` remain planned order;
- targets preserve spelling/order/duplicates;
- `pytest_args` is null when pytest is not selected and equals `plan.targets`
  (including an empty list) when selected;
- `planned_test_scope` is `not_selected`, `partial`, or `complete` from pytest
  selection/targets; `planned_coverage_scope` is `not_requested`;
- exit `0 -> passed`, positive exit `-> failed`, negative/spawn `-> error`;
- a missing selected process becomes `missing_primary_process`, has an empty
  process array, and remains in position;
- `complete` is false for error/missing primary and true for ordinary pass or
  positive failure;
- overall precedence is error, then failed, then passed;
- top-level `pytest` and `coverage` are present and null;
- planning-error reports contain no run-only fields.

Add a cardinality matrix before implementation:

| Execution observations | Expected builder behavior |
| --- | --- |
| planned Ruff + ty, observed only ty | Ruff becomes `missing_primary_process`; ty remains observed |
| observed Ruff + extra Bandit | `ReportingError` |
| observed Ruff twice | `ReportingError` |
| observed check whose `PlannedCheck` differs from the plan | `ReportingError` |
| observed ty before Ruff for a Ruff → ty plan | `ReportingError` |

The builder accepts a unique in-order subsequence of planned checks so missing
results can be synthesized in place. It rejects extras, duplicates, mismatched
planned command/cwd data, and out-of-order observations rather than silently
reordering them.

Run:

```bash
uv run --frozen python -m pytest tests/test_reporting.py -q
```

Expected: RED during import because `reporting.py` does not exist.

- [ ] **Step 2: Implement the report types and projection**

Use `Literal` aliases for every B enum. A process projection follows:

```text
raw returncode == 0      -> exited, exit_code 0, passed
raw returncode > 0       -> exited, that exit_code, failed
raw returncode < 0       -> signaled, signal abs(code), error
raw returncode is null   -> spawn_failed, null exit/signal, error
```

For terminal observations, both streams are exactly:

```python
CapturedText(False, "", False, 0)
```

For this step, implement captured empty/plain byte streams only far enough to
satisfy the initial builder fixtures. The boundary, sanitizer, truncation, and
advisory behaviors remain RED work in the next steps.

- [ ] **Step 3: Write failing capture-boundary and advisory tests**

Cover independently for stdout and stderr:

| Raw bytes | Expected |
| --- | --- |
| `0`, `65_535`, `65_536` bytes | not truncated, omitted 0 |
| `65_537` bytes | final 65,536 retained, omitted 1 |
| much larger | exact tail and exact omitted count |
| invalid UTF-8 at retained boundary | U+FFFD after tailing |
| CSI color | escape removed, text retained |
| OSC + BEL and OSC + ST | whole complete control sequence removed |
| incomplete OSC | preserved |

Each truncated stream adds one advisory with exact message:

```text
<check> process <index> (<role>) <stream> omitted <N> byte(s); only the final 65536 bytes are included.
```

The hint is null. Sort advisories by `(code, message)`.

- [ ] **Step 4: Implement captured-text normalization and advisories**

Use a pure helper:

```python
def capture_text(raw: bytes) -> CapturedText:
    retained = raw[-65_536:]
    omitted = len(raw) - len(retained)
    text = strip_terminal_sequences(retained.decode("utf-8", errors="replace"))
    return CapturedText(True, text, omitted > 0, omitted)
```

Use a standard CSI regex equivalent to
`ESC [ [0-?]* [ -/]* [@-~]`. Remove OSC from `ESC ]` through the first BEL or
ST (`ESC \\`). Preserve an unterminated OSC tail rather than deleting arbitrary
diagnostic text.

Run the capture/advisory tests and confirm GREEN before continuing.

- [ ] **Step 5: Write the failing producer-invariant matrix**

Before adding validation, use `dataclasses.replace` to create one invalid
producer model per rule and assert `ReportingError` with a stable message
prefix. Include:

- schema version and every B enum;
- negative duration/omitted-byte values;
- relative project/process paths;
- every illegal exited/signaled/spawn-failed null combination;
- every illegal captured/truncated/omitted combination;
- check error/status inconsistency;
- run completeness/overall-status inconsistency;
- planning-report fixed-value inconsistency; and
- non-null B `pytest`/`coverage`.

Run only this matrix and confirm RED because `validate_report_v1` does not yet
enforce it.

- [ ] **Step 6: Implement producer validation**

`validate_report_v1` rejects with `ReportingError`:

- `schema_version != 1` or an unknown `kind`/enum;
- negative duration/omitted-byte values;
- non-absolute project/process paths;
- exited/signaled/spawn-failed nullability violations;
- inconsistent captured/truncated/omitted states;
- `CheckResult.error` inconsistent with `status="error"`;
- run completeness/status precedence violations;
- planning-report fixed-value violations; and
- unexpected non-null B `pytest` or `coverage`.

Do not build an arbitrary-dict consumer. Tests can use `dataclasses.replace`
on frozen producer models to prove unsupported version/invariant rejection.

Builder cardinality errors are also `ReportingError`, but are proved by the
Step 1 matrix rather than treated as model validation.

- [ ] **Step 7: Verify and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_reporting.py -q
uv run --frozen pyrepo-check --all
git diff --check
```

Commit:

```bash
git add src/pyrepo_check/reporting.py tests/test_reporting.py
git commit -m "feat: build versioned agent reports"
```

---

### Task 4: Add Independent Terminal and JSON Renderers

**Files:**

- Modify: `src/pyrepo_check/reporting.py`
- Modify: `tests/test_reporting.py`

**Interfaces:**

- Add `render_terminal(report: AgentReportV1) -> str`.
- Add `serialize_json(report: AgentReportV1) -> bytes`.
- Add `select_exit_code(report: AgentReportV1) -> int`.

- [ ] **Step 1: Write failing terminal renderer snapshots**

The terminal renderer returns a complete string but performs no I/O. A passed
run with Ruff then ty is exactly:

```text

==> pyrepo-check summary: passed (complete)
    passed: ruff, ty
```

A mixed fixture is exactly ordered as:

```text

==> pyrepo-check summary: error (incomplete)
    error: ruff: Could not start process: FileNotFoundError: uv
    failed: annotations (exit 1)
    advisory: annotations process 1 (primary) stderr omitted 1 byte(s); only the final 65536 bytes are included.
    passed: ty
```

Use the actual report fixture message if the platform error string differs;
the renderer itself must not reconstruct OS messages. Planning errors render
to stderr-ready text:

```text
Unknown check(s): mypy
Hint: Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest
```

with the hint line omitted when null. Error checks appear first in planned
order, then failed checks, advisories, then one compact success line.

- [ ] **Step 2: Write failing exact JSON tests**

`serialize_json` uses:

```python
text = json.dumps(
    payload,
    ensure_ascii=False,
    allow_nan=False,
    separators=(",", ":"),
)
return text.encode("utf-8") + b"\n"
```

Assert:

- one terminal newline and no other bytes are returned by the serializer;
- exact UTF-8 bytes are returned even when the stdout text encoding is not
  UTF-8; Unicode is not rendered as `\\u` escapes;
- exact recursive keys, required nulls, enum strings, array order, and
  normative object member insertion order;
- planning report has only its five top-level keys;
- run report has only its eleven top-level keys;
- repeated serialization is byte-stable;
- `validate_report_v1` runs before serialization;
- monkeypatched encoder/validation failure raises before returning any bytes.

The final assertion means no bytes object is returned; the CLI no-partial
guarantee is tested separately before any write.

- [ ] **Step 3: Implement manual schema projection and renderers**

Do not use `dataclasses.asdict` as the wire contract. Write small private
projection functions that insert every member explicitly in normative order.
This prevents a future internal dataclass field from leaking into schema v1.

`select_exit_code` walks check/process order and returns the first positive
`exit_code`; if none, it maps overall failed/error/passed to `1/2/0`.
Planning errors return `2`.

- [ ] **Step 4: Verify and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_reporting.py -q
uv run --frozen pyrepo-check --all
git diff --check
```

Commit:

```bash
git add src/pyrepo_check/reporting.py tests/test_reporting.py
git commit -m "feat: render terminal and json reports"
```

---

### Task 5: Wire the CLI Through One Report

**Files:**

- Modify: `src/pyrepo_check/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_compatibility.py`
- Test: `tests/test_reporting.py`
- Test: `tests/test_execution.py`

**Interfaces:**

- Add public argparse `--format {terminal,json}` with default `terminal` only
  in this atomic CLI-wiring task.
- CLI flow becomes parse → config/facts/plan → execute → build one report →
  render selected projection → write → select report exit.
- Semantic planning errors build `PlanningErrorReportV1` and run no commands.
- A `ValueError` raised while loading config maps to
  `invalid_project_config`; typed planner failures retain their code/hint;
  unexpected `Exception` from config loading, facts collection, or planning
  maps to `internal_planning_error`. `KeyboardInterrupt`/`SystemExit` and
  non-`OSError` execution-runner exceptions propagate.

- [ ] **Step 1: Write failing public syntax and terminal integration tests**

First update the complete help snapshot to contain:

```text
usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen]
                    [--format {terminal,json}]
                    [checks ...]
```

and:

```text
  --format {terminal,json}
                        Output terminal diagnostics or one JSON document.
```

Assert `parse_args([]).format == "terminal"` and
`parse_args(["--format", "json", "ty"]).format == "json"`. Conventional
argparse invalid choices remain text on stderr with exit `2`.

Prove:

- default terminal calls retain inherited output and current banners;
- the summary appears only after the final runner call;
- all-pass, ordinary-failure, signal, and spawn-continuation summaries/exit
  codes match the report;
- `FileNotFoundError` on one check no longer escapes and later checks run;
- injected `ValueError` still escapes unchanged;
- terminal planning error goes only to stderr, includes a non-null hint when
  available, returns `2`, and spawns nothing;
- argparse help/invalid syntax remains argparse-owned text with exits `0/2`.

- [ ] **Step 2: Write failing JSON integration tests**

Use an injected runner whose captured stdout/stderr contain arbitrary braces,
newlines, JSON fragments, Unicode, CSI, and OSC. Use `capsysbinary` and assert:

```python
captured = capsysbinary.readouterr()
payload = json.loads(captured.out.decode("utf-8"))
assert captured.out.endswith(b"\n")
assert captured.err == b""
```

Also assert no `==>` banner exists outside JSON, both streams are inside the
matching process, and the exit code follows positive-first/report precedence.
Write JSON through `sys.stdout.buffer`, not through locale-dependent text
encoding.

For JSON planning errors, assert one complete document on stdout, empty
stderr, exit `2`, exact five top-level keys, and zero runner calls.

- [ ] **Step 3: Write failing exception-boundary tests**

Test each boundary independently by monkeypatching the named CLI dependency:

| Raised from | Exception | Expected |
| --- | --- | --- |
| `load_project_config` | `ValueError("bad config")` | `invalid_project_config` report, zero runner calls |
| `load_project_config` | `RuntimeError("config bug")` | `internal_planning_error` report |
| `collect_existing_positionals` | `RuntimeError("facts bug")` | `internal_planning_error` report |
| `plan_run` | `PlanningFailure(...)` | same typed code/message/hint |
| `plan_run` | `RuntimeError("planner bug")` | `internal_planning_error` report |
| `execute_plan`/runner | injected `ValueError` | identical exception propagates; no planning report |
| any planning dependency | `KeyboardInterrupt` or `SystemExit` | identical `BaseException` propagates |

Run the same expected planning cases in terminal and JSON where stream
selection differs. All planning cases execute zero commands.

- [ ] **Step 4: Write failing no-partial-output and cardinality tests**

Before implementing orchestration, monkeypatch `build_run_report`,
`validate_report_v1`, and `json.dumps` in separate JSON-mode tests. Each must
assert stdout is exactly empty, stderr is one fallback line, no partial `{`
exists, and exit is the first positive process code or `2` when none exists.

Add one integration-level malformed execution result for each builder
cardinality rejection: extra, duplicate, mismatched, and out-of-order
observations. Confirm each reaches the same reporting fallback with zero JSON
bytes. Missing observations are not fallback errors; they produce an ordinary
schema-valid JSON report whose selected check is
`error/missing_primary_process` and whose run is incomplete/error.

- [ ] **Step 5: Implement CLI orchestration and fallback**

Keep planning and execution exception boundaries separate. The adapter should
follow this shape:

```text
parse args
try load config
except ValueError -> invalid-config planning report
except Exception -> internal-planning report
try collect facts + plan
except PlanningFailure -> typed planning report
except Exception -> internal-planning report
execute plan (arbitrary runner exceptions still propagate)
try build report + render whole terminal string or JSON byte string
except Exception -> stderr fallback; first positive execution exit else 2
write the already-complete terminal text or JSON bytes to the selected stream
return select_exit_code(report)
```

The `except Exception` clauses deliberately do not catch `BaseException`, so
`KeyboardInterrupt` and `SystemExit` propagate. Because the load-config block
contains only `load_project_config`, its existing config `ValueError` contract
cannot accidentally catch planner/runner `ValueError`.

Use one fallback diagnostic prefix:

```text
pyrepo-check: internal reporting error: <message>
```

For terminal planning error, write the rendered planning text to stderr. For
JSON planning error, serialize fully to UTF-8 bytes and then write once to
`sys.stdout.buffer`. If planning-report construction/serialization itself
fails, write only the plain fallback to stderr and return `2`.

- [ ] **Step 6: Verify and commit**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_cli.py tests/test_compatibility.py tests/test_reporting.py \
  tests/test_execution.py -q
uv run --frozen pyrepo-check --all
git diff --check
```

Commit:

```bash
git add src/pyrepo_check/cli.py tests/test_cli.py tests/test_compatibility.py
git commit -m "feat: expose terminal and json reports"
```

---

### Task 6: Document and Independently Verify Milestone B

**Files:**

- Modify: `README.md`
- Modify: `.agents/skills/pyrepo-check/SKILL.md`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Test: complete repository

**Interfaces:**

- Document `pyrepo-check --format json <checks/targets>` as the agent-facing
  machine contract.
- Keep terminal commands and focused/strict semantics unchanged.
- Mark only Milestone B implemented after all acceptance evidence exists.

- [ ] **Step 1: Add focused documentation**

README and Agent Skill examples must distinguish:

```bash
pyrepo-check ty
pyrepo-check pytest tests/test_cli.py::test_name
pyrepo-check --format json ty
pyrepo-check --format json --all
```

Explain:

- terminal is default and streams native findings before a summary;
- JSON captures tool stdout/stderr inside one versioned document;
- focused/strict selection commands did not change;
- `pytest`/`coverage` structured top-level sections remain null until
  Milestone C, while the pytest check process is still reported;
- exit codes preserve the first positive tool failure; spawn/signal-only
  errors return `2`.

Do not document Test Shortcuts or coverage as available.

- [ ] **Step 2: Run focused and strict acceptance commands**

Run:

```bash
uv run --frozen python -m pytest tests/test_reporting.py -q
uv run --frozen python -m pytest tests/test_cli.py tests/test_execution.py -q
uv run --frozen python -m pytest -q
uv run --frozen pyrepo-check --all
uv run --frozen pyrepo-check --format json ty
git diff --check
git status --short
```

Expected:

- all tests pass;
- strict Ruff, annotations, ty, Bandit, and pytest pass;
- the JSON smoke command emits one parseable run document with
  `schema_version=1`, `kind="run"`, and a passing ty check;
- no generated artifact or unrelated file is added.

- [ ] **Step 3: Update delivery status only after verification**

Change the design status/table from Milestone B “designed; not implemented” to
“implemented and verified,” citing renderer, CLI integration, exact schema
tests, spawn/signal continuation tests, and the strict gate. Leave Milestones
C and D explicitly unimplemented.

- [ ] **Step 4: Run an independent whole-branch review**

Review the complete diff from `63654c0` for:

- exact Milestone B spec coverage;
- no C/D leakage;
- public/legacy compatibility;
- correct JSON isolation and schema nullability;
- failure/completeness/exit precedence;
- security of subprocess argument vectors and control-sequence handling;
- test quality and missing edge cases.

Address every Critical/Important finding and rerun the complete acceptance
commands. A reviewer assertion is not verification; command evidence remains
required.

- [ ] **Step 5: Commit documentation/status**

```bash
git add README.md .agents/skills/pyrepo-check/SKILL.md \
  docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "docs: document agent report output"
```

## Acceptance Checklist

- [ ] Terminal is still the default and native diagnostics stream unchanged.
- [ ] Terminal adds one deterministic post-run summary.
- [ ] JSON stdout is exactly one parseable document plus newline.
- [ ] JSON contains captured/sanitized/truncated tool output and exact omission
  counts.
- [ ] Planning errors are typed, execute nothing, and render correctly in both
  formats.
- [ ] Positive validation failures are complete/failed; spawn, signal, and
  missing process evidence are incomplete/error.
- [ ] Later checks run after ordinary failures, signals, and `OSError` spawn
  failures.
- [ ] First positive process exit/report-status exit precedence is proven.
- [ ] Producer schema validation and no-partial-JSON fallback are proven.
- [ ] Direct focused checks, targets, pytest nodes, strict aggregate commands,
  frozen mode, and legacy runner facade remain covered.
- [ ] Top-level `pytest` and `coverage` remain explicitly null in B.
- [ ] No shortcut, coverage, plugin, dependency, or unrelated file leaks into
  the milestone.
- [ ] Focused tests, full pytest, strict gate, JSON smoke, diff check, clean
  status, and independent review all pass before B is called implemented.
