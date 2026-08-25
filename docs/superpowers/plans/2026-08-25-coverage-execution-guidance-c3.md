# Coverage Execution and Guidance C3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add trustworthy line-and-branch coverage execution and structured agent guidance while preserving one pytest run, direct targets, Test Shortcuts, and the existing strict aggregate gate.

**Architecture:** Configuration records only native Coverage.py facts, and the pure planner decides whether coverage is not requested, unavailable, partial, or complete. `pytest_execution.py` remains the single test-run coordinator, while a new `coverage_execution.py` owns Coverage.py subprocesses and immutable coverage observations. A shared `artifact_safety.py` owns bounded no-follow evidence reads and streaming digest/copy helpers. A new `coverage_evidence.py` validates Coverage JSON, applies the exact scope/threshold matrix, and builds the public result; `reporting.py` only composes and projects that result.

**Tech Stack:** Python `>=3.13.15`, consumer Coverage.py `coverage[toml]>=7.15,<8`, consumer pytest `>=8,<9`, stdlib `tomllib`/`json`/`hashlib`/descriptor-relative filesystem APIs, uv subprocesses, immutable dataclasses, pytest unit and external-consumer tests

**Spec:** `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`

## Global Constraints

- Scope is Milestone C3 coverage execution and guidance only. Milestone D repository Coverage.py adoption and Agent Skill synchronization remain excluded.
- Keep `[project].dependencies = []`; do not change `pyproject.toml`, `uv.lock`, or `.agents/skills/**` in C3.
- Preserve plain commands, direct pytest files/node IDs, Test Shortcuts, target order, check order, `uv run [--frozen]`, consumer `cwd`, existing `PYTHONPATH`, terminal streaming, and schema version `1`.
- Run pytest exactly once. Coverage instrumentation replaces the primary command with `python -m coverage run ... -m pytest`; it never performs a second pytest invocation.
- Coverage failures never fall back to successful plain pytest. Missing configuration is advisory-only solely for an implicit target-free aggregate run.
- When coverage is planned, attempt both pytest and coverage preflights in order whenever the verified workspace and consumer process launcher remain usable, even if pytest preflight is unsupported. Start the primary only when both preflights pass. Planned coverage never becomes null: a passed coverage preflight followed by no primary/data is `data_missing`; setup that prevents coverage preflight itself is `preflight_invalid`.
- Use argument vectors only. Never use a shell, consumer-root glob, consumer-owned `.coverage`, consumer-owned JSON report, or direct SQLite parsing.
- Put the plugin, pytest artifact, base data, rejected shards, digest-verified report snapshot, and Coverage JSON in the invocation-owned OS temporary directory outside the consumer root.
- Remove inherited `COVERAGE_PROCESS_START` and `COVERAGE_PROCESS_CONFIG` from every pytest/Coverage.py subprocess; set run-owned `COVERAGE_FILE` and `COVERAGE_RCFILE` only for coverage work.
- Accept exactly one no-follow regular base data file and no `.coverage.*` or `coverage-data.*` shards. Verify original and snapshot SHA-256 digests before and after JSON generation.
- Coverage JSON is the authoritative public input. Bound it to 128 MiB. Stream-copy and digest the SQLite data file with a 512 MiB ceiling; never retain its bytes in a report observation.
- Resolve Coverage JSON file keys against the consumer root, require an existing regular measured file whose resolved path remains beneath the resolved root, normalize to a project-relative `/` path, and reject duplicate normalized paths.
- `[tool.coverage.report]` may be absent. When present, finite numeric `fail_under` is retained exactly; native Coverage.py decides threshold success using its own precision semantics.
- A stable Coverage.py version string is ASCII digits in `major.minor` or `major.minor.patch` form with no pre/dev/post/local suffix. Version `>=7.15,<8` proceeds.
- The planner owns static consumer intent and command components. The executor owns ephemeral paths and the evidence-dependent choice to add `--fail-under=0`; both execution and reporting call the same pure eligibility/skip-reason policy so they cannot disagree.
- Focused, target-bearing, shortcut, failed-test, incomplete-test, and evidence-error runs never apply a native threshold.
- Coverage status is independent from pytest check status, but coverage `failed` participates in overall failure and coverage `error` makes the run incomplete/error.
- Every implementation task records a real RED test before production code and a GREEN result after the smallest implementation.
- Do not merge, push, publish, delete the worktree, or synchronize skills without fresh user approval.

## File Structure

