# Agent-first quality reporting and focused test execution

**Status:** Draft for user review
**Date:** 2026-08-24
**Related context:** [`CONTEXT.md`](../../../CONTEXT.md)
**Research:** [`2026-08-24-agent-guidance-metrics.md`](../../research/2026-08-24-agent-guidance-metrics.md)

## Problem

`pyrepo-check` already provides useful focused checks and a strict aggregate
gate, but the rules that turn CLI arguments and project configuration into an
ordered run are split across the CLI, configuration, and runner modules. Tests
repeat subprocess-shaped fakes and assert internal command wiring. Adding
coverage, structured agent output, or reusable test selections directly to
that structure would increase the coupling.

Agents are the primary users. A run must therefore identify the exact failing
check or missing evidence, preserve fast focused feedback, and provide a
stable machine-readable result. It must not replace actionable evidence with a
single score.

## Goals

1. Concentrate run-selection, target, ordering, and command rules in one pure
   planning boundary.
2. Isolate process execution behind a recording-friendly internal seam.
3. Render one Agent Report as normal terminal output or versioned JSON.
4. Preserve direct check, file, directory, and pytest-node focused runs.
5. Add project-defined Test Shortcuts for repeatable pytest subsets.
6. Add line and branch Coverage Guidance with exact missing lines and branches.
7. Report the slowest tests and skip, xfail, and xpass evidence.
8. Preserve the existing strict Ruff, annotation, `ty`, Bandit, and pytest
   behavior while the architecture is extracted.

## Non-goals

- A dynamic plugin architecture or one class per check.
- Normalizing every Ruff, `ty`, or Bandit diagnostic into a common finding
  schema in this version; their raw output remains available per check.
- Changed-code coverage until the Git comparison-base contract is designed.
- Complexity scoring, mutation testing, automatic flaky-test retries, or
  repeated-run flaky classification.
- Dependency vulnerability auditing in the ordinary development loop. That is
  a separate scheduled feature.
- Staging, approval, deployment, rollback, or remote-service orchestration.

## Considered approaches

### Selected: deep planning, execution, and reporting boundaries

The CLI becomes a thin adapter. A pure planner owns repository-quality policy,
an executor owns subprocess behavior, and report renderers own human and agent
output. This gives one test surface for run policy and two justified adapters:
real subprocess execution and deterministic recording in tests.

### Rejected: extend the current CLI and runner directly

This produces the smallest immediate diff but makes every new feature cross
argument disambiguation, configuration, command construction, execution, and
CLI tests again.

### Rejected: plugin or class per check

The check set is intentionally small and fixed. Per-check classes, factories,
and adapters would add navigation and extension machinery without removing
meaningful policy.

## Compatibility contract

Phase 1 must preserve all current observable behavior:

- Check names: `ruff`, `annotations`, `annotations-fix`, `ty`, `bandit`, and
  `pytest`.
- No arguments behaves like a target-free `--all`.
- A target-only invocation selects Ruff, annotations, `ty`, and Bandit.
- `pytest <file-or-node>` forwards direct pytest targets unchanged.
- `--all <target>` runs every non-mutating check against that target and is a
  Focused Run, not repository-wide completion evidence.
- Target-free `--all` uses strict repository-root behavior for the existing
  file-oriented checks and runs the complete pytest suite.
- Check order remains Ruff, annotations, `ty`, Bandit, then pytest.
- `annotations-fix` remains opt-in and is never part of `--all`.
- Once execution starts, a failed check does not hide later diagnostics.
- Existing `uv run` and `--frozen` selection remains unchanged.

Phase 2 may add a compact terminal summary, but existing tool diagnostics and
exit semantics remain available. Phase 3 may instrument pytest for reporting
and coverage, but must preserve pytest selection and run it only once.

## Final architecture

The dependency direction is:

```text
CLI adapter -> configuration + planner -> run plan
                                        -> executor -> check results
                                                    -> Agent Report
                                                    -> terminal or JSON renderer
```

### `cli.py`: input and output adapter

The CLI parses syntax into a `RunRequest`, loads project configuration, asks
the planner for a `RunPlan`, executes it, and renders the resulting
`AgentReport`. It does not decide target defaults, check ordering, strict-gate
policy, Test Shortcut expansion, or tool command shapes.

### `config.py`: project facts

`ProjectConfig` retains existing focused Ruff/Bandit targets and frozen-mode
detection. It adds validated Test Shortcut definitions and detects whether the
native Coverage.py configuration has the minimum fields required by this
feature. It does not reinterpret Coverage.py exclusions or thresholds.

### `planning.py`: repository-quality policy

The pure planner accepts `RunRequest` plus `ProjectConfig` and returns an
ordered `RunPlan`. It owns:

- check-name and target disambiguation;
- focused versus strict-aggregate classification;
- target precedence and strict repository-root behavior;
- aggregate ordering and mutating-check exclusion;
- Test Shortcut expansion and conflicts;
- whether pytest is plain or coverage-instrumented;
- exact tool command construction; and
- expected structured artifacts.

Plan types live with the planner rather than in a generic models module.

### `execution.py`: side effects and outcomes

The executor owns subprocess invocation, working directory, duration
measurement, stdout/stderr policy, continue-after-failure behavior, and exit
aggregation. Every command is an argument vector executed without a shell.
Production uses a subprocess adapter. Tests use one recording adapter rather
than repeating raw `CompletedProcess` fakes.

Terminal mode streams tool output so diagnostics remain readable as checks run.
JSON mode captures tool output so stdout contains exactly one JSON document;
captured text has ANSI control sequences removed. Structured artifacts are read
after their producing command.

### `reporting.py`: one result, two projections

Reporting constructs the Agent Report from the plan and execution results. The
terminal and JSON renderers are projections of the same result. This module
owns schema versioning, priority ordering, coverage summarization, test-health
summarization, and rendering failures.

### `_pytest_report_plugin.py`: structured pytest evidence

`pyrepo-check` ships one standalone plugin source file. It imports only the
standard library and the pytest installed in the consumer project; it never
imports `pyrepo_check`.

For each selected pytest run, the executor first creates an owner-only
temporary directory outside the consumer repository. Before loading the
plugin, it runs a `pytest_preflight` under the consumer interpreter. Its `-c`
probe uses Python 3.7-compatible syntax, reads `sys.version_info` before
importing pytest, and emits a small JSON record. A consumer below Python 3.9
produces a synthetic `PytestResult` with `unsupported_python`; missing or
out-of-range pytest produces `module_unavailable` or `unsupported_version`. No
plugin is loaded and no tests run in those cases.

After a successful preflight, the executor copies the plugin under a fresh
Python-identifier module name, appends that directory to the existing
`PYTHONPATH`, and supplies the artifact path through
`PYREPO_CHECK_PYTEST_JSON`. It then preserves native module execution:

