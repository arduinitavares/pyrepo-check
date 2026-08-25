# Structured Pytest Evidence C2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace schema-v1 `pytest: null` with trustworthy structured pytest evidence collected from exactly one consumer pytest run.

**Architecture:** The planner explicitly marks pytest evidence work; a standalone consumer-side plugin writes a private raw artifact plus exclusive writer markers in a run-owned temporary directory. Execution snapshots typed preflight, process, artifact, and writer observations before exact cleanup; `pytest_evidence.py` then validates and consolidates those immutable observations, and `reporting.py` projects one public result to terminal and JSON.

**Tech Stack:** Python `>=3.13.15`, pytest `>=8,<9`, pytest-xdist and pytest-rerunfailures as development-only fixtures, stdlib `json`/`tempfile`/`shutil`/`uuid`, uv subprocesses, immutable dataclasses, pytest integration tests

**Spec:** `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`

## Global Constraints

- Scope is Milestone C2 structured pytest evidence only. Milestone C3 coverage execution and Milestone D Agent Skill synchronization are excluded.
- Package and consumer Python require `>=3.13.15`; supported consumer pytest is every stable `>=8,<9` minor line.
- Keep `[project].dependencies = []`. Development-only xdist/rerun fixtures are allowed solely to prove fail-closed integration.
- The standalone plugin imports only standard-library modules and consumer pytest; it never imports `pyrepo_check` or a private `_pytest` module.
- Preserve `uv run [--frozen] python -m pytest`, consumer `cwd`, consumer `sys.path[0]`, existing `PYTHONPATH`, direct targets, Test Shortcuts, and one pytest execution.
- Run a captured `pytest_preflight` first. Unsupported Python, missing pytest, unsupported pytest, or invalid preflight output prevents plugin loading and test execution.
- Copy the plugin under a fresh valid identifier into one owner-only OS temporary directory outside the consumer root; append its directory to inherited `PYTHONPATH` and pass exact run-owned paths through environment variables.
- Remove inherited `COVERAGE_PROCESS_START` and `COVERAGE_PROCESS_CONFIG` from pytest preflight and primary environments so plain C2 cannot trigger nested consumer-root coverage output.
- Each plugin process creates one exclusive writer marker before publishing. Reporting trusts artifact bytes only when the immutable marker inventory and artifact writer identity are the same exact singleton.
- The plugin artifact is versioned and atomically replaced. Missing, malformed, non-finalized, multi-writer, unsupported-parallelism, retry-shaped, or exit-mismatched evidence fails closed and never fabricates counts.
- Version 1 accepts one pytest process. Active xdist, worker metadata, repeated phases, and non-core outcomes such as `rerun` invalidate evidence; `-n 0` remains valid.
- Terminal primary pytest output stays inherited; JSON primary output and every preflight stream are captured. Existing non-pytest command, output, continuation, and first-positive-exit behavior stays unchanged.
- Snapshot only exact regular files and writer markers from the current run directory. Remove only that directory in a `finally` path before reporting; reporting never reopens a temporary path.
- When pytest is selected, `RunReportV1.pytest` is non-null; otherwise it is null. Through C2, `coverage` remains null and `planned_coverage_scope` remains `not_requested`.
- Every implementation task records a real failing RED test before production code and a passing GREEN test after the smallest implementation.
- Do not merge, push, publish, or modify `.agents/skills/**` during this plan.

## File Structure

- `src/pyrepo_check/_pytest_report_plugin.py`: standalone pytest hooks, raw lifecycle/phase records, exclusive writer marker, and atomic artifact publication.
- `src/pyrepo_check/planning.py`: explicit immutable `PytestExecutionPlan` attached only to pytest checks.
- `src/pyrepo_check/execution.py`: generic ordered process/check observations and executor dispatch.
- `src/pyrepo_check/pytest_execution.py`: consumer preflight, isolated plugin environment, primary launch, immutable artifact snapshot, and cleanup.
- `src/pyrepo_check/pytest_evidence.py`: raw/preflight trust validation, outcome consolidation, scope classification, and public pytest dataclasses.
- `src/pyrepo_check/reporting.py`: Agent Report construction, validation, terminal/JSON projection, advisories, and overall status.
- `tests/test_pytest_report_plugin.py`: real plugin lifecycle, scope, expected-failure, xdist, rerun, and early-stop projects.
- `tests/test_pytest_version_matrix.py`: mandatory isolated pytest `8.0` through `8.4` compatibility runs.
- `tests/test_pytest_execution.py`: preflight, environment, command, artifact states, writer inventory, cleanup, and consumer boundary.
- `tests/test_pytest_evidence.py`: strict validation, precedence, exit matrix, consolidation, ordering, and observed scope.
- Existing planning/execution/reporting/CLI/runner/support/compatibility tests: regression and public integration coverage.
- `README.md` and the design status table: updated only after the final acceptance gate; `.agents/skills/**` stays untouched.