- `src/pyrepo_check/artifact_safety.py`: shared bounded JSON parsing, no-follow regular-file reads, streaming copy/digest, and typed safety failures.
- `src/pyrepo_check/config.py`: native Coverage.py configuration facts and validation.
- `src/pyrepo_check/planning.py`: coverage request policy, planned scope, and immutable coverage execution metadata.
- `src/pyrepo_check/cli.py`: public `--coverage` syntax only.
- `src/pyrepo_check/coverage_execution.py`: Coverage.py preflight, commands, environment, data/shard/snapshot validation, Coverage JSON snapshot, and immutable observations.
- `src/pyrepo_check/pytest_execution.py`: one-run coordinator that shares its verified temporary workspace with coverage execution.
- `src/pyrepo_check/execution.py`: optional coverage observation on the selected pytest check.
- `src/pyrepo_check/coverage_evidence.py`: coverage public dataclasses, JSON trust validation, gap normalization/ranking, scope, error precedence, gate eligibility, and threshold result.
- `src/pyrepo_check/reporting.py`: schema-v1 composition/validation, terminal guidance, JSON projection, advisories, completeness, and exit integration.
- `tests/test_artifact_safety.py`: shared evidence byte, file-type, digest, copy, and size-bound tests.
- `tests/test_coverage_execution.py`: preflight, command/environment, base/shard/snapshot/digest, and cleanup-boundary tests.
- `tests/test_coverage_evidence.py`: raw JSON validation, paths, counts/arcs/ranking, errors, scope, and threshold matrix.
- Existing config/planning/pytest-execution/reporting/CLI/compatibility tests: regression and public integration coverage.
- `README.md` and the design status table: updated only after final C3 acceptance; `.agents/skills/**` stays untouched.

## Dependency Graph

```text
Task 1 artifact safety
  -> Task 4 execution lifecycle
  -> Task 6 data snapshot and JSON generation

Task 2 native configuration
  -> Task 3 CLI and planning
  -> Task 4 execution lifecycle

Task 3 CLI and planning
  -> Task 4 execution lifecycle
  -> Task 5 evidence policy

Task 4 execution lifecycle
  -> Task 5 evidence policy
  -> Task 6 data snapshot and JSON generation

Task 5 evidence policy
  -> Task 6 data snapshot and JSON generation
  -> Task 7 evidence construction

Task 6 data snapshot and JSON generation
  -> Task 7 evidence construction

Task 7 evidence construction
  -> Task 8 report projection
  -> Task 9 external integration

Tasks 1-9
  -> Task 10 documentation and milestone gate
```

---

### Task 1: Extract Shared Artifact Safety Without Behavior Change

**Files:**
- Create: `src/pyrepo_check/artifact_safety.py`
- Create: `tests/test_artifact_safety.py`
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `src/pyrepo_check/pytest_evidence.py`
- Modify: `tests/test_pytest_execution.py`

**Interfaces:**
- Move the existing private bounded JSON parser and exact no-follow regular-file reader without semantic changes.
- Produce `read_regular_file(path, *, max_bytes, dir_fd=None) -> bytes`.
- Produce `load_bounded_json(content, *, max_nesting=64) -> object` with non-finite JSON rejection.
- Produce `digest_regular_file(...) -> FileDigest` and `copy_regular_file_with_digest(...) -> FileDigest`, using SHA-256, bounded streaming, exclusive no-follow destination creation, complete-write handling, and no in-memory data-file copy.
- Preserve existing pytest artifact limits, diagnostics, exception classes, and monkeypatch seams through deliberate imports or migrated tests.

- [ ] **Step 1: Move current behavior tests to a failing shared-module test**

Move the exact-size, one-byte-over, metadata-growth, FIFO, symlink, nesting, and `NaN`/`Infinity` cases from `tests/test_pytest_execution.py` into `tests/test_artifact_safety.py`, importing the not-yet-created module.

Run: `uv run --frozen python -m pytest tests/test_artifact_safety.py -vv`

Expected: FAIL because `pyrepo_check.artifact_safety` does not exist.

- [ ] **Step 2: Extract existing reader and parser, then preserve pytest behavior**

Move `_UnsafePathError`, `_BoundedReadError`, `_read_regular_file`, `_load_bounded_json`, the nesting scan, and constant rejection. Update `pytest_execution.py` and `pytest_evidence.py` to import them. Do not alter run-directory creation, verification, cleanup, artifact state, or public reporting.

Run: `uv run --frozen python -m pytest tests/test_artifact_safety.py tests/test_pytest_execution.py tests/test_pytest_evidence.py -vv`

Expected: PASS with the same C2 behavior.

- [ ] **Step 3: Write RED streaming digest/copy tests**

Cover exact/over-limit source sizes, source symlink/FIFO, exclusive destination, partial writes, source mutation during read, destination digest mismatch, and no destination bytes retained as a Python observation.

Run: `uv run --frozen python -m pytest tests/test_artifact_safety.py -k 'digest or copy' -vv`

Expected: FAIL because streaming helpers do not exist.

- [ ] **Step 4: Implement bounded streaming SHA-256 copy**