```text
uv run [--frozen] python -m pytest -p <unique_plugin> <pytest arguments>
```

Coverage mode likewise preserves `-m pytest`:

```text
uv run [--frozen] python -m coverage run <coverage options> \
  -m pytest -p <unique_plugin> <pytest arguments>
```

Appending rather than prepending the isolated directory preserves the
consumer repository as `sys.path[0]`, retains the existing `PYTHONPATH`, and
does not expose the globally installed tool package inside the consumer's
environment. The random module name prevents shadowing consumer modules. A
script adapter that calls `pytest.main` is rejected because script execution
would change `sys.path[0]` and module-startup semantics.

This boundary follows [Python's `-m` path semantics](https://docs.python.org/3/using/cmdline.html),
[pytest's documented plugin loading and report hooks](https://docs.pytest.org/en/8.4.x/reference/reference.html),
and [Coverage.py's `run -m` behavior](https://coverage.readthedocs.io/en/7.15.2/commands/cmd_run.html).

The plugin uses pytest's supported collection, deselection, runtest-report,
and session-finish hooks. It atomically writes a versioned artifact containing
the target pytest version, exit code, collection/session state, and per-node
setup/call/teardown reports. Missing, malformed, schema-invalid, or
non-finalized artifacts are evidence errors; the artifact pytest version must
equal the trusted preflight version. Version 1 supports pytest
`>=8,<9` in consumer Python `>=3.9`. The standalone source uses only Python 3.9
grammar and standard-library APIs because the preflight prevents it from being
loaded on an unsupported interpreter.

The plugin records execution outcome separately from expected-failure
metadata. In pytest 8, strict XPASS is exactly a call-phase report with
`outcome == "failed"`, no `wasxfail`, and the string form of `longrepr`
beginning `[XPASS(strict)] `; the suffix is its reason, or null when empty.
Non-strict XPASS is exactly a passed call with string `wasxfail`. XFAIL has
exactly one skipped setup, call, or teardown report with string `wasxfail`,
covering imperative `pytest.xfail()` and `xfail(run=False)` as well as call-time
marks. Multiple `wasxfail` reports, non-string metadata, or metadata on another
shape are `artifact_invalid`. This is the only narrow, version-gated
representation shim; the plugin does not parse terminal output or import pytest
private modules. The slow-test duration is the sum of recorded setup, call, and
teardown time.

Version 1 supports one pytest process only. It explicitly detects active
pytest-xdist with a `pytest.hookimpl(optionalhook=True)`
`pytest_xdist_setupnodes(config, specs)` hook:
non-empty worker specs finalize `unsupported_parallelism` before workers start,
while `-n 0` and merely having xdist installed remain allowed. Other
third-party multi-worker runners are outside the supported contract. Observable
worker metadata, multiple artifact-writer identities, duplicated session
lifecycles, or coverage shards invalidate evidence, but the tool does not claim
proactive detection of every arbitrary runner. This restriction applies to
plain structured pytest evidence and coverage.

Repeated reports for the same `(nodeid, setup|call|teardown)` phase, including
consumer retry plugins, are unsupported in version 1. They invalidate the
artifact rather than being overwritten, double-counted, or flattened into a
pass. Any report outcome outside pytest's core `passed`, `failed`, and `skipped`
values, including a plugin's `rerun`, is also `unsupported_retries`. Likewise,
an artifact exit code that differs from the actual subprocess exit code is
invalid; the subprocess result is authoritative. This reporting adapter is an
execution detail, not a general plugin system.

These rules reject observable retry protocols. A third-party
`pytest_runtest_protocol` implementation that suppresses every intermediate
report and exposes only a final attempt is outside the version-1 contract; the
tool does not claim to detect hidden attempts for arbitrary plugins. Attempt-
aware retry support requires a later schema that preserves every attempt.

The plugin records pytest's final effective arguments with an outer,
`tryfirst` hook wrapper around
`pytest_load_initial_conftests(early_config, parser, args)`. It copies `args`
after every inner implementation and post-yield mutation has completed, then
removes only the exact tool-owned `-p <unique_plugin>` pair. The list therefore
includes configured `addopts`, `PYTEST_ADDOPTS`, invocation arguments, and
public hook mutations in pytest's
[documented option composition](https://docs.pytest.org/en/8.4.x/example/simple.html#how-to-change-command-line-options-defaults).
Before collection, it also snapshots the final semantic values of pytest's
core selector/narrowing options so a plugin that mutates `config.option`
without changing `args` cannot hide selection: collection paths plus `keyword`,
`markexpr`, `deselect`, `ignore`, `ignore_glob`, `lf`, `pyargs`, `collectonly`,
`setuponly`, and `setupplan`.
For a planned complete suite, these core options are scope-neutral in version
1: `-r<chars>`, repeated `-q`/`--quiet`, repeated `-v`/`--verbose`,
`--tb=<style>`, `-l`/`--showlocals`/`--no-showlocals`, `--color=<value>`,
`--code-highlight=<value>`, `-s`/`--capture=<method>`,
`--disable-warnings`, `--strict-config`, `--strict-markers`,
`--durations=<count>`, and `--durations-min=<seconds>`. Their separated
operand forms are also accepted where pytest supports them.

Every positional collection argument, known selector or narrowing option
(`-k`, `-m`, `--deselect`, `--ignore`, `--ignore-glob`, `--lf`,
`--last-failed`, `--pyargs`, `--collect-only`, `--setup-only`, or
`--setup-plan`), and every external option outside that frozen scope-neutral
set is conservative partial evidence. Any `pytest_deselected` event is also
partial. An outer collection-modification wrapper records item identities
before and after other hooks and the identities supplied to
`pytest_deselected` during that wrapped call. `collection_reduced` means exactly
the removed identities not covered by those deselection events.
This makes unknown third-party options and collection filters fail gate
eligibility instead of being guessed safe; native `testpaths` and static
pre-collection exclusions remain the project's canonical suite definition.

## Run request and planning contract

A `RunRequest` carries only user intent:

- selected check names;
- direct targets;
- `--all`;
- `--no-frozen`;
- output format (`terminal` or `json`);
- optional Test Shortcut name; and
- whether coverage was explicitly requested.

The planner produces immutable planned commands with the owning check name,
argument vector, working directory, expected artifacts, and whether the
command contributes test or coverage evidence.

`mode` is `strict_aggregate` only for no-check or target-free `--all`; every
other invocation is `focused`. `planned_test_scope` is `not_selected` without
pytest, `partial` when pyrepo-check supplies a target or Test Shortcut, and
`complete` when it supplies no pytest selector. This is an intent claim;
effective pytest options and collection can reduce the observed
`PytestResult.scope`. Native pytest configuration remains project-owned, and
effective selectors plus deselected counts stay visible in test evidence.

`planned_coverage_scope` is `not_requested` when coverage is irrelevant,
`unavailable` when aggregate coverage would apply but native configuration is
absent, and `partial` or `complete` when coverage is planned. This planning
claim is distinct from the observed scope in `CoverageResult`. These fields
are orthogonal: an explicit pytest-only Focused Run can have complete planned
test and coverage scope without becoming threshold-eligible.

The CLI adds:

```text
--format terminal|json
--shortcut NAME
--coverage
```

Terminal remains the default. Supported examples include:

```bash
pyrepo-check ty
pyrepo-check pytest tests/test_cli.py::test_name
pyrepo-check pytest --shortcut unit
pyrepo-check pytest --shortcut unit --coverage
pyrepo-check --format json ty
```

`--shortcut` is valid only with an explicit pytest-only Focused Run. It is
invalid with direct targets, multiple selected checks, or `--all`. `--coverage`
is valid when pytest is explicitly selected or when existing no-check/`--all`
semantics select the aggregate run. A target or Test Shortcut makes its
coverage partial; `pyrepo-check --coverage` is therefore a target-free strict
aggregate run.

## Test Shortcut configuration

Test Shortcuts are project-owned names for repeatable pytest subsets:

```toml
[tool.pyrepo-check.test-shortcuts]
unit = ["tests/unit"]
integration = ["-m", "integration"]
cli = ["tests/test_cli.py"]
```

Names must match `[a-z][a-z0-9_-]*`. Values must be non-empty lists of strings.
The version-1 token grammar is:

```text
shortcut := item+
item     := test_target | "-m" expression | "-k" expression
```

A shortcut may contain zero or more test targets, at most one `-m` pair, and
at most one `-k` pair, in any order. The planner preserves token order. A
selector flag must have one following non-empty expression. At least one test
target or selector is required.

Test targets are project-relative files, directories, or pytest node IDs. The
path portion before the first `::` must exist beneath the project root. Absolute
paths, `..` escapes, tokens beginning with `-`, `--`, repeated selectors, and
missing selector operands are invalid. Shell syntax, environment assignments,
output plugins, early-exit flags, and arbitrary pytest options are therefore
not part of this grammar. This keeps a shortcut portable and prevents it from
suppressing required evidence.

An unknown shortcut fails during planning, lists available names, and runs no
commands. The report includes the shortcut name and exact expanded pytest
arguments.

## Coverage contract

### Supported version and native configuration

Version 1 supports Coverage.py `coverage[toml]>=7.15,<8` in consumer Python
`>=3.10`, matching the package's
[published Python support](https://coverage.readthedocs.io/en/7.15.2/).
Plain structured pytest evidence retains its Python `>=3.9` floor. The tool
installation remains isolated and does not make Coverage.py available to
`uv run` inside the consumer project.

Coverage settings belong to Coverage.py in `pyproject.toml`. The minimum is:

```toml
[tool.coverage.run]
branch = true
source = ["src/package_name"]

[tool.coverage.report]
# fail_under = 90  # optional; strict aggregate gate only
```

`branch = true` is required. At least one non-empty string must be configured
across native `source`, `source_pkgs`, or `source_dirs`. `show_missing` is not
required because JSON already provides missing lines and branches.

Native `omit`, exclusions, paths, and precision remain effective. Version 1 is
single-process: non-empty `concurrency` or `patch` settings, or
`parallel = true`, are invalid coverage configuration. Only output locations
are overridden: run data and JSON belong to the current run's temporary
directory. Explicit coverage with absent or invalid minimum configuration is a
planning error. Aggregate coverage activates automatically when the minimum is
valid; a completely absent coverage configuration preserves plain pytest and
adds a setup advisory. A partially present, invalid, or parallel coverage
configuration fails planning rather than being silently ignored.

The executor checks that the consumer environment contains a supported
Coverage.py version before starting coverage. Missing or unsupported coverage
is an execution error and is never reported as successful plain pytest.
The preflight runs consumer Python with an argument-vector `-c` probe that
emits the Python version and, on supported Python, imports `coverage` and emits
`coverage.__version__`. Consumer Python below 3.10 is
`CoverageError.unsupported_python`; only a stable Coverage.py release in the
supported range proceeds. Its stdout/stderr and result are recorded as the
`coverage_preflight` process.

### Collection and single-process data

The coverage pytest command is an argument vector equivalent to:

```text
uv run [--frozen] python -m coverage run \
  --rcfile=<absolute consumer pyproject.toml> \
  --data-file=<absolute run temp>/.coverage \
  -m pytest -p <unique_plugin> <pytest arguments>
```

For `pytest_preflight`, `coverage_preflight`, the primary collection process,
and `coverage_json`, the executor removes inherited `COVERAGE_PROCESS_START`
and `COVERAGE_PROCESS_CONFIG` so a consumer
[startup hook](https://coverage.readthedocs.io/en/latest/api_module.html#coverage.process_startup)
cannot begin nested measurement. It then points `COVERAGE_FILE` and
`COVERAGE_RCFILE` only to run-owned paths. Pytest executes exactly once.

After pytest exits, the executor inspects only the current run directory; it
does not use a shell glob or inspect consumer-owned coverage files. The exact
base `.coverage` path must be one readable regular file. A missing, malformed,
or symlinked base is a coverage evidence error. Any `.coverage.*` shard is
`unexpected_parallel_data` and invalidates the evidence; version 1 never runs
Coverage.py `combine`.

Before reporting, the executor copies the validated base bytes into a newly
created empty `report-input` subdirectory and verifies that the copy has the
same SHA-256 digest. It scans both the run root for `.coverage.*` and the
snapshot directory for `coverage-data.*` immediately before reporting.
Coverage JSON reads only that uniquely named snapshot. This isolates
Coverage.py 7.14+'s
[reporting-time parallel-data discovery](https://coverage.readthedocs.io/en/7.15.2/commands/cmd_combine.html)
from the validated base. After JSON generation, the executor scans both shard
namespaces again and verifies that both the original base and snapshot digests
are unchanged. `--keep-combined` ensures a snapshot-adjacent shard cannot be
silently consumed and deleted. Any shard or digest change is
`unexpected_parallel_data`. Detached
producers or remote workers are outside the supported contract; the evidence
guarantee ends at this final validation after the sole supported pytest process
has exited. A later write by an unsupported detached process may be removed by
cleanup and is not claimed to be detected. Coverage is read only through
documented Coverage.py reporting commands; the SQLite data file is never
parsed directly.

### JSON and threshold matrix

Coverage JSON is generated in the run directory with an argument vector
equivalent to:

```text
uv run [--frozen] python -m coverage json \
  --rcfile=<absolute consumer pyproject.toml> \
  --data-file=<absolute run temp>/report-input/coverage-data \
  -o <absolute run temp>/coverage.json \
  --keep-combined \
  [--fail-under=0]
```

Threshold eligibility requires all of:

- a target-free Strict Aggregate Gate;
- pytest exit code `0`;
- a finalized, valid pytest artifact;
- a complete pytest session;
- observed `PytestResult.scope == "complete"` with no scope reasons;
- successful single-process coverage collection; and
- valid coverage JSON.

When eligible, `--fail-under=0` is omitted so native `fail_under` and precision
semantics apply. Every focused, target-bearing, failed, interrupted, or
otherwise incomplete run uses `--fail-under=0`; coverage remains guidance.

| Pytest/evidence state | Coverage JSON | Coverage status | Threshold |
| --- | --- | --- | --- |
| Eligible strict aggregate | Native `fail_under` | `passed`, or `failed` when exit `2` accompanies valid JSON | Evaluated when configured |
| Focused or target-bearing | `--fail-under=0` | `guidance` | Skipped: focused or partial |
| Complete pytest exit `1` | `--fail-under=0` | `guidance` | Skipped: pytest failed |
| Interrupted/incomplete or exit `2`, `3`, or `4` | `--fail-under=0` when data exists | `guidance` or `error` | Skipped: pytest incomplete |
| Pytest exit `5` | `--fail-under=0` when data exists | `guidance` or `error` | Skipped: no tests collected |
| Missing version/data, unexpected parallel data, or invalid JSON | None or failed command | `error` | Skipped: evidence error |

For an eligible strict run, JSON exit `2` plus a valid artifact means only
`threshold_not_met`. Any other nonzero exit, exit `2` in a non-eligible run, or
missing/malformed JSON is an evidence error. The JSON `meta.version` must equal
the trusted coverage preflight version. Pytest status remains independent:
passed tests can coexist with a failed coverage threshold, and failed tests can
coexist with valid coverage guidance. The command and exit classification use
Coverage.py's [JSON reporting contract](https://coverage.readthedocs.io/en/7.15.2/commands/cmd_json.html).

### Guidance and deterministic ranking

For each measured production file, report:

- covered and missing statement counts;
- covered and missing branch counts;
- exact missing line numbers; and
- exact missing branch arcs.

`missing_opportunities` is exactly `missing_statements + missing_branches`.
Files sort by that value descending, then normalized project-relative path
ascending. Missing lines sort numerically. Missing arcs sort by source line,
then destination line; negative arc values are retained because Coverage.py
uses them for code-object entry or exit. Percentages are terminal headers only;
JSON keeps exact integer counts.

Execution mode, coverage scope, and threshold eligibility are independent. A
pytest-only Focused Run can execute the complete test suite and produce
complete-scope guidance without becoming strict-gate evidence.

### Temporary artifacts and cleanup

One owner-only OS temporary directory per invocation contains only the unique
pytest plugin, pytest JSON, `.coverage`, any rejected `.coverage.*` shards, the
validated `report-input/coverage-data` snapshot, and coverage JSON. It is
outside the consumer root, so the feature needs no consumer `.gitignore`
entry.

After artifacts are parsed into the Agent Report, the executor removes the
exact directory in a `finally` path. Cleanup failure is an execution error and
makes the run incomplete. An uncatchable process kill may leave an OS-temp
directory; later runs never reuse or broadly scavenge it. No cleanup operation
may target the consumer root, a glob, or a directory not created by the current
invocation.

## Agent Report contract

JSON mode writes one UTF-8 JSON document followed by one newline to stdout.
Tool output is contained inside that document; no banner or diagnostic is
written beside it. The version-1 document is a discriminated union:

```text
AgentReportV1 = PlanningErrorReportV1 | RunReportV1
```

A planning failure has no synthetic run plan or `not_run` check entries:

```json
{
  "schema_version": 1,
  "kind": "planning_error",
  "overall_status": "error",
  "complete": false,
  "error": {
    "code": "unknown_test_shortcut",
    "message": "Unknown Test Shortcut: smoke",
    "hint": "Available Test Shortcuts: cli, integration, unit"
  }
}
```

A run report has this top-level shape:

```json
{
  "schema_version": 1,
  "kind": "run",
  "project_root": "/absolute/project/root",
  "mode": "focused",
  "overall_status": "failed",
  "complete": true,
  "selection": {
    "checks": ["ty"],
    "targets": [],
    "test_shortcut": null,
    "pytest_args": null,
    "planned_test_scope": "not_selected",
    "planned_coverage_scope": "not_requested"
  },
  "checks": [
    {
      "name": "ty",
      "status": "failed",
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--frozen", "python", "-m", "ty", "check"],
          "cwd": "/absolute/project/root",
          "outcome": "exited",
          "exit_code": 1,
          "signal": null,
          "duration_ms": 412,
          "stdout": {
            "captured": true,
            "text": "",
            "truncated": false,
            "omitted_bytes": 0
          },
          "stderr": {
            "captured": true,
            "text": "",
            "truncated": false,
            "omitted_bytes": 0
          },
          "error_message": null
        }
      ],
      "error": null
    }
  ],
  "pytest": null,
  "coverage": null,
  "advisories": []
}
```

### Normative version-1 types

All members shown below are required. Nullable members are present with JSON
`null`; they are not omitted. Counts and durations are non-negative integers,
and JSON numbers are finite.

```text
PlanningErrorReportV1 {
  schema_version: 1
  kind: "planning_error"
  overall_status: "error"
  complete: false
  error: PlanningError
}

RunReportV1 {
  schema_version: 1
  kind: "run"
  project_root: string
  mode: "focused" | "strict_aggregate"
  overall_status: "passed" | "failed" | "error"
  complete: boolean
  selection: Selection
  checks: CheckResult[]
  pytest: PytestResult | null
  coverage: CoverageResult | null
  advisories: Advisory[]
}

Selection {
  checks: CheckName[]
  targets: string[]
  test_shortcut: string | null
  pytest_args: string[] | null
  planned_test_scope: "not_selected" | "partial" | "complete"
  planned_coverage_scope:
    "not_requested" | "unavailable" | "partial" | "complete"
}

CheckName =
  "ruff" | "annotations" | "annotations-fix" | "ty" | "bandit" | "pytest"

CheckResult {
  name: CheckName
  status: "passed" | "failed" | "error"
  processes: ProcessResult[]
  error: CheckError | null
}

ProcessResult {
  role: "primary" | "pytest_preflight" | "coverage_preflight" | "coverage_json"
  argv: string[]
  cwd: string
  outcome: "exited" | "signaled" | "spawn_failed"
  exit_code: integer | null
  signal: integer | null
  duration_ms: integer
  stdout: CapturedText
  stderr: CapturedText
  error_message: string | null
}

CapturedText {
  captured: boolean
  text: string
  truncated: boolean
  omitted_bytes: integer
}

CheckError {
  code:
    "spawn_failed"
    | "terminated_by_signal"
    | "pytest_preflight_failed"
    | "pytest_evidence_error"
    | "coverage_preflight_failed"
    | "missing_primary_process"
    | "cleanup_failed"
  message: string
}

PytestResult {
  status: "passed" | "failed" | "error"
  complete: boolean
  scope: "partial" | "complete"
  scope_reasons: TestScopeReason[]
  pytest_version: string | null
  exit_code: integer | null
  evidence: PytestEvidence | null
  error: PytestError | null
}

PytestEvidence {
  effective_args: string[]
  collected: integer
  deselected: integer
  counts: PytestCounts
  collection_errors: CollectionIssue[]
  collection_skips: CollectionIssue[]
  slowest: SlowTest[]
  special_outcomes: SpecialTestOutcome[]
}

TestScopeReason =
  "planned_selector"
  | "effective_narrowing_option"
  | "unclassified_external_option"
  | "deselected_tests"
  | "collection_reduced"
  | "incomplete_session"

PytestCounts {
  passed: integer
  failed: integer
  errors: integer
  skipped: integer
  xfailed: integer
  xpassed: integer
}

CollectionIssue {
  nodeid: string
  message: string
}

SlowTest {
  nodeid: string
  duration_ms: integer
}

SpecialTestOutcome {
  nodeid: string
  outcome: "skipped" | "xfailed" | "xpassed"
  reason: string | null
  strict: boolean | null
  affects_exit: boolean
  duration_ms: integer
}

PytestError {
  code:
    "unsupported_python"
    | "module_unavailable"
    | "unsupported_version"
    | "preflight_invalid"
    | "unsupported_parallelism"
    | "unsupported_retries"
    | "exit_code_mismatch"
    | "not_started"
    | "spawn_failed"
    | "terminated_by_signal"
    | "artifact_missing"
    | "artifact_invalid"
    | "artifact_not_finalized"
    | "session_incomplete"
    | "interrupted"
    | "internal_error"
    | "usage_error"
    | "unknown_exit_code"
  message: string
}

CoverageResult {
  status: "passed" | "failed" | "guidance" | "error"
  scope: "partial" | "complete"
  evidence_complete: boolean
  coverage_version: string | null
  gate_eligible: boolean
  threshold: CoverageThreshold
  totals: CoverageTotals | null
  files: CoverageFile[]
  error: CoverageError | null
}

CoverageThreshold {
  configured: boolean
  value: number | null
  evaluated: boolean
  passed: boolean | null
  skipped_reason:
    null
    | "evidence_error"
    | "not_configured"
    | "focused_run"
    | "partial_run"
    | "pytest_failed"
    | "pytest_incomplete"
    | "no_tests_collected"
}

CoverageTotals {
  statements: CoverageCounts
  branches: CoverageCounts
}

CoverageCounts {
  covered: integer
  missing: integer
}

CoverageFile {
  path: string
  statements: FileStatementCoverage
  branches: FileBranchCoverage
}

FileStatementCoverage {
  covered: integer
  missing: integer
  missing_lines: integer[]
}

FileBranchCoverage {
  covered: integer
  missing: integer
  missing_arcs: [integer, integer][]
}

CoverageError {
  code:
    "unsupported_python"
    | "module_unavailable"
    | "unsupported_version"
    | "preflight_invalid"
    | "spawn_failed"
    | "terminated_by_signal"
    | "unsupported_parallelism"
    | "data_missing"
    | "unexpected_parallel_data"
    | "generation_failed"
    | "artifact_missing"
    | "artifact_invalid"
  message: string
}

PlanningError {
  code:
    "invalid_arguments"
    | "invalid_project_config"
    | "unknown_check"
    | "unknown_target"
    | "unknown_test_shortcut"
    | "invalid_test_shortcut"
    | "coverage_configuration_required"
    | "internal_planning_error"
  message: string
  hint: string | null
}

Advisory {
  code:
    "coverage_not_configured"
    | "coverage_threshold_not_applied"
    | "missing_test_reason"
    | "output_truncated"
  message: string
  hint: string | null
}
```

`kind` is the discriminator. A planning error has no `selection`, `checks`,
`pytest`, or `coverage` because no valid plan exists. A run report has no
top-level error envelope; the check, pytest, or coverage result that owns an
execution/evidence error contains it.

`checks` contains selected checks in planned order. A standard check has one
`primary` process. A pytest check records `pytest_preflight` before its primary;
coverage instrumentation additionally records `coverage_preflight` before the
primary and `coverage_json` afterward. The process array contains only commands
actually attempted, in execution order, so a failed preflight has no primary.
`CheckResult.status` describes the selected validation itself: coverage
threshold/evidence status remains independent in `coverage`. Non-pytest
positive exit codes retain existing validation-failure semantics; spawn or
signal termination is `error`. `CheckResult.error` is non-null exactly when its
status is `error`.

For pytest, `CheckResult.status` normally equals `PytestResult.status`.
Preflight, launch, or pytest-artifact errors make both results `error`; a
cleanup failure makes the check `error` even when the already-finalized pytest
outcome remains passed or failed. Coverage data, JSON generation, and threshold
outcomes occur after the primary pytest process and affect only
`CoverageResult`, never the pytest check's status.

For each process, `exit_code` is non-null only when `outcome` is `exited`, and
`signal` is non-null only when it is `signaled`; both are null for a spawn
failure. `error_message` is null for an ordinary exit and otherwise contains a
diagnostic. A coverage preflight or temporary-cleanup failure that prevents or
invalidates pytest belongs to the pytest check. Coverage
collection/reporting failures after pytest terminates belong to the independent
coverage result.

Preflight stdout must be exactly one schema-valid JSON record. Spawn and signal
failures use matching `CheckError` and owning pytest/coverage error codes; an
ordinary nonzero exit, malformed JSON, extra output, or wrong fields use
`pytest_preflight_failed + PytestError.preflight_invalid` or
`coverage_preflight_failed + CoverageError.preflight_invalid`. A pytest or
coverage version field is non-null exactly when its corresponding preflight
returned a trusted version string, including a trusted but unsupported version;
it is null when the interpreter is unsupported, the module is unavailable, or
the preflight result itself is untrusted. Truncated preflight output is always
`preflight_invalid`; the parser never treats a retained tail as a full record.

`complete` is evidence completeness, not success. It is true only when every
planned check has an authoritative terminal result, every required structured
artifact is valid, pytest did not stop early, and cleanup completed. Ordinary
check failures, a completed pytest exit `1`, no tests collected, and a coverage
threshold failure are complete outcomes. Spawn/signals, interruption, early
pytest stop, missing/malformed artifacts, and cleanup failure make it false.

Overall status uses this precedence:

1. `error` when `complete` is false or a check, pytest, or coverage result is
   `error`;
2. otherwise `failed` when a check or pytest result is `failed`, or coverage is
   `failed`; and
3. otherwise `passed` (`guidance` and advisories do not fail a run).

### Pytest result and exit matrix

`pytest` is null exactly when pytest was not selected. Otherwise its status is
independent from coverage. A valid coverage artifact may coexist with failed
tests, and a passed pytest session may coexist with a coverage error or failed
threshold. When a spawn or coverage preflight error prevents pytest from
starting, `pytest` is still present with status `error`, `evidence: null`, and
`not_started` error.

`evidence` is non-null only when one finalized, schema-valid plugin artifact
passes writer/session cardinality and subprocess-exit reconciliation. It may
describe a valid but interrupted or early-stop session. It is null for
preflight/launch failure and every missing, malformed, non-finalized,
multi-writer, unsupported-parallelism, retry-shaped, or exit-mismatched
artifact; those paths set `complete: false` rather than inventing zero counts
or empty findings.

`scope_reasons` contains unique values in this fixed order. Membership is
exact: `planned_selector` iff `Selection.planned_test_scope` is `partial`;
`effective_narrowing_option` iff trusted final arguments or semantic option
values contain known narrowing; `unclassified_external_option` iff trusted
arguments contain an unclassified external option; `deselected_tests` iff
trusted `evidence.deselected` is positive; `collection_reduced` iff the trusted
collection wrapper observed removed item identities not covered by a
`pytest_deselected` event; and `incomplete_session` iff
`PytestResult.complete` is false. When evidence is null, only planner-known
`planned_selector` plus `incomplete_session` may appear.
`PytestResult.scope` is `complete` if and only if the reason array is empty;
otherwise it is `partial`. Thus a target-free plan cannot claim complete test
evidence when config or `PYTEST_ADDOPTS` adds `-k`/`-m`, a path, an
ignore/deselect option, an unknown external option, or a plugin deselects
items.

The plugin finalizes `pytest.complete` only when collection completed, every
selected node reached a consolidated outcome, and the session did not stop
early. When complete, `evidence` is non-null and its six outcome counts sum to
`evidence.collected`. Collection errors and skips remain separate arrays;
`evidence.deselected` is not included in `evidence.collected`.

| Pytest exit | Public meaning | `pytest.status` | `pytest.error` | Completeness |
| --- | --- | --- | --- | --- |
| `0` | Tests completed successfully | `passed` | null | True only with finalized valid evidence and no early stop/collection error. |
| `1` | Tests failed | `failed` | null, or `session_incomplete` after early stop | True when all selected tests completed; false after early stop. |
| `2` | Interrupted | `error` | `interrupted` | False. |
| `3` | Pytest internal error | `error` | `internal_error` | False. |
| `4` | Pytest usage error | `error` | `usage_error` | False. |
| `5` | No tests collected | `failed` | null | True with a finalized artifact recording zero collected nodes. |
| Any other non-negative value | Unknown pytest exit | `error` | `unknown_exit_code` | False. |
| No exit code | Spawn failure or signal | `error` | `spawn_failed` or `terminated_by_signal` | False. |

A missing, invalid, or non-finalized artifact overrides exits `0`, `1`, and `5`
to pytest `error` and incomplete evidence.

`PytestResult.error` is null for an authoritative, complete exit `0`, `1`, or
`5`. It is non-null for every `error` status and for an exit `1` whose early
stop makes evidence incomplete; that early-stop case uses `session_incomplete`
while retaining failed-test status.

When multiple pytest defects are observable, the single error code uses this
precedence: preflight-specific error; spawn/signal; artifact missing;
non-finalized artifact; schema/writer/expected-failure `artifact_invalid`;
`unsupported_parallelism`; `unsupported_retries`; `exit_code_mismatch`; then
the exit-matrix error. Later diagnostics remain in process output, but do not
replace the higher-precedence code.

`ProcessResult.exit_code` from the primary pytest subprocess is authoritative
and is copied to `PytestResult.exit_code`. The plugin artifact's session exit
code must match it. A mismatch, including one caused by a later
`pytest_sessionfinish` hook changing the exit status, is `exit_code_mismatch`,
invalidates the artifact, and makes pytest incomplete/error.

Node outcome is separate from expected-failure metadata. Consolidation uses
this precedence: failed setup or teardown is `errors`; strict XPASS is
`xpassed` with `strict: true` and `affects_exit: true`; ordinary call failure is
`failed`; the single valid setup/call/teardown XFAIL is `xfailed`; non-strict
XPASS is `xpassed` with `affects_exit: false`; an ordinary skip in any phase is
`skipped`; otherwise the node is `passed`. Thus a teardown failure remains an
error even after an earlier XFAIL. Pytest's exit code remains authoritative for
whether XPASS fails the session. The matrix follows pytest's
[public exit codes](https://docs.pytest.org/en/8.4.x/reference/exit-codes.html)
and [strict-XPASS contract](https://docs.pytest.org/en/8.4.x/how-to/skipping.html#strict-parameter).

`evidence.slowest` contains at most ten terminal nodes, sorted by total duration
descending then node ID ascending. `evidence.special_outcomes` contains every
skipped, xfailed, and xpassed node sorted by node ID. A missing/empty reason is
`null` and adds a `missing_test_reason` advisory; it does not change status.
Repeated phase reports or non-core outcomes are `unsupported_retries`; they
invalidate the artifact, so no retry can flatten an earlier failure into a
pass.

`SpecialTestOutcome.strict` is null for an ordinary skip or XFAIL because a
failed expected test's public report does not authoritatively expose the
strictness policy. It is `false` for non-strict XPASS and `true` for strict
XPASS. `affects_exit` is true only for strict XPASS. For a complete session,
the six members of `evidence.counts` sum to `evidence.collected`.

### Coverage result

`coverage` is null exactly when `selection.planned_coverage_scope` is
`not_requested` or `unavailable`. The unavailable case also adds the
`coverage_not_configured` advisory. When coverage is planned, its status means:

- `passed`: valid complete evidence with no applied threshold failure;
- `failed`: valid eligible evidence below an applied native threshold;
- `guidance`: valid non-gating evidence from a focused, partial, failed-test,
  or incomplete-test run; and
- `error`: required coverage execution or evidence failed.

When coverage was planned and pytest reports `unsupported_parallelism` before
execution, coverage is also `error` with
`CoverageError.unsupported_parallelism`; it never reports guidance from startup
imports alone.

When multiple coverage defects are observable, the single error code uses this
precedence: preflight-specific error or spawn/signal; unsupported parallelism;
unexpected parallel data; missing data; JSON generation failure; missing JSON;
invalid JSON. Lower-precedence diagnostics remain in process output.

`threshold.value` is the native threshold exactly when `configured` is true
and is otherwise null. `evaluated` is true only when it is configured and
`gate_eligible` is true. `passed` is non-null if and only if evaluated.
`skipped_reason` is null if and only if evaluated;
otherwise exactly one reason is selected with this precedence:

1. `evidence_error`;
2. `not_configured`;
3. `no_tests_collected`;
4. `pytest_incomplete`;
5. `pytest_failed`;
6. `partial_run`; and
7. `focused_run`.

Skipping a configured threshold adds `coverage_threshold_not_applied`.

Coverage status permits only these combinations:

| Status | Scope | `evidence_complete` | `gate_eligible` | Totals / error | Threshold state |
| --- | --- | --- | --- | --- | --- |
| `passed` | `complete` | true | true | totals non-null; error null | Configured: evaluated true, passed true, reason null. Unconfigured: evaluated false, passed null, reason `not_configured`. |
| `failed` | `complete` | true | true | totals non-null; error null | Configured, evaluated true, passed false, reason null. |
| `guidance` | `partial` or `complete` | true | false | totals non-null; error null | Evaluated false, passed null, first applicable non-error skip reason. |
| `error` | `partial` | false | false | totals null; files empty; error non-null | Evaluated false, passed null, reason `evidence_error`. |

`evidence_complete` means the coverage artifact accounts for the entire test
selection that actually ran; it is independent from whether that selection was
repository-wide. Planned scope becomes observed `CoverageResult.scope` by this
rule: planned `partial` always remains `partial`; planned `complete` becomes
observed `complete` only when `PytestResult.scope` is `complete` and coverage
evidence is complete, and otherwise becomes `partial`. Planned
`not_requested` and `unavailable` produce no `CoverageResult`.
`gate_eligible` implements the exact strict-aggregate eligibility list in the
Coverage contract, regardless of whether a threshold is configured.

For `passed`, `failed`, or `guidance`, totals are non-null and `error` is null.
For `error`, totals are null, files are empty, and `error` is non-null. Coverage
percentages are calculated only for terminal display from exact integer counts.

### Paths, durations, captured output, and ordering

`project_root` and every process `cwd` are absolute
`Path.resolve(strict=False)` paths with native separators. Targets, pytest
arguments, argument vectors, and node IDs preserve their original spelling and
order. Coverage paths are project-relative with `/` separators and no leading
`./`; a measured file outside the project root invalidates the artifact.

Durations are integer milliseconds rounded to nearest, with half milliseconds
rounded upward. Process duration uses a monotonic clock around spawn through
termination; test phases are summed before rounding.

Terminal mode lets each primary check process inherit stdout/stderr exactly as
today so checks retain their TTY behavior. Those streams use `captured: false`,
empty text, no truncation, and zero omitted bytes. Machine-only
`pytest_preflight`, `coverage_preflight`, and `coverage_json` helpers are
captured in both modes so their structured output can be validated; terminal
mode renders their diagnostics only on error.

Every captured stream retains only the final 65,536 raw bytes, decodes UTF-8
with invalid sequences replaced by U+FFFD, and removes ECMA-48 CSI/OSC terminal
sequences. `omitted_bytes` is the discarded raw-byte count and is zero exactly
when `truncated` is false. JSON mode captures primary processes as well, so all
of its process streams use `captured: true`; an `output_truncated` advisory
identifies every truncated stream.

Array order is normative:

- `selection.checks` uses canonical planned check order;
- `selection.targets` and `selection.pytest_args` preserve user/config token
  order;
- checks and processes use execution order;
- collection errors and collection skips use node ID ascending, then message
  ascending;
- slow tests use duration descending then node ID ascending;
- special outcomes use node ID ascending;
- coverage files use missing opportunities descending then path ascending;
- missing lines and branch arcs use numeric ascending order; and
- advisories use code then message ascending.

String ties use Unicode code-point order. JSON member order is not semantic,
but the renderer emits members in the order defined above for deterministic
snapshots.

### Versioning

Consumers reject unsupported `schema_version`, missing required fields, wrong
types/nullability, and unknown enum values. They ignore unknown object members
so additive metadata can remain compatible. The version-1 producer emits only
the members defined here. Removing/changing a field, changing meaning or an
ordering rule, or adding an enum value requires a new schema version. Human
messages and captured output are diagnostic; consumers branch on `kind`,
status, and `code`.

### Terminal attention order

The terminal renderer preserves streamed tool diagnostics, then orders its
summary as:

1. incomplete or errored evidence;
2. failed checks, pytest, or coverage in planned order;
3. exact coverage gaps;
4. special pytest outcomes and slow nodes;
5. advisories; and
6. compact successful-check confirmation.

Existing Ruff, `ty`, and Bandit findings remain in tool output rather than
receiving a speculative common diagnostic schema.

## Error and CLI exit behavior

Semantic planning errors execute no commands. Terminal mode writes the message
and hint to stderr. JSON mode writes a complete planning-error document to
stdout. Both return CLI exit code `2`. Conventional argparse help and syntax
errors remain text because output mode may not have parsed successfully;
`--help` returns `0` and syntax errors return `2`.

After execution begins:

- completed validation/test failures are `failed`, not `error`;
- spawn failures, signals, interrupted/internally failed pytest, and missing
  required artifacts are `error`;
- later independent checks continue after an ordinary failure or per-check
  error; this spawn-error continuation is an intentional Phase 2 behavior
  change, not part of the Phase 1 compatibility refactor;
- coverage generation is attempted after a completed pytest failure when
  run-owned data exists; and
- no score or advisory overrides failure, error, or incomplete evidence.

The CLI exit code is selected after report construction:

1. if any executed process returned a positive nonzero code, return the first
   such code in planned execution order, preserving existing behavior;
2. otherwise return `1` when overall status is `failed`;
3. otherwise return `2` when overall status is `error`; and
4. otherwise return `0`.

JSON serialization completes into memory before stdout is written. Construction
or serialization failure emits no JSON bytes, writes one plain fallback error
to stderr, and uses the exit selection above. Failure while writing an already
serialized document is outside the no-partial-document guarantee.

## Testing strategy

Development is test-first. Each behavior moves only after a failing test proves
the intended contract.

### Phase 1 tests: behavior-preserving architecture

- Characterize current command names, target disambiguation, configured target
  precedence, strict repository-root behavior, `--all <target>`, command order,
  frozen mode, mutation exclusion, and failure aggregation.
- Add a table-driven planner matrix mapping `RunRequest + ProjectConfig` to an
  ordered `RunPlan`.
- Test ordinary-failure continuation and first-nonzero aggregation through one
  recording executor. Characterize the current spawn-exception abort so Phase
  1 does not accidentally change it.
- Reduce CLI tests to argument syntax, user-facing errors, and exit behavior.

### Phase 2 tests: reporting

- Assert terminal rendering independently from execution.
- Assert the exact version-1 JSON keys, status vocabulary, deterministic order,
  nested types/nullability, and serialization of captured output.
- Prove JSON stdout is one parseable document even when checks write arbitrary
  output.
- Prove bounded capture, invalid UTF-8 replacement, ANSI removal, deterministic
  sorting, planning-error envelopes, and version rejection.
- Add the deliberate spawn-error continuation behavior and prove later checks
  still run while overall evidence becomes incomplete/error.
- Prove rendering errors fail without emitting partial JSON.

### Phase 3 tests: Test Shortcuts, pytest evidence, and coverage

- Test shortcut name/value validation, expansion, unknown-name suggestions,
  exact grammar, path containment, and every invalid combination.
- Exercise the standalone pytest plugin against temporary projects containing
  exits `0` through `5`, completed and early-stop failures, collection failure,
  setup/teardown errors, skip, call/setup/teardown XFAIL including
  `xfail(run=False)`, exact strict/non-strict XPASS report shapes,
  contradictory expected-failure shapes, and deliberately slow tests.
- Prove target-free runs become observed partial and gate-ineligible for
  config `addopts` or `PYTEST_ADDOPTS` selectors/paths/ignores, any deselection,
  and unclassified external options, while scope-neutral `-ra` remains complete.
- Prove a plugin-injected `--ignore`, a plugin-injected `-k` that happens to
  match every collected item, direct semantic-option mutation, and an
  unreported collection-list reduction all remain partial and gate-ineligible.
  Assert exact reason arrays: a reported reduction uses `deselected_tests`;
  only uncovered removed identities add `collection_reduced`.
- Prove consumer Python below 3.9 and pytest outside `>=8,<9` stop in
  `pytest_preflight` without loading the plugin. Prove Python 3.9 plain evidence
  works while coverage returns `CoverageError.unsupported_python` and launches
  no pytest process.
- Prove xdist `-n 0` remains single-process, non-empty xdist worker specs stop
  before workers start, and observable worker metadata, multiple artifact
  writers, duplicated sessions, or coverage shards invalidate evidence. Active
  xdist must report `evidence: null` without fabricated counts.
- Prove repeated setup, call, or teardown reports and a plugin `rerun` outcome
  are `unsupported_retries`; prove plugin/subprocess exit-code mismatches also
  fail evidence closed without a false pass. Document a custom protocol that
  hides all intermediate attempts as unsupported and not claimed detectable.
- Prove missing, malformed, non-finalized, and otherwise invalid plugin
  artifacts produce `evidence: null`, unique planner-known scope reasons, and
  no fabricated counts or empty findings.
- Install/run `pyrepo-check` outside the target environment and prove plain and
  coverage launches retain consumer `cwd`, root-module imports, `sys.path[0]`,
  existing `PYTHONPATH`, `python -m pytest`, and `uv run [--frozen]` semantics.
- Test missing/unsupported Coverage.py, valid base-only data, rejected
  `parallel`/`concurrency`/`patch` configuration, rejected `.coverage.*`
  shards, missing or malformed base data, malformed JSON, planned-to-observed
  partial/complete scope, exact line/branch gaps, and every threshold-matrix
  row and skip-reason precedence case.
- Prove JSON reporting reads the digest-verified isolated snapshot, never
  accepts an implicitly combined shard, and rejects root or snapshot-adjacent
  shards plus original/snapshot digest changes before final validation.
- Prove inherited `COVERAGE_PROCESS_START` and `COVERAGE_PROCESS_CONFIG` are
  absent from every coverage subprocess and cannot create consumer-root data
  or nested measurement.
- Prove focused and failed/incomplete pytest runs force `--fail-under=0`, only
  an eligible strict run interprets valid JSON exit `2` as threshold failure,
  and pytest executes exactly once.
- Prove the consumer's existing `.coverage`, coverage JSON, and worktree status
  remain unchanged by run-owned artifacts and cleanup.

### Phase 4 tests: repository adoption

- Add `coverage[toml]>=7.15,<8` to this repository's development group and
  regenerate `uv.lock` before activating coverage configuration.
- Add this repository's native line and branch coverage configuration.
- Measure the current baseline after the implementation; do not reuse a
  historical percentage as proof.
- Set an optional no-regression threshold at or below the verified baseline,
  not an arbitrary 100%.
- Run focused planner/renderer tests during development and the strict
  aggregate gate at every phase boundary.

## Delivery sequence

Each phase is an independently verified commit:

1. **Refactor:** extract planning and execution with no behavior change.
2. **Report:** add Agent Report plus terminal/JSON renderers.
3. **Feature:** add Test Shortcuts, pytest evidence, and coverage as separate
   atomic commits within the phase.
4. **Adopt:** add/lock the supported Coverage.py dependency, configure this
   repository's coverage, and document focused and strict workflows.

Refactoring and feature behavior remain separate commits. Existing unrelated
worktree files are never included.

## Acceptance criteria

The work is complete when all of the following are proven:

1. Existing focused and aggregate commands retain their selection, targets,
   ordering, and exit behavior through Phase 1.
2. `pyrepo-check ty` remains a typing-only Focused Run.
3. Direct pytest files and node IDs remain supported.
4. Valid Test Shortcuts expand deterministically; invalid or conflicting ones
   fail before execution.
5. Terminal remains the default, and JSON stdout conforms to the complete
   version-1 discriminated contract.
6. All selected checks continue after an ordinary failure and are represented
   in the report.
7. Coverage reports exact line and branch gaps, distinguishes partial from
   complete evidence, and gates only a configured strict aggregate run.
8. Coverage runs pytest once and creates/modifies no pyrepo-check-owned
   coverage, plugin, or report artifact under the consumer root.
9. Pytest exits `0` through `5`, early-stop completeness, slow nodes, skip,
   XFAIL, and strict/non-strict XPASS map exactly to the report contract.
10. Unsupported Python/pytest versions, active xdist, observable multi-writer
    or retry-shaped evidence, exit-code disagreement, and parallel coverage
    artifacts cannot produce a false pass; hidden arbitrary retry protocols are
    explicitly outside version 1.
11. Missing tools, configuration, or artifacts cannot produce a false pass.
12. The repository declares and locks the supported Coverage.py range before
    Ruff, annotation enforcement, `ty`, Bandit, pytest, and coverage pass its
    strict aggregate gate.
13. Dependency auditing, changed-code coverage, complexity, mutation, and
    flaky repetition remain outside this change.