---

### Task 1: Standalone Plugin Lifecycle, Collection, and Writer Identity

**Files:**
- Create: `src/pyrepo_check/_pytest_report_plugin.py`
- Create: `tests/test_pytest_report_plugin.py`

**Interfaces:**
- Consumes: absolute `PYREPO_CHECK_PYTEST_JSON` and `PYREPO_CHECK_PYTEST_WRITER_DIR` paths plus its own `__name__`.
- Produces: one marker created with exclusive-create semantics at `pytest-writer-<writer_id>.json`; its JSON is `{schema_version: 1, writer_id: string, pid: integer}`.
- Produces: an atomically replaced artifact with exact top-level members `schema_version`, `state`, `writer_id`, `pytest_version`, `session`, `effective_args`, `semantic_options`, `collection`, `reports`, and `flags`.
- Produces: `state` is `started` until terminal publication and `finalized` only from the import-time exit handler after `pytest_sessionfinish` and `pytest_unconfigure` complete. `session` contains integer `starts`, `finishes`, `exit_code`, plus booleans `collection_completed` and `stopped_early`.
- Produces: `collection` contains string arrays `initial_nodeids`, `final_nodeids`, `deselected_nodeids`, `uncovered_removed_nodeids` plus arrays of `{nodeid, message}` for `errors` and `skips`.
- Produces: `semantic_options` contains `collection_paths`, `keyword`, `markexpr`, `deselect`, `ignore`, `ignore_glob`, `lf`, `pyargs`, `collectonly`, `setuponly`, and `setupplan`, using only strings, booleans, or string arrays.
- Produces: each report is `{nodeid, when, outcome, duration, wasxfail_present, wasxfail_valid, wasxfail, longrepr}`; duration is a finite non-negative JSON number and nullable text is JSON null.
- Produces: `flags` contains booleans `unsupported_parallelism`, `unsupported_retries`, and `worker_metadata`.

- [ ] **Step 1: Write the isolated plugin harness and failing lifecycle test**

Create `run_plugin_project()` in the test module. It copies the source plugin under `pyrepo_check_pytest_<uuid hex>.py`, sets artifact/writer environment paths outside the project, runs `sys.executable -m pytest -p MODULE`, and returns `CompletedProcess`, parsed artifact, marker documents, and project path.

```python
def test_plugin_finalizes_one_atomic_session_for_a_passing_test(tmp_path: Path) -> None:
    run = run_plugin_project(tmp_path, "def test_ok():\n    assert True\n")
    assert run.completed.returncode == 0
    assert run.artifact["state"] == "finalized"
    assert run.artifact["writer_id"] == run.markers[0]["writer_id"]
    assert run.artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 0,
        "collection_completed": True,
        "stopped_early": False,
    }
    assert [item["when"] for item in run.artifact["reports"]] == [
        "setup", "call", "teardown"
    ]
```

- [ ] **Step 2: Run the lifecycle test and record RED**

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py::test_plugin_finalizes_one_atomic_session_for_a_passing_test -vv`

Expected: FAIL because the standalone plugin does not exist.

- [ ] **Step 3: Implement exclusive writer registration and atomic lifecycle publication**

Use only public pytest hooks and stdlib. Create the marker with mode `0o600` and `open(..., "x")`. `_publish(state)` writes compact JSON to a unique sibling file, flushes, calls `os.fsync`, sets owner-only mode, and `os.replace`s it over the exact artifact path. Publish `started` during `pytest_sessionstart`; record the candidate finish during `pytest_sessionfinish`, close the hook lifecycle after `pytest_unconfigure`, and publish `finalized` from an import-time `atexit` handler only when one start, one finish, closure, and representable scope evidence are present. Any post-close hook activity attempts to atomically restore non-finalized state. If that publication fails after a terminal artifact was already published, terminate the child with pytest's internal-error exit code so parent-side process/artifact validation rejects any stale finalized bytes.

Document the boundary: fatal interpreter errors, unhandled terminating signals, and consumer `os._exit()` before terminal publication do not run `atexit` and must leave missing or started evidence. The post-terminal storage-failure fallback can leave finalized raw bytes on disk, but its non-zero primary process status makes those bytes invalid evidence. Hidden plugin state with no argument, option, collection, deselection, or report signal and concurrent background relay during shutdown remain outside the version-1 cooperative-plugin contract.

- [ ] **Step 4: Run the lifecycle test and record GREEN**

Run the Step 2 command again.

Expected: PASS with one marker and one finalized artifact.

- [ ] **Step 5: Write failing effective-argument, semantic-option, collection-wrapper, and collection-issue tests**

Add real projects proving configured `addopts`, `PYTEST_ADDOPTS`, invocation args, and inner-hook mutations survive; only the owned `-p MODULE` pair is removed; argument and semantic-option observations accumulate conservatively across lifecycle hooks; reported deselection is distinguished from silent removal; collection errors/skips remain separate; and normal collection completes.

```python
assert artifact["collection"]["deselected_nodeids"] == ["test_sample.py::test_b"]
assert artifact["collection"]["uncovered_removed_nodeids"] == []
assert artifact["semantic_options"]["keyword"] == "kept"
```

- [ ] **Step 6: Run the new collection/scope tests and record RED**

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py -k 'effective or semantic or collection or deselect' -vv`