Open the source with `O_NOFOLLOW|O_NONBLOCK`, validate regular type and size, stream at most 512 MiB in 64 KiB chunks, hash while copying, create the destination with `O_CREAT|O_EXCL|O_NOFOLLOW` mode `0o600` relative to its verified directory descriptor, fsync, reopen/hash the destination, and reject any size/digest mismatch.

Run: `uv run --frozen python -m pytest tests/test_artifact_safety.py -vv`

Expected: PASS.

- [ ] **Step 5: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/artifact_safety.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/pytest_evidence.py tests/test_artifact_safety.py tests/test_pytest_execution.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/artifact_safety.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/pytest_evidence.py tests/test_artifact_safety.py tests/test_pytest_execution.py
git commit -m "refactor: share safe artifact handling"
```

---

### Task 2: Load Native Coverage.py Configuration Facts

**Files:**
- Modify: `src/pyrepo_check/config.py`
- Modify: `tests/test_config.py`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`

**Interfaces:**
- Produce immutable `CoverageConfig(config_path: Path, fail_under: int | float | None)`.
- Produce `ProjectConfig.coverage: CoverageConfig | None` with a default preserving existing test constructors.
- Entirely absent `[tool.coverage]` produces `None`.
- Any partially present coverage table must have `[tool.coverage.run]`, exact `branch = true`, and at least one non-empty string across list-valued `source`, `source_pkgs`, or `source_dirs`.
- Reject `parallel = true`, non-empty list-valued `concurrency`, and non-empty list-valued `patch`.
- `[tool.coverage.report]` may be absent. When `fail_under` is present, accept only a finite TOML integer/float excluding booleans and retain its numeric value.
- Preserve all other native Coverage.py settings without translating them; Coverage.py later reads the same absolute `pyproject.toml`.

- [ ] **Step 1: Write RED native-config classification table**

Cover no pyproject, unrelated tables, absent coverage, valid `source`/`source_pkgs`/`source_dirs`, multiple source families, absent report table, integer/float threshold, missing/false/non-boolean branch, empty/invalid source values, wrong table shapes, `parallel`, `concurrency`, `patch`, and non-finite/wrong-type threshold.

Run: `uv run --frozen python -m pytest tests/test_config.py -k coverage -vv`

Expected: FAIL because coverage facts do not exist.

- [ ] **Step 2: Parse the complete TOML document once and validate facts**

Refactor the loader so one `tomllib.load` feeds both `[tool.pyrepo-check]` and `[tool.coverage]`. Keep existing missing/invalid pyrepo-check behavior unchanged. Raise one specific `InvalidCoverageConfigError` with a configuration-path diagnostic for partially present invalid coverage.

Run: `uv run --frozen python -m pytest tests/test_config.py -vv`

Expected: PASS.

- [ ] **Step 3: Resolve the specification seams explicitly**

Amend the design spec to state: report table optional; threshold value extraction rules; stable Coverage.py version grammar; planner/static versus executor/ephemeral command ownership; 128 MiB JSON and 512 MiB streaming-data bounds; root-relative path resolution/containment; and C3 completion excluding Milestone D acceptance criterion 12.

Run: `git diff --check`

Expected: PASS with C3 status still “designed; not implemented.”