Expected: FAIL because Task 1 does not yet collect these signals.

- [ ] **Step 7: Implement documented argument, semantic, collection, deselection, and report hooks**

Use a pytest-8 new-style outer wrapper around `pytest_load_initial_conftests`; retain the live `args`, observe it before and after `yield`, and remove only the exact owned pair. Refresh retained arguments and config options through session start, collection modification/finish, session finish, unconfigure, and interpreter exit. Additions, true booleans, and non-empty expressions remain sticky; incomparable argument order or conflicting non-empty expressions leave the artifact non-finalized. Use an outer collection wrapper, `pytest_deselected`, `pytest_collectreport`, `pytest_collection_finish`, and `pytest_runtest_logreport`. Do not import hook or report classes from `_pytest`.

- [ ] **Step 8: Run plugin tests, quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py -k 'not expected_failure and not xdist and not rerun and not early_stop' -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/_pytest_report_plugin.py tests/test_pytest_report_plugin.py`

Run: `uv run --frozen python -m ty check src/pyrepo_check/_pytest_report_plugin.py tests/test_pytest_report_plugin.py`

```bash
git add -- src/pyrepo_check/_pytest_report_plugin.py tests/test_pytest_report_plugin.py
git commit -m "feat: collect raw pytest session evidence"
```

---

### Task 2: Expected Failures, Early Stop, xdist, Retries, and Pytest 8 Compatibility

**Files:**
- Modify: `src/pyrepo_check/_pytest_report_plugin.py`
- Modify: `tests/test_pytest_report_plugin.py`
- Create: `tests/test_pytest_version_matrix.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_compatibility.py`

**Interfaces:**
- Consumes: Task 1 raw schema and writer protocol.
- Produces: raw reports sufficient to distinguish pytest-8 skip/XFAIL/XPASS shapes without terminal parsing.
- Produces: `flags.unsupported_retries` for a repeated `(nodeid, when)` or non-core outcome; `flags.unsupported_parallelism` before non-empty xdist specs create workers; `flags.worker_metadata` when worker state is observable.
- Produces: `session.stopped_early` only when a final selected node lacks a terminal outcome, `session.shouldstop` is non-empty, or `session.shouldfail` is non-empty. Ordinary `session.testsfailed > 0` after every selected node executes does not mean early stop.
- Produces: mandatory proof on pytest `8.0.2`, `8.1.1`, `8.2.2`, `8.3.5`, and `8.4.2`.

- [ ] **Step 1: Add development-only xdist/rerun fixtures under a failing dependency assertion**

First extend compatibility tests to assert `[project].dependencies` remains empty and dependency group `dev` contains bounded `pytest-xdist>=3.8,<4` and `pytest-rerunfailures>=16.6,<17` entries. Run the test and record RED, then edit `pyproject.toml` and regenerate `uv.lock` with Python 3.13.15.

Run: `uv run --frozen python -m pytest tests/test_compatibility.py -k pytest_fixture_dependencies -vv`

Expected after lock regeneration: PASS while runtime dependencies remain empty.

- [ ] **Step 2: Write and run the failing expected-failure and early-stop matrices**

Parametrize ordinary skip, imperative/marked XFAIL in setup/call/teardown, `xfail(run=False)`, strict XPASS, and non-strict XPASS. Add real `-x` and `--maxfail=1` projects with a collected-but-unexecuted node. Add a counter-case where multiple failing tests all execute and `stopped_early` remains false. Assert exact pytest-8 report shapes and that true early-stop collection is complete, terminal-node coverage incomplete, and `stopped_early` true.

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py -k 'expected_failure or early_stop' -vv`

Expected: FAIL before metadata/completeness implementation.

- [ ] **Step 3: Implement exact expected-failure metadata and early-stop detection**

Capture `wasxfail` presence/type without serializing arbitrary objects, and store `str(longrepr)` only when non-null. Keep raw phases; host code validates and consolidates later. Define terminal node coverage from final selected IDs and phase records. Set `stopped_early` only for missing terminal coverage or non-empty `session.shouldstop`/`session.shouldfail`; do not use `session.testsfailed` by itself.

- [ ] **Step 4: Write failing real and synthetic xdist/retry tests**

Required real tests: `-n 0` finalizes normally; `-n 1` records a forced `unsupported_parallelism` finish before exit-time publication and leaves a worker-only sentinel absent; `--reruns 1` publishes `unsupported_retries`. Keep synthetic plugins for repeated setup/teardown and arbitrary `rerun`. No test in this block may skip.

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py -k 'xdist or rerun or repeated_phase' -vv`

Expected: FAIL before fail-closed flags/hooks.

- [ ] **Step 5: Implement fail-closed xdist, worker, and retry detection**

Use `@pytest.hookimpl(optionalhook=True)` for `pytest_xdist_setupnodes(config, specs)`. With non-empty specs, record the forced finish before a controlled pytest exit; installed-but-inactive xdist and `-n 0` remain normal. The exit handler publishes the terminal artifact after unconfigure. Flag worker metadata, repeated core phases, and non-core outcomes.

- [ ] **Step 6: Write the failing stable-pytest-minor compatibility matrix**

Invoke isolated cached uv environments pinned to `8.0.2`, `8.1.1`, `8.2.2`, `8.3.5`, and `8.4.2`. For every version run pass, XFAIL, strict XPASS, non-strict XPASS, and deselection fixtures and assert common raw schema/shapes. Setup/network failure is failure, never skip.

Run: `uv run --frozen python -m pytest tests/test_pytest_version_matrix.py -vv`

Expected: FAIL until the matrix helper and cross-minor behavior exist.

- [ ] **Step 7: Make the plugin pass all supported pytest 8 minors**

Fix only documented-hook or public-report incompatibilities from Step 6. Add no version branches outside the approved strict-XPASS shim.

- [ ] **Step 8: Run Task 2 suites, quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py tests/test_pytest_version_matrix.py tests/test_compatibility.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/_pytest_report_plugin.py tests/test_pytest_report_plugin.py tests/test_pytest_version_matrix.py tests/test_compatibility.py`

Run: `uv run --frozen python -m ty check src/pyrepo_check/_pytest_report_plugin.py tests/test_pytest_report_plugin.py tests/test_pytest_version_matrix.py tests/test_compatibility.py`

```bash
git add -- src/pyrepo_check/_pytest_report_plugin.py tests/test_pytest_report_plugin.py tests/test_pytest_version_matrix.py tests/test_compatibility.py pyproject.toml uv.lock
git commit -m "feat: reject unsupported pytest execution shapes"
```

---

### Task 3: Explicit Pytest Planning and Ordered Generic Process Observations

**Files:**
- Modify: `src/pyrepo_check/planning.py`
- Modify: `src/pyrepo_check/execution.py`
- Modify: `src/pyrepo_check/reporting.py`
- Modify: `src/pyrepo_check/runner.py`
- Modify: `tests/test_planning.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_reporting.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/support.py`

**Interfaces:**
- Produces: `PytestExecutionPlan(consumer_python: tuple[str, ...], pytest_args: tuple[str, ...], artifact_protocol: Literal["pytest_v1"] = "pytest_v1")`.
- Produces: `PlannedCheck.pytest: PytestExecutionPlan | None`; only pytest has metadata, and its command equals `(*consumer_python, "-m", "pytest", *pytest_args)`.
- Produces: `ExecutedProcess(role, command, cwd, returncode, duration_ms, stdout, stderr, spawn_error)` and `ExecutedCheck(planned, processes)`.
- Preserves: architecture only; pytest still runs as one ordinary primary and public `pytest` remains null until later tasks.

- [ ] **Step 1: Write failing planner metadata tests**

Assert pytest exposes consumer Python/args directly, ordinary checks have null metadata, and targets/shortcuts/frozen mode keep the same visible command.

```python
assert pytest_check.command == (
    *pytest_check.pytest.consumer_python,
    "-m",
    "pytest",
    *pytest_check.pytest.pytest_args,
)
```

Run: `uv run --frozen python -m pytest tests/test_planning.py -k pytest_execution_plan -vv`

Expected: FAIL because `PytestExecutionPlan` does not exist.

- [ ] **Step 2: Implement explicit pytest planning metadata**

Split `_uv_python_prefix` into consumer-Python command and explicit `-m` composition. Attach metadata only to pytest; never parse argv later.

- [ ] **Step 3: Write failing generic ordered-process migration tests**

Migrate factories first. Prove ordinary checks still have one primary, identical argv/cwd/duration/capture/spawn/signal, continuation, banner order, and first-positive result.

Run: `uv run --frozen python -m pytest tests/test_execution.py tests/test_reporting.py tests/test_cli.py tests/test_runner.py -vv`