- [ ] **Step 4: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/config.py tests/test_config.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/config.py tests/test_config.py docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "feat: validate native coverage configuration"
```

---

### Task 3: Plan Coverage Intent and Expose `--coverage`

**Files:**
- Modify: `src/pyrepo_check/planning.py`
- Modify: `src/pyrepo_check/cli.py`
- Modify: `tests/test_planning.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_compatibility.py`

**Interfaces:**
- Add `RunRequest.coverage_requested: bool = False`.
- Add `CoverageExecutionPlan(consumer_python, config_path, fail_under, artifact_protocol="coverage_v1")` and attach it optionally to `PytestExecutionPlan`.
- Add `RunPlan.planned_coverage_scope: "not_requested" | "unavailable" | "partial" | "complete"`.
- Add planning error `coverage_configuration_required`.
- Explicit `--coverage` is valid only when pytest is explicitly selected or aggregate semantics select pytest.
- Explicit coverage without valid configuration is a planning error and executes nothing.
- A target or Test Shortcut plans partial coverage; target-free pytest plans complete coverage but remains a focused run; target-free no-check/`--all` plans complete strict coverage.
- A valid native config auto-enables coverage for target-free aggregate runs. Entirely absent config plans `unavailable` and plain pytest. Focused runs without `--coverage` remain `not_requested`.

- [ ] **Step 1: Write RED planner matrix**

Cover explicit focused pytest, direct target, Test Shortcut, explicit aggregate, implicit aggregate auto-enable, aggregate absent config, `ty --coverage`, target-only `--coverage`, focused plain pytest with valid config, and `--all <target> --coverage`. Assert mode, pytest args, coverage metadata, and planned scope exactly.

Run: `uv run --frozen python -m pytest tests/test_planning.py -k coverage -vv`

Expected: FAIL because request/plan coverage fields do not exist.

- [ ] **Step 2: Implement pure coverage selection policy**

Keep shortcut conflict checks first, then validate coverage selection. Do not add random paths or observed-evidence decisions to the plan. Preserve `PlannedCheck.command` as the current plain-pytest compatibility/display command; attach the coverage plan only to pytest metadata.

Run: `uv run --frozen python -m pytest tests/test_planning.py -vv`

Expected: PASS, including all C1/C2 target and shortcut cases.

- [ ] **Step 3: Write RED CLI syntax and zero-spawn planning-error tests**

Assert `--coverage` works before/after check tokens, appears in help, populates `RunRequest`, and that invalid/missing configuration renders exact terminal/JSON planning errors without calling the runner.

Run: `uv run --frozen python -m pytest tests/test_cli.py tests/test_compatibility.py -k coverage -vv`

Expected: FAIL because argparse has no `--coverage`.

- [ ] **Step 4: Add syntax-only CLI wiring**

Add one boolean flag and map `InvalidCoverageConfigError`/`PlanningFailure` into the existing report boundary. Do not make CLI coverage policy decisions.

Run: `uv run --frozen python -m pytest tests/test_cli.py tests/test_compatibility.py -vv`

Expected: PASS.

- [ ] **Step 5: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/planning.py src/pyrepo_check/cli.py tests/test_planning.py tests/test_cli.py tests/test_compatibility.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/planning.py src/pyrepo_check/cli.py tests/test_planning.py tests/test_cli.py tests/test_compatibility.py
git commit -m "feat: plan coverage execution"
```

---

### Task 4: Add Coverage Preflight and One Instrumented Pytest Process

**Files:**
- Create: `src/pyrepo_check/coverage_execution.py`
- Create: `tests/test_coverage_execution.py`
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `src/pyrepo_check/execution.py`
- Modify: `tests/test_pytest_execution.py`

**Interfaces:**
- Produce typed `CoveragePreflightRecord`, `CoveragePreflightObservation`, `CoverageArtifactObservation`, and `CoverageExecutionObservation` values.
- Add `ExecutedCheck.coverage: CoverageExecutionObservation | None = None`.
- Preflight emits exactly one compact schema-v1 JSON line containing consumer Python version, availability, and raw Coverage.py version.
- Classify `supported`, `unsupported_python`, `module_unavailable`, `unsupported_version`, `preflight_invalid`, `spawn_failed`, and `terminated_by_signal`.
- Required attempted-process order is `pytest_preflight`, `coverage_preflight`, then optional `primary`.
- Attempt coverage preflight after an ordinary pytest-preflight classification failure so each planned evidence owner gets an authoritative preflight; do not attempt it when platform/workspace setup or process-launch capability makes another command unsafe or impossible.
- Primary argv is `(*consumer_python, "-m", "coverage", "run", "--rcfile=<absolute pyproject>", "--data-file=<run>/.coverage", "-m", "pytest", "-p", plugin_module, *pytest_args)`.
- Either preflight failure prevents primary execution and never falls back to plain pytest. When both fail, the pytest check reports its own pytest-preflight failure first while coverage retains its independent preflight error.

- [ ] **Step 1: Write RED Coverage.py preflight table**

Cover Python `3.13.14`/`3.13.15`, missing coverage, stable `7.15`/`7.15.2`/`7.99.0`, `7.14.9`, `8.0.0`, prerelease/dev/post/local suffixes, malformed/extra/truncated/invalid UTF-8 output, nonzero, signal, and spawn failure. Cross these with failed pytest preflight: assert coverage preflight is still attempted when safe, no primary runs unless both pass, and platform/workspace setup failure attempts neither process.

Run: `uv run --frozen python -m pytest tests/test_coverage_execution.py -k preflight -vv`

Expected: FAIL because coverage execution does not exist.

- [ ] **Step 2: Implement exact preflight execution and parsing**

Use a Python-3.7-compatible `-c` probe that reads `sys.version_info` before importing coverage. Capture both streams in terminal and JSON modes. Retain a trusted unsupported stable version string; use null when the module/version record is unavailable or untrusted.

Run the Step 1 command again.

Expected: PASS.

- [ ] **Step 3: Write RED command, environment, ordering, and one-run tests**

Assert both preflights use consumer Python and config path; every coverage subprocess removes inherited startup vars; coverage commands set run-owned `COVERAGE_FILE`/`COVERAGE_RCFILE`; primary retains consumer `cwd`, root import, existing `PYTHONPATH`, plugin isolation, targets, and shortcuts; and only one command contains `-m pytest`.

Run: `uv run --frozen python -m pytest tests/test_coverage_execution.py tests/test_pytest_execution.py -k 'command or environment or order or once' -vv`