Expected: FAIL because `ExecutedCheck` still stores primary fields directly.

- [ ] **Step 4: Implement the behavior-preserving observation refactor**

Move primary data into immutable `ExecutedProcess`; make `ExecutedCheck.processes` the only process source. Update facade/reporting without changing current one-primary validation or `pytest: null`.

- [ ] **Step 5: Run architecture regression and commit**

Run: `uv run --frozen python -m pytest tests/test_planning.py tests/test_execution.py tests/test_reporting.py tests/test_cli.py tests/test_runner.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/planning.py src/pyrepo_check/execution.py src/pyrepo_check/reporting.py src/pyrepo_check/runner.py tests/test_planning.py tests/test_execution.py tests/test_reporting.py tests/test_cli.py tests/test_runner.py tests/support.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/planning.py src/pyrepo_check/execution.py src/pyrepo_check/reporting.py src/pyrepo_check/runner.py tests/test_planning.py tests/test_execution.py tests/test_reporting.py tests/test_cli.py tests/test_runner.py tests/support.py
git commit -m "refactor: model ordered check processes"
```

---

### Task 4: Consumer Preflight, Isolated Launch, Typed Artifact Snapshot, and Cleanup

**Files:**
- Create: `src/pyrepo_check/pytest_execution.py`
- Create: `tests/test_pytest_execution.py`
- Modify: `src/pyrepo_check/execution.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/support.py`

**Interfaces:**
- Consumes: Task 3 `PytestExecutionPlan`; no argv parsing/check-name branch selects evidence.
- Produces: `PytestPreflightRecord(python_version, pytest_available, pytest_version)`.
- Produces: `PytestPreflightObservation(classification, record, diagnostic)` where classification is `supported`, `unsupported_python`, `module_unavailable`, `unsupported_version`, `preflight_invalid`, `spawn_failed`, or `terminated_by_signal`.
- Produces: `PytestArtifactObservation(state, content, writer_ids, diagnostic)` where state is `not_attempted`, `snapshot`, `missing`, `unsafe_path`, or `read_failed`; content is non-null only for snapshot.
- Produces: `PytestExecutionObservation(preflight, artifact, cleanup_error)` attached as `ExecutedCheck.pytest`; process order is captured `pytest_preflight`, then optional `primary`.

- [ ] **Step 1: Write failing preflight classification and stop-before-plugin tests**

The Python-3.7-compatible probe reads `sys.version_info` before pytest import and emits one compact record. Test supported `3.13.15/8.4.2`; Python `3.13.14`; missing pytest; pytest `7.4.4`/`9.0.0`; nonzero, signal, spawn, extra output, malformed JSON, wrong types/schema, invalid UTF-8, and more than 65,536 bytes. Every invalid case has no primary.

Run: `uv run --frozen python -m pytest tests/test_pytest_execution.py -k preflight -vv`

Expected: FAIL because `pytest_execution.py` does not exist.

- [ ] **Step 2: Implement exact preflight execution and parsing**

Always capture preflight. Accept one schema-valid JSON line and reject streams that reporting would truncate. Preserve preflight spawn and signal as their matching classifications; ordinary nonzero/extra/malformed output is `preflight_invalid`. Stable supported pytest has major `8`; add no packaging dependency.

- [ ] **Step 3: Write failing isolated-command, environment, and consumer-semantics tests**

Prove primary argv is `(*consumer_python, "-m", "pytest", "-p", MODULE, *pytest_args)`; root imports/cwd/sys.path remain consumer-owned; existing PYTHONPATH precedes appended plugin; coverage startup vars are absent; all run files are outside consumer root; JSON captures primary while terminal inherits.

Run: `uv run --frozen python -m pytest tests/test_pytest_execution.py -k 'launch or environment or consumer' -vv`

Expected: FAIL before isolated launch.

- [ ] **Step 4: Implement isolated primary execution from planner metadata**

Create owner-only temp before preflight; copy/chmod plugin; set exact artifact/writer paths; append PYTHONPATH; remove coverage vars; launch one primary only after supported preflight. Use postponed annotations and a local execution import to avoid runtime cycles.

- [ ] **Step 5: Write failing artifact-state, writer-inventory, and cleanup tests**

Cover snapshot, missing, symlink/non-regular, read failure, zero/one/multiple or malformed markers, ID mismatch, preflight failure, primary spawn/signal, and cleanup failure. Assert immutable bytes/sorted writer IDs exist before cleanup; cleanup runs on every path and never targets consumer root.

Run: `uv run --frozen python -m pytest tests/test_pytest_execution.py -k 'artifact or writer or cleanup' -vv`

Expected: FAIL before typed snapshot/cleanup.

- [ ] **Step 6: Implement typed snapshot and exact finally cleanup**

Use `lstat`/exact regular-file checks without following symlinks. Inventory only exact marker names in the run dir, parse sorted IDs, read artifact bytes, then remove only the created directory in `finally`. Preserve observation when cleanup fails.

- [ ] **Step 7: Run execution suites, quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_execution.py tests/test_pytest_execution.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/execution.py src/pyrepo_check/pytest_execution.py tests/test_execution.py tests/test_pytest_execution.py tests/support.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/execution.py src/pyrepo_check/pytest_execution.py tests/test_execution.py tests/test_pytest_execution.py tests/support.py
git commit -m "feat: isolate pytest evidence execution"
```

---

### Task 5: Preflight and Raw Artifact Trust Validation

**Files:**
- Create: `src/pyrepo_check/pytest_evidence.py`
- Create: `tests/test_pytest_evidence.py`

**Interfaces:**
- Consumes: Task 4 observations and Task 1 raw schema.
- Produces: `ValidatedPhaseReport` records whose expected-failure metadata is already normalized as `none`, `xfail`, `xpass_non_strict`, or `xpass_strict`, with validated nullable reason and strict/affects-exit values.
- Produces: `ValidatedPytestSession` with trusted version, authoritative exit, effective args, semantics, collection, normalized phase reports, flags, and session.
- Produces: `PytestValidationFailure(code, message, pytest_version, exit_code)` using public PytestError codes.
- Produces: `validate_pytest_execution(check) -> ValidatedPytestSession | PytestValidationFailure`.

- [ ] **Step 1: Write failing preflight and artifact-precedence tests**

Assert exact precedence: preflight-specific unsupported/version/invalid; preflight or primary spawn/signal; artifact missing; artifact not finalized; schema/writer/expected-failure invalid; unsupported parallelism; unsupported retries; exit mismatch.

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -k validation_precedence -vv`

Expected: FAIL because validation does not exist.

- [ ] **Step 2: Implement strict preflight and raw-artifact validation**

Reject unsupported schema, required field/type/enum errors, non-finite/negative durations, unsafe/read failures, non-singleton or mismatched IDs, session cardinality, version mismatch, malformed or contradictory XPASS/XFAIL shapes, flags, repeated/non-core reports, and exit mismatch. Validate expected-failure shape before applying unsupported-parallelism/retry precedence, extract a strict-XPASS reason only from the pytest-8 `[XPASS(strict)] ` prefix, normalize an empty reason to null, and return only normalized phase records. Ignore unknown object members.

- [ ] **Step 3: Write failing combined-defect precedence table**

Combine each higher error with every lower defect and assert the higher wins. Assert the exact observation map: `missing` becomes `artifact_missing`; finalized-path `unsafe_path`, `read_failed`, malformed writer marker, non-singleton writers, or writer-ID mismatch becomes `artifact_invalid`; and a valid snapshot whose raw state is not `finalized` becomes `artifact_not_finalized`. Prove cleanup error does not erase an otherwise validated session because reporting owns the cleanup override.

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -k artifact_validation -vv`

Expected: FAIL until all strict branches exist.

- [ ] **Step 4: Complete validation, run quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -k 'validation or artifact' -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/pytest_evidence.py tests/test_pytest_evidence.py`

Run: `uv run --frozen python -m ty check src/pyrepo_check/pytest_evidence.py tests/test_pytest_evidence.py`

```bash
git add -- src/pyrepo_check/pytest_evidence.py tests/test_pytest_evidence.py
git commit -m "feat: validate pytest artifact trust"
```

---

### Task 6: Public Pytest Result, Consolidation, Exit Matrix, and Scope

**Files:**
- Modify: `src/pyrepo_check/pytest_evidence.py`
- Modify: `tests/test_pytest_evidence.py`

**Interfaces:**
- Consumes: Task 5 validation result and `RunPlan.planned_test_scope`.
- Produces: exact immutable `PytestResult`, `PytestEvidence`, `PytestCounts`, `CollectionIssue`, `SlowTest`, `SpecialTestOutcome`, and `PytestError`.
- Produces: `build_pytest_result(plan: RunPlan, check: ExecutedCheck) -> PytestResult`.
- Produces: scope order `planned_selector`, `effective_narrowing_option`, `unclassified_external_option`, `deselected_tests`, `collection_reduced`, `incomplete_session`.

- [ ] **Step 1: Write failing evidence-null and exit-matrix tests**