Expected: FAIL before coordinator integration.

- [ ] **Step 4: Coordinate coverage inside the existing verified run directory**

Keep `pytest_execution.py` responsible for creating/verifying/cleaning one workspace. Delegate coverage-specific preparation and command creation to `coverage_execution.py`. Gate identities before/after each new preparation and process boundary exactly like the existing plugin gates. Preserve plain C2 execution unchanged.

Run: `uv run --frozen python -m pytest tests/test_coverage_execution.py tests/test_pytest_execution.py tests/test_execution.py -vv`

Expected: PASS for preflight/primary behavior while coverage artifact reporting remains intentionally incomplete until Task 6.

- [ ] **Step 5: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/coverage_execution.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/execution.py tests/test_coverage_execution.py tests/test_pytest_execution.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/coverage_execution.py src/pyrepo_check/pytest_execution.py src/pyrepo_check/execution.py tests/test_coverage_execution.py tests/test_pytest_execution.py
git commit -m "feat: run pytest under coverage"
```

---

### Task 5: Define Coverage Result Types and One Gate Policy

**Files:**
- Create: `src/pyrepo_check/coverage_evidence.py`
- Create: `tests/test_coverage_evidence.py`

**Interfaces:**
- Produce all normative C3 immutable types: `CoverageResult`, `CoverageThreshold`, `CoverageTotals`, `CoverageCounts`, `CoverageFile`, `FileStatementCoverage`, `FileBranchCoverage`, and `CoverageError`.
- Produce immutable `CoverageGatePolicy(gate_eligible, skipped_reason, force_fail_under_zero)`.
- Produce one pure `coverage_gate_policy(plan, pytest_result, evidence_complete)` used before JSON execution and during final result construction.
- Gate eligibility requires target-free strict aggregate mode, primary pytest exit `0`, finalized valid complete pytest evidence, observed complete scope with no scope reasons, and complete coverage collection evidence.
- Select threshold skipped reasons in exact precedence: evidence error, not configured, no tests, pytest incomplete, pytest failed, partial run, focused run.
- Keep this module independent from `coverage_execution.py`; use `TYPE_CHECKING` and primitive/public plan/pytest inputs so execution may import the policy without a cycle.

- [ ] **Step 1: Write RED gate-policy matrix**

Cover strict configured/unconfigured, focused target-free pytest, direct target, shortcut, observed partial from external narrowing, pytest exits `0`-`5`, missing/invalid pytest evidence, incomplete session, no tests, and coverage evidence complete/incomplete. Assert gate eligibility, forced `--fail-under=0`, and every skip-reason precedence combination.

Run: `uv run --frozen python -m pytest tests/test_coverage_evidence.py -k gate_policy -vv`

Expected: FAIL because `coverage_evidence.py` does not exist.

- [ ] **Step 2: Add immutable public types and implement the pure policy**

Define the public dataclasses and literals exactly as schema v1. The policy accepts no temporary paths, process objects, or raw JSON; it decides only whether native threshold semantics may be requested and which threshold reason applies.

Run the Step 1 command again.

Expected: PASS.

- [ ] **Step 3: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/coverage_evidence.py tests/test_coverage_evidence.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/coverage_evidence.py tests/test_coverage_evidence.py
git commit -m "feat: define coverage gate policy"
```

---

### Task 6: Isolate Coverage Data and Generate Trusted JSON

**Files:**
- Modify: `src/pyrepo_check/coverage_execution.py`
- Modify: `src/pyrepo_check/pytest_execution.py`
- Modify: `tests/test_coverage_execution.py`
- Modify: `tests/test_pytest_execution.py`
- Modify: `tests/test_artifact_safety.py`

**Interfaces:**
- After primary exit, require base `.coverage` to be one readable no-follow regular file.
- Reject any run-root `.coverage.*` shard before or after report generation.
- Create an empty no-follow `report-input` directory and stream-copy base data to its exact `coverage-data` name while checking SHA-256.
- Reject any snapshot-adjacent `coverage-data.*` shard before or after report generation.
- Run Coverage JSON against only the snapshot, with `--keep-combined` and `--fail-under=0` unless the shared pure policy says the strict gate is eligible.
- Snapshot Coverage JSON as immutable bytes, then prove original/snapshot size and SHA-256 remain unchanged before cleanup.
- Record process role `coverage_json`; never run `coverage combine`.

- [ ] **Step 1: Write RED base/shard/snapshot matrix**

Cover valid base only; missing, symlinked, FIFO, oversized, unreadable, and concurrently changed base; run-root shards; snapshot shards; destination collision; copy/digest mismatch; original/snapshot mutation before/after JSON; and shard creation/removal attempts around reporting.

Run: `uv run --frozen python -m pytest tests/test_coverage_execution.py -k 'data or shard or snapshot or digest' -vv`

Expected: FAIL because coverage data is not isolated.

- [ ] **Step 2: Implement descriptor-relative namespace scans and snapshot**

Inspect only the current run descriptor and newly created report-input descriptor. Match literal base/shard prefixes without a shell glob. Use Task 1 streaming helpers. Preserve exact observation precedence: preflight/spawn/signal, unsupported parallelism, unexpected parallel data, missing data, generation failure, missing JSON, invalid JSON.

Run the Step 1 command again.

Expected: PASS.

- [ ] **Step 3: Write RED Coverage JSON command/exit tests**

Build a finalized pytest result from the immutable plugin snapshot. Assert eligible strict evidence omits `--fail-under=0`; every focused/partial/failed/incomplete/no-tests state includes it. Assert JSON exit `2` is retained for eligible threshold classification; every other nonzero case is a generation error candidate. Assert JSON command uses only the snapshot and `--keep-combined`.

Run: `uv run --frozen python -m pytest tests/test_coverage_execution.py -k 'coverage_json or fail_under or threshold' -vv`

Expected: FAIL before JSON generation.

- [ ] **Step 4: Implement JSON generation and immutable observation**

Call the existing pure pytest result builder after primary snapshot and before cleanup. Call Task 5 `coverage_gate_policy` to select the argv. Capture helper streams in both output modes, read JSON with the 128 MiB bound, finalize all namespace/digest checks, and retain no temporary path as a reporting dependency.

Run: `uv run --frozen python -m pytest tests/test_coverage_execution.py tests/test_pytest_execution.py -vv`

Expected: PASS.

- [ ] **Step 5: Prove cleanup and consumer boundaries, then commit**

Assert all C3 paths are removed by the existing exact finally cleanup, cleanup failure remains typed, consumer `.coverage`/JSON files are byte-identical, consumer worktree status is unchanged, and no later run reuses the workspace.

Run: `uv run --frozen python -m pytest tests/test_artifact_safety.py tests/test_coverage_execution.py tests/test_pytest_execution.py -vv`

Run: `uv run --frozen python -m ruff check src/pyrepo_check/artifact_safety.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/pytest_execution.py tests/test_artifact_safety.py tests/test_coverage_execution.py tests/test_pytest_execution.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/artifact_safety.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/pytest_execution.py tests/test_artifact_safety.py tests/test_coverage_execution.py tests/test_pytest_execution.py
git commit -m "feat: isolate coverage evidence"
```

---

### Task 7: Build Coverage Result and Exact Gaps

**Files:**
- Modify: `src/pyrepo_check/coverage_evidence.py`
- Modify: `tests/test_coverage_evidence.py`
- Modify: `src/pyrepo_check/coverage_execution.py`
- Modify: `src/pyrepo_check/pytest_execution.py`

**Interfaces:**
- Extend Task 5 types with one authoritative builder over immutable execution observations; import coverage execution types only under `TYPE_CHECKING` to avoid a runtime cycle.
- Validate Coverage JSON schema/version, trusted `meta.version`, exact non-negative integer counts, missing line/branch arrays, summary consistency, project-root containment, and deterministic uniqueness.
- Build missing branch arcs from Coverage JSON `missing_branches`; retain negative endpoints and sort by source then destination.
- Sort files by `missing_statements + missing_branches` descending, then normalized path ascending.
- Implement exact error precedence, observed scope transition, evidence completeness, threshold evaluated/passed state, and reuse Task 5 gate/skip policy without duplicating it.

- [ ] **Step 1: Write RED valid JSON and deterministic-guidance tests**

Cover zero/nonzero statements and branches, negative arcs, unordered/duplicate lines and arcs, multiple files with ranking ties, relative/absolute paths, `..`, symlink escape, outside-root files, duplicate normalized paths, unknown additive members, malformed types, inconsistent summaries/totals, non-finite values, and preflight version mismatch.

Run: `uv run --frozen python -m pytest tests/test_coverage_evidence.py -k 'json or gap or path or order' -vv`

Expected: FAIL because Task 5 has no Coverage JSON validator or result builder.

- [ ] **Step 2: Implement strict JSON validation and gap normalization**

Parse only immutable Task 6 bytes with `load_bounded_json`. Ignore unknown object members, but require all Coverage.py members used for counts and missing detail. Never compute public exact counts from percentages.

Run the Step 1 command again.

Expected: PASS.

- [ ] **Step 3: Write RED result/error/threshold matrix**

Cover every preflight/artifact/generation error and combined-defect precedence. Include failed pytest preflight plus supported coverage preflight as `data_missing`, failed pytest and coverage preflights as their independent typed results, and setup-before-coverage-preflight as `preflight_invalid`. Cover strict configured pass/fail, strict unconfigured pass, focused complete guidance, partial target/shortcut guidance, pytest exit `1`, exits `2`-`4`, exit `5`, observed partial from external narrowing, cleanup-independent eligibility, and builder agreement with every Task 5 gate-policy result.