Every validation failure yields incomplete/null evidence/partial scope and only planner-known selector plus incomplete reason. Parametrize exits `0`-`5`, unknown, spawn, signal. Complete exit 1 is failed/null error; early-stop exit 1 is failed/session_incomplete; valid zero exit 5 is complete/failed; exits 2/3/4 are error/incomplete but may retain valid partial evidence.

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -k 'evidence_null or exit_matrix' -vv`

Expected: FAIL before public result construction.

- [ ] **Step 2: Implement public dataclasses and exit/completeness mapping**

Trusted preflight alone supplies version. Valid incomplete artifacts may retain evidence; invalid ones may not. Complete requires collection complete, no collection errors, no stop, terminal outcome per final node, and count sum equal collected; any collection error forces incomplete. Early stop/interruption does not require the sum.

- [ ] **Step 3: Write failing consolidation and deterministic-order tests**

Assert precedence over Task 5's already-normalized phase records: setup/teardown failure, strict XPASS, call failure, valid XFAIL, non-strict XPASS, ordinary skip, pass. Task 6 must not discover or emit a new artifact-invalid condition. Sum phase durations, round half-up, sort ten slowest by `(-duration_ms, nodeid)`, special by node ID, collection issues by `(nodeid, message)`.

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -k 'consolidation or ordering' -vv`

Expected: FAIL before consolidation.

- [ ] **Step 4: Implement outcome consolidation and ordering**

Consolidate only Task 5's normalized expected-failure kind, reason, strictness, and affects-exit fields. Skip/XFAIL strictness remains null. Never reinterpret raw `wasxfail`/`longrepr` or parse terminal output in this task.

- [ ] **Step 5: Write failing observed-scope matrix**

Test positional args and all known narrowing options; only the frozen report/verbosity/tb/locals/color/capture/warnings/strict/duration set is neutral, including separated operands. Unknown options are unclassified. Test semantic mutations, deselection, uncovered reduction, fixed order, and target-free `-ra` complete.

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -k scope -vv`

Expected: FAIL before scope classification.

- [ ] **Step 6: Implement fixed-order unique scope classification**

Scope is complete iff reasons are empty. Never infer artifact-derived reasons when evidence is null.

- [ ] **Step 7: Run evidence suite, quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_pytest_evidence.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/pytest_evidence.py tests/test_pytest_evidence.py`

Run: `uv run --frozen python -m ty check src/pyrepo_check/pytest_evidence.py tests/test_pytest_evidence.py`

```bash
git add -- src/pyrepo_check/pytest_evidence.py tests/test_pytest_evidence.py
git commit -m "feat: consolidate structured pytest evidence"
```

---

### Task 7: Agent Report Schema, Terminal Projection, and Advisories

**Files:**
- Modify: `src/pyrepo_check/reporting.py`
- Modify: `tests/test_reporting.py`

**Interfaces:**
- Consumes: ordered processes and Task 6 `build_pytest_result`.
- Produces: `RunReportV1.pytest: PytestResult | None`, exact JSON, validation, terminal order, advisories, completeness/status, and exit selection.
- Produces: pytest processes `pytest_preflight`, optional `primary`; ordinary checks retain one primary.
- Produces: pytest check status normally equals result; cleanup forces check error while preserving finalized pytest pass/fail.

- [ ] **Step 1: Write failing exact-schema and precedence snapshots**

Cover complete pass/failure, exit 5, interrupted partial evidence, focused partial, evidence-null, cleanup override, a selected pytest check with no execution observation (`PytestError.not_started` plus `missing_primary_process`), and unselected pytest. Selected is non-null, unselected null, coverage null, and precedence is incomplete/error before failed before passed.

Run: `uv run --frozen python -m pytest tests/test_reporting.py -k pytest -vv`

Expected: FAIL because reporting still requires pytest null and ordinary cardinality.

- [ ] **Step 2: Integrate public pytest construction, projection, and validation**

Serialize/validate every required field, enum, type, nullability, count, duration, and ordering rule. Map preflight, spawn/signal, evidence, and cleanup errors to their CheckError codes.

- [ ] **Step 3: Write failing terminal, advisory, and exit tests**

Order incomplete/error evidence; failed checks/pytest; special/slow; advisories; passes. Add sorted `missing_test_reason` for null/empty special reasons and preserve truncation advisories. Helper diagnostics render only on error. Preserve first positive process exit; otherwise failed 1, error 2, pass 0.

Run: `uv run --frozen python -m pytest tests/test_reporting.py -k 'terminal or advisory or exit' -vv`

Expected: FAIL before C2 projection.

- [ ] **Step 4: Implement terminal/advisory/exit behavior**

Keep other tools in native output; add only structured pytest attention lines. Sort advisories by `(code, message)`.