Run: `uv run --frozen python -m pytest tests/test_coverage_evidence.py -k 'result or error or threshold or scope' -vv`

Expected: FAIL before public-result construction.

- [ ] **Step 4: Implement one authoritative builder and shared policy**

Return `coverage=None` only for planned `not_requested`/`unavailable`. For planned coverage errors return partial/incomplete/error with null totals, empty files, and `evidence_error`. For valid non-gating evidence return guidance without altering pytest status. Interpret Coverage JSON exit `2` as `threshold_not_met` only for eligible strict evidence with valid JSON.

Run: `uv run --frozen python -m pytest tests/test_coverage_evidence.py -vv`

Expected: PASS.

- [ ] **Step 5: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/coverage_evidence.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/pytest_execution.py tests/test_coverage_evidence.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/coverage_evidence.py src/pyrepo_check/coverage_execution.py src/pyrepo_check/pytest_execution.py tests/test_coverage_evidence.py
git commit -m "feat: build structured coverage guidance"
```

---

### Task 8: Project Coverage Through Agent Report Schema v1

**Files:**
- Modify: `src/pyrepo_check/reporting.py`
- Modify: `tests/test_reporting.py`
- Modify: `src/pyrepo_check/coverage_evidence.py`
- Modify: `tests/test_coverage_evidence.py`

**Interfaces:**
- Replace `RunReportV1.coverage: None` with `CoverageResult | None` without changing schema version or existing member order.
- Project `RunPlan.planned_coverage_scope` exactly.
- A pytest check process array permits `pytest_preflight`, required attempted `coverage_preflight` when coverage is planned and setup remains usable, optional primary, and optional `coverage_json` in attempted order. A failed pytest preflight may therefore coexist with a real coverage preflight and no primary.
- Coverage preflight that prevents pytest owns `coverage_preflight_failed` on the pytest check and an independent typed coverage error.
- Coverage collection/reporting/threshold outcomes after primary never change pytest check status.
- Overall precedence is incomplete/error, then failed including coverage threshold, then passed.
- Terminal attention order is evidence errors, planned failures, exact coverage gaps, pytest special/slow results, advisories, passed checks.

- [ ] **Step 1: Write RED exact schema-v1 projection tests**

Assert every coverage object member, nullability, enum, number/count constraint, path/order rule, process shape, top-level order, and invalid cross-field mutation. Preserve existing pytest and ordinary-check validation cases unchanged.

Run: `uv run --frozen python -m pytest tests/test_reporting.py -k coverage -vv`

Expected: FAIL because reporting rejects non-null coverage.

- [ ] **Step 2: Compose and validate coverage with thin reporting helpers**

Import public coverage types and validators from `coverage_evidence.py`. Keep raw Coverage JSON knowledge out of reporting. Update completeness/status/advisory builders to accept coverage explicitly.

Run: `uv run --frozen python -m pytest tests/test_reporting.py -vv`

Expected: PASS.

- [ ] **Step 3: Write RED terminal, JSON, advisory, and exit tests**

Assert exact gap lines/arcs and calculated terminal-only percentages; threshold failure; coverage evidence error diagnostics; `coverage_not_configured`; `coverage_threshold_not_applied`; deterministic ordering; one compact JSON document; and first-positive process exit precedence including eligible Coverage JSON exit `2`.

Run: `uv run --frozen python -m pytest tests/test_reporting.py -k 'terminal or serialize or advisory or exit' -vv`

Expected: FAIL for new coverage projections only.

- [ ] **Step 4: Implement both projections from one report**

Serialize exact integer counts and missing opportunities. Calculate line and branch percentages to two decimal places only for terminal headers; display `100.00%` for a zero denominator. Do not emit speculative common findings for Ruff, ty, or Bandit.

Run: `uv run --frozen python -m pytest tests/test_reporting.py tests/test_coverage_evidence.py -vv`

Expected: PASS.

- [ ] **Step 5: Run focused quality gates and commit**

Run: `uv run --frozen python -m ruff check src/pyrepo_check/reporting.py src/pyrepo_check/coverage_evidence.py tests/test_reporting.py tests/test_coverage_evidence.py`

Run: `uv run --frozen python -m ty check`

```bash
git add -- src/pyrepo_check/reporting.py src/pyrepo_check/coverage_evidence.py tests/test_reporting.py tests/test_coverage_evidence.py
git commit -m "feat: report coverage guidance"
```

---

### Task 9: Prove CLI and External-Consumer Compatibility

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_compatibility.py`
- Modify: `tests/support.py`
- Modify: `src/pyrepo_check/cli.py`

**Interfaces:**
- Prove the public examples: `pyrepo-check pytest --coverage`, `pyrepo-check pytest --shortcut unit --coverage`, `pyrepo-check --coverage`, and `pyrepo-check --format json --all` with valid/absent config.
- Prove plain pytest remains byte/argv compatible when coverage is not planned.
- Prove an installed tool outside the consumer environment finds Coverage.py only in the consumer environment.
- Prove consumer `.coverage`, coverage JSON, plugin names, `cwd`, `sys.path[0]`, `PYTHONPATH`, and git/worktree state remain unchanged.

- [ ] **Step 1: Write RED end-to-end consumer projects**

Create isolated consumers for full, target, node, shortcut, failed, incomplete, no-tests, threshold-pass, threshold-fail, missing dependency, invalid config, and shard-producing cases. Record the primary invocation count and pre/post filesystem bytes/status.

Run: `uv run --frozen python -m pytest tests/test_cli.py tests/test_compatibility.py -k coverage -vv`

Expected: FAIL until all seams are integrated.

- [ ] **Step 2: Make only necessary CLI integration corrections**

Wire final report construction to the completed coverage observation. Preserve the existing no-partial-JSON fallback and runner exception boundaries. Do not move policy into CLI.

Run the Step 1 command again.

Expected: PASS.

- [ ] **Step 3: Run the complete C3 behavior matrix**

Run: `uv run --frozen python -m pytest tests/test_artifact_safety.py tests/test_coverage_execution.py tests/test_coverage_evidence.py tests/test_pytest_execution.py tests/test_pytest_evidence.py tests/test_reporting.py tests/test_cli.py tests/test_compatibility.py -vv`

Expected: PASS with no required skips; pytest runs once in every coverage case.

- [ ] **Step 4: Run regression and quality gates, then commit**

Run: `uv run --frozen python -m pytest -q`

Run: `uv run --frozen python -m ruff check .`

Run: `uv run --frozen python -m ty check`

```bash
git add -- tests/test_cli.py tests/test_compatibility.py tests/support.py src/pyrepo_check/cli.py
git commit -m "test: prove coverage consumer boundaries"
```

---

### Task 10: Document C3 and Run the Milestone Boundary Gate

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Verify unchanged: `pyproject.toml`
- Verify unchanged: `uv.lock`
- Verify unchanged: `.agents/skills/pyrepo-check/SKILL.md`

**Interfaces:**
- Document explicit, focused, shortcut, implicit aggregate, absent-config advisory, JSON guidance, and threshold behavior.
- Mark C3 implemented/verified only after the complete final gate passes.
- Keep D designed/not implemented and state that this repository does not yet adopt Coverage.py or a threshold.
- Keep both repository and installed Agent Skills intentionally stale until after D, per user direction.

- [ ] **Step 1: Run full pre-documentation acceptance**

Run: `uv run --frozen python -m pytest -q`

Run: `uv run --frozen pyrepo-check --all`

Expected: existing strict gate passes. Because D is not implemented, this repository has no native coverage config and the aggregate result includes `coverage_not_configured` rather than C3 coverage evidence.

- [ ] **Step 2: Inspect protected boundaries**

Run: `git diff --check`

Run: `uv lock --check`

Run: `git diff 973babb9d23ccc257e6404daef61f87c11e6bd0f -- pyproject.toml uv.lock .agents/skills`

Expected: no diff for dependency/configuration or Agent Skill paths.

- [ ] **Step 3: Update README and delivery status honestly**

Add copy-paste command examples and one abridged structured coverage example. Replace the C2 “coverage remains null” statement. Mark only C3 implemented/verified after Steps 1-2. Keep D and skill synchronization explicitly pending.

- [ ] **Step 4: Verify documentation and commit**

Run: `git diff --check`

Run: `uv run --frozen python -m pytest -q`

```bash
git add -- README.md docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "docs: document coverage guidance"
```

- [ ] **Step 5: Run final exact-tree verification**

Run: `uv run --frozen pyrepo-check --all`

Run: `uv run --frozen python -m pytest -q`

Run: `git diff --check`

Run: `uv lock --check`

Run: `git status --short --branch`

Expected: all checks pass; worktree is clean; C3 is implemented; D remains pending; no skill files changed.

## C3 Completion Evidence

Before asking for merge/push approval, record:

- exact branch, worktree, and final SHA;
- commit list for Tasks 1-10;
- complete test count and strict-gate output;
- explicit/shortcut/aggregate coverage command examples;
- valid JSON evidence for complete and partial coverage;
- threshold pass/fail and unavailable-config cases;
- proof pytest ran exactly once;
- proof consumer artifacts/worktree stayed unchanged;
- empty diffs for `pyproject.toml`, `uv.lock`, and `.agents/skills/**`;
- independent code review verdict and any residual risks.