- [ ] **Step 5: Run reporting suite, quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_reporting.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/reporting.py tests/test_reporting.py`

Run: `uv run --frozen python -m ty check src/pyrepo_check/reporting.py tests/test_reporting.py`

```bash
git add -- src/pyrepo_check/reporting.py tests/test_reporting.py
git commit -m "feat: project pytest evidence into agent reports"
```

---

### Task 8: CLI and External-Consumer Integration

**Files:**
- Modify: `src/pyrepo_check/cli.py`
- Modify: `src/pyrepo_check/runner.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_compatibility.py`

**Interfaces:**
- Consumes: Tasks 3-7 without CLI syntax changes.
- Produces: one terminal/JSON result from real external consumer, one preflight, at most one primary.
- Preserves: targets/shortcuts, non-pytest checks, fallback, editable install, and Python floor.

- [ ] **Step 1: Write failing real JSON CLI tests**

Use minimal uv consumers. Assert JSON pytest has non-null evidence, exact processes, one primary; nodes/shortcuts preserve args/scope; unsupported pytest stops after preflight with schema-valid error.

Run: `uv run --frozen python -m pytest tests/test_cli.py -k structured_pytest -vv`

Expected: FAIL before CLI integration fixtures.

- [ ] **Step 2: Integrate CLI/runner and fallback boundaries**

Pass runner/environment seams through execution. Reporting/serialization failure emits no partial JSON after cleanup. Preserve subprocess exit selection.

- [ ] **Step 3: Write failing external consumer and cleanliness tests**

Run installed pyrepo-check outside target env and prove cwd/root imports/sys.path/PYTHONPATH/uv/module semantics. Assert `.coverage`, plugin/artifact files, and git status unchanged even with inherited coverage startup variables.

Run: `uv run --frozen python -m pytest tests/test_compatibility.py -k external_consumer -vv`

Expected: FAIL before full boundary fixture.

- [ ] **Step 4: Complete external boundary compatibility**

Fix only integration gaps. Never expose installed `pyrepo_check` inside consumer pytest and never call `pytest.main`.

- [ ] **Step 5: Run integration/regression, quality checks, and commit**

Run: `uv run --frozen python -m pytest tests/test_cli.py tests/test_runner.py tests/test_compatibility.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/cli.py src/pyrepo_check/runner.py tests/test_cli.py tests/test_runner.py tests/test_compatibility.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/cli.py src/pyrepo_check/runner.py tests/test_cli.py tests/test_runner.py tests/test_compatibility.py
git commit -m "feat: expose structured pytest evidence through cli"
```

---

### Task 9: C2 Acceptance, User Documentation, and Verified Status

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Verify unchanged: `.agents/skills/pyrepo-check/SKILL.md`

**Interfaces:**
- Consumes: all C2 commits and acceptance matrix.
- Produces: full-suite/strict-gate evidence before “implemented and verified”.
- Produces: README non-null evidence guidance; C3/D and Agent Skill unchanged.

- [ ] **Step 1: Run the complete C2 behavior matrix**

Run: `uv run --frozen python -m pytest tests/test_pytest_report_plugin.py tests/test_pytest_version_matrix.py tests/test_pytest_execution.py tests/test_pytest_evidence.py tests/test_reporting.py tests/test_cli.py tests/test_compatibility.py -vv`

Expected: PASS for exits 0-5, early stop, collection issues, setup/teardown errors, skip/XFAIL/XPASS, slow nodes, scope, all pytest minors, preflight failures, xdist/retry/cardinality/exit mismatch, artifact states, consumer semantics, cleanup, JSON, and terminal. No required skips.

- [ ] **Step 2: Run complete repository tests**

Run: `uv run --frozen python -m pytest -q`

Expected: all PASS with no C2 warning.

- [ ] **Step 3: Run strict repository gate**

Run: `uv run --frozen pyrepo-check --all`

Expected: Ruff, annotations, ty, Bandit, pytest PASS; pytest executes once with structured summary.

- [ ] **Step 4: Inspect boundaries**

Run: `git diff --check`

Run: `git status --short`

Run: `git diff --name-only f7b46b1..HEAD -- .agents/skills`

Run: `uv lock --check`

Expected: clean checks, no run artifacts, empty Agent Skill diff.

- [ ] **Step 5: Update docs only after Steps 1-4 pass**

Replace README's C1 null statement with a concise JSON example for status, complete, scope reasons, counts, special outcomes, and slowest. Mark C2 implemented/verified in both design status tables; leave C3/D unchanged and Skill sync deferred.

- [ ] **Step 6: Verify documentation diff and commit**

Run: `git diff --check`

Run: `git diff --name-only -- .agents/skills`

Expected: pass and empty Skill output.

```bash
git add -- README.md docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "docs: document structured pytest evidence"
```

- [ ] **Step 7: Re-run strict gate on final committed tree**

Run: `uv run --frozen pyrepo-check --all`

Expected: complete PASS on exact final C2 tree. Create no empty acceptance commit.
