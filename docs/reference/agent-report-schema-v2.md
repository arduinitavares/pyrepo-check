# Agent Report schema version 2

This is the public JSON contract emitted by `pyrepo-check --format json`.
`src/pyrepo_check/reporting_schema.py`, `src/pyrepo_check/pytest_evidence.py`, and
`src/pyrepo_check/coverage_evidence.py` are the implementation owners. JSON is UTF-8,
compact, and followed by one newline. Dataclass declaration order is JSON key order.

## Notation

- `T | null` means a nullable field; the key is still present.
- `T[]` means a JSON array. Python tuples serialize as arrays.
- `integer` excludes booleans. Counts and durations are non-negative unless stated.
- `number` is a finite JSON integer or float.
- Paths described as absolute are non-empty, NUL-free, lexically normalized absolute
  strings. Coverage paths are project-relative POSIX strings.
- Schema version 2 is the top-level report version. A trusted check-start marker has
  its own `schema_version: 1` artifact protocol.

## Top-level discriminated union

### `PlanningErrorReportV2`

Exact field order:

| Field | Type | Contract |
| --- | --- | --- |
| `schema_version` | `2` | Always 2. |
| `kind` | `"planning_error"` | Union discriminator. |
| `overall_status` | `"error"` | Planning never produces findings. |
| `complete` | `false` | No run occurred. |
| `tool_environment` | `ToolEnvironmentEvidence` | Controller observation. |
| `repository_environment` | `null` | Planning stops before repository execution. |
| `error` | `PlanningErrorV2` | Required typed planning error. |

### `RunReportV2`

Exact field order:

| Field | Type | Contract |
| --- | --- | --- |
| `schema_version` | `2` | Always 2. |
| `kind` | `"run"` | Union discriminator. |
| `project_root` | absolute path | Resolved project root. |
| `mode` | `"focused"` \| `"strict_aggregate"` | Selection mode. |
| `overall_status` | `"passed"` \| `"failed"` \| `"error"` | Aggregated public outcome. |
| `complete` | boolean | Whether all requested evidence completed without typed error. |
| `tool_environment` | `ToolEnvironmentEvidence` | Controller observation. |
| `repository_environment` | `RepositoryEnvironmentEvidence` | Always present, even when partial/error. |
| `selection` | `Selection` | Planned public selection. |
| `checks` | `CheckResultV2[]` | Selected checks in fixed order. |
| `pytest` | `PytestResult` \| `null` | Non-null exactly when pytest was selected. |
| `coverage` | `CoverageResult` \| `null` | Null only for planned `not_requested` or `unavailable`. |
| `advisories` | `Advisory[]` | Unique, sorted by `(code, message)`. |

## Common and environment objects

### `PythonEvidence`

`implementation: string`, `version: [integer, integer, integer]`, then
`executable: absolute path`. Version parts are non-negative. Repository execution
accepts CPython 3.10 through 3.13; Tool Python follows the package requirement.

### `ToolEnvironmentEvidence`

`pyrepo_check_version: string`, then `python: PythonEvidence`.

### `PlanningErrorV2`

`code: PlanningErrorCodeV2`, `message: string`, then `hint: string | null`.

### `RepositoryPythonSelectionEvidence`

`kind: "default" | "explicit"`, then `request: string | null`. `request` is null
exactly for `default`; for `explicit` it is the validated CLI value.

### `LockEvidence`

`path: absolute path`, then `status: "current" | "missing" | "unverified"`.
`current` requires a successful locked environment probe, not mere file presence.

### `EnvironmentError`

`code: EnvironmentErrorCode`, `message: string`, then `hint: string | null`.

### `CheckErrorV2`

`code: CheckErrorCodeV2`, `message: string`, then `hint: string | null`.

### `CapturedText`

Exact order: `captured: boolean`, `text: string`, `truncated: boolean`,
`omitted_bytes: integer`. When `captured` is false the remaining values are `""`,
`false`, and `0`. When captured, `truncated` is true exactly when `omitted_bytes > 0`.

### `ProcessResult`

Exact order:

| Field | Type |
| --- | --- |
| `role` | `ProcessRole` |
| `argv` | `string[]` |
| `cwd` | absolute path |
| `outcome` | `"exited"` \| `"signaled"` \| `"spawn_failed"` |
| `exit_code` | integer or null |
| `signal` | positive integer or null |
| `duration_ms` | integer |
| `stdout` | `CapturedText` |
| `stderr` | `CapturedText` |
| `error_message` | string or null |

For `exited`, `exit_code` is non-negative and `signal`/`error_message` are null. For
`signaled`, only `signal` and `error_message` are non-null. For `spawn_failed`, both
numeric codes are null and `error_message` is non-null.

### `DependencyEvidence`

Exact order: `name`, `module`, `required`, `status`, `version`, `origin`, `process`,
`error`.

- `name`: `"ruff" | "ty" | "bandit" | "pytest" | "coverage"`
- `module`: exactly the same literal as `name`, using the fixed mapping below
- `required`: string
- `status`: `available | missing | incompatible | shadowed | unusable | unobserved`
- `version`: string or null
- `origin`: absolute path or null
- `process`: `ProcessResult | null`
- `error`: `CheckErrorV2 | null`

Every non-null process has role `dependency_probe`. Status correlations are exact:

| Status | Version/origin | Process/error |
| --- | --- | --- |
| `available` | Supported version and non-null origin | Successful process; null error. |
| `missing` | No additional correlation | Successful process; `check_dependency_missing`. |
| `incompatible` | Non-null version | Successful process; `check_dependency_incompatible`. |
| `shadowed` | Non-null conflicting origin | Successful process; `check_dependency_shadowed`. |
| `unusable` | No additional correlation | Successful process; `check_dependency_unusable`. |
| `unobserved` | Both null | Either an attempted process with `check_dependency_unusable`, or null process/error because a pre-execution environment error prevented the attempt. |

Here a successful probe has role `dependency_probe`, `outcome="exited"`, and
`exit_code=0`; both `stdout` and `stderr` have `captured=true`, `truncated=false`, and
`omitted_bytes=0`. The public validator intentionally imposes no further version/origin
nullability rule on `missing` or `unusable`, nor on the unconstrained side of
`incompatible` or `shadowed`.

The normalized `required` ranges are fixed for this release:

| Dependency | Module | Required range |
| --- | --- | --- |
| `ruff` | `ruff` | `>=0.15,<1` |
| `ty` | `ty` | `>=0.0.35,<0.1` |
| `bandit` | `bandit` | `>=1.9,<2` |
| `pytest` | `pytest` | `>=8,<9` |
| `coverage` | `coverage` | `>=7.15,<8` |

### `RepositoryEnvironmentEvidence`

Exact field order:

| Field | Type |
| --- | --- |
| `manager` | `"uv"` |
| `manager_version` | string or null |
| `path` | absolute path or null |
| `python_selection` | `RepositoryPythonSelectionEvidence` |
| `python` | `PythonEvidence` \| `null` |
| `lock` | `LockEvidence` |
| `dependency_selection` | `"default"` |
| `mutation_protection` | `"unobserved"` \| `"protected_files"` \| `"tracked_files"` |
| `dependencies` | `DependencyEvidence[]` |
| `processes` | `ProcessResult[]` |
| `error` | `EnvironmentError` \| `null` |

Environment processes contain attempted pre-execution `repository_safety` commands in
order, then `uv_version`, then `environment_probe`; the post-run tracked-file listing
is last. Dependencies follow first-required check order, share one Ruff entry, and put
Coverage after pytest. `tracked_files` means both Git snapshots were built;
`protected_files` is the non-Git protected-file proof; neither claims sandboxing.

## Selection, checks, and advisories

### `Selection`

Exact order:

1. `checks: CheckName[]`
2. `targets: string[]`
3. `test_shortcut: string | null`
4. `pytest_args: string[] | null`
5. `planned_test_scope: "not_selected" | "partial" | "complete"`
6. `planned_coverage_scope: "not_requested" | "unavailable" | "partial" | "complete"`

Check names are `ruff`, `annotations`, `annotations-fix`, `ty`, `bandit`, and
`pytest`. They are unique and ordered `ruff`, `annotations`, `ty`, `bandit`,
`pytest`, then opt-in `annotations-fix`. Without pytest, shortcut/pytest args are
null and scopes are `not_selected`/`not_requested`. Direct pytest targets equal
`pytest_args` and make planned test scope partial; no targets makes it complete.
Non-null Test Shortcut names match `[a-z][a-z0-9_-]*`. A shortcut requires
pytest-only selection, no direct targets, non-empty pytest args, and partial scope.

### `AnalysisPythonAuthorityEvidence`

`authority: "repository_tool"`, then `pyrepo_check_override: null`. This reports
who controls static language semantics, not a numeric Python version.

### `CheckStartEvidence`

Exact order: `schema_version: 1`, `check: CheckName`,
`module: "ruff" | "ty" | "bandit" | "pytest" | "coverage"`,
`arguments_sha256: string`, then `python: PythonEvidence`. The digest is exactly 64
lowercase hexadecimal characters. All values must match the validated invocation and
prepared Repository Python.

### `CheckResultV2`

Exact order: `name`, `status`, `execution_environment`,
`analysis_python_authority`, `start_evidence`, `processes`, `error`.

- `status`: `passed | failed | error`
- `execution_environment`: `"repository" | null`
- `analysis_python_authority`: `AnalysisPythonAuthorityEvidence | null`
- `start_evidence`: `CheckStartEvidence | null`
- `processes`: `ProcessResult[]`
- `error`: `CheckErrorV2 | null`

A passed/failed check has a completed primary, trusted start evidence,
`execution_environment: "repository"`, and null error. An error check always has an
error. Static-analysis authority is non-null only for Ruff, annotations,
annotations-fix, or Ty after valid start evidence and primary exit 0 or 1. An
environment/dependency synthesized error has no process or start/execution/analysis
evidence. Independent checks may still run.

### `Advisory`

`code: AdvisoryCode`, `message: string`, then `hint: string | null`.

## Pytest objects

### `PytestResult`

Exact order:

1. `status: "passed" | "failed" | "error"`
2. `complete: boolean`
3. `scope: "partial" | "complete"`
4. `scope_reasons: TestScopeReason[]`
5. `pytest_version: string | null`
6. `exit_code: integer | null`
7. `evidence: PytestEvidence | null`
8. `error: PytestError | null`

### `PytestEvidence` and nested objects

Exact order: `effective_args: string[]`, `collected: integer`,
`deselected: integer`, `counts: PytestCounts`,
`collection_errors: CollectionIssue[]`, `collection_skips: CollectionIssue[]`,
`slowest: SlowTest[]`, `special_outcomes: SpecialTestOutcome[]`.

- `PytestCounts`: `passed`, `failed`, `errors`, `skipped`, `xfailed`, `xpassed`
  (all integers, in that order).
- `CollectionIssue`: `nodeid: string`, then `message: string`.
- `SlowTest`: `nodeid: string`, then `duration_ms: integer`.
- `SpecialTestOutcome`: `nodeid: string`,
  `outcome: "skipped" | "xfailed" | "xpassed"`, `reason: string | null`,
  `strict: boolean | null`, `affects_exit: boolean`, then
  `duration_ms: integer`. Skips and xfails require null `strict` and false
  `affects_exit`; an xpass requires non-null `strict` and
  `affects_exit == strict`.
- `PytestError`: `code: PytestErrorCode`, then `message: string`.

Pytest correlations:

- `complete=true` requires evidence, null error, and a non-error status. Null evidence
  requires an incomplete error result with a non-null error. The sole non-error result
  allowed to contain an error is incomplete `failed` evidence with
  `session_incomplete`.
- Pytest status, error, exit, evidence, and dependency-version correlations are:

  | Error/code family | Status and completeness | Exit/evidence | `pytest_version` |
  | --- | --- | --- | --- |
  | null, exit 0 / 1 / 5 | `passed` / `failed` / `failed`; may be complete | Same exit; evidence present. Complete exit 5 requires collected 0. | Exact available dependency version. |
  | `session_incomplete` | `failed`, incomplete | Exit 1; evidence present. | Exact available dependency version. |
  | `interrupted` / `internal_error` / `usage_error` | `error`, incomplete | Exit 2 / 3 / 4; evidence present. | Exact available dependency version. |
  | `unknown_exit_code` | `error`, incomplete | Exit outside 0-5; evidence present. | Exact available dependency version. |
  | Missing / incompatible / shadowed, unusable, or unobserved dependency | `module_unavailable` / `unsupported_version` / `preflight_invalid`; `error`, incomplete | Null exit/evidence. | Null / known incompatible version / null. |
  | Environment unavailable | `preflight_invalid`; `error`, incomplete | Null exit/evidence. | Null. |
  | Setup did not reach primary | `not_started`; `error`, incomplete | Null exit/evidence. | Null before marker preparation; otherwise exact available dependency version. |
  | `spawn_failed` / `terminated_by_signal` | `error`, incomplete | Null exit/evidence. | Exact available dependency version. |
  | `unsupported_parallelism`, `unsupported_retries`, `exit_code_mismatch`, `artifact_missing`, `artifact_invalid`, or `artifact_not_finalized` | `error`, incomplete | Retained primary exit; null evidence. | Exact available dependency version. |

  If workspace setup fails before retaining an unavailable-dependency preflight, the
  result uses the `not_started` row. `unsupported_python` remains in the owner enum
  but has no accepted schema-v2 Run Report correlation; Repository Python rejection
  is represented by environment error plus synthesized `preflight_invalid` pytest.
- Scope is complete exactly when `scope_reasons` is empty. Reasons are unique in the
  registry order. `planned_selector` exactly matches partial planned scope;
  `deselected_tests` exactly matches a positive deselected count; and
  `incomplete_session` exactly negates `complete`. The generator adds
  `effective_narrowing_option`, `unclassified_external_option`, and
  `collection_reduced` from the validated pytest invocation/artifact. Null evidence
  permits only `planned_selector` and `incomplete_session`.
- Collection errors and skips are unique and ordered by `(nodeid, message)`. Slow
  tests use unique nodeids, descending duration then nodeid order, and contain exactly
  `min(10, total outcome count)` entries. Special outcomes use unique nodeids and
  nodeid order, and their cardinality equals skipped + xfailed + xpassed. A shared
  nodeid has the same duration in both lists. Outcome counts never exceed collected;
  complete evidence has no collection errors and total outcomes equal collected.

## Coverage objects

### `CoverageResult`

Exact order:

1. `status: "passed" | "failed" | "guidance" | "error"`
2. `scope: "partial" | "complete"`
3. `evidence_complete: boolean`
4. `coverage_version: string | null`
5. `gate_eligible: boolean`
6. `threshold: CoverageThreshold`
7. `totals: CoverageTotals | null`
8. `files: CoverageFile[]`
9. `error: CoverageError | null`

### Coverage nested objects

- `CoverageThreshold`: `configured: boolean`, `value: number | null`,
  `evaluated: boolean`, `passed: boolean | null`,
  `skipped_reason: CoverageThresholdSkipReason | null`.
- `CoverageCounts`: `covered: integer`, then `missing: integer`.
- `CoverageTotals`: `statements: CoverageCounts`, then `branches: CoverageCounts`.
- `FileStatementCoverage`: `covered`, `missing`, then ordered unique positive
  `missing_lines: integer[]`; its length equals `missing`.
- `FileBranchCoverage`: `covered`, `missing`, then ordered unique
  `missing_arcs: [integer, integer][]`; endpoints are nonzero and length equals
  `missing`.
- `CoverageFile`: `path`, `statements`, then `branches`. Paths are unique,
  project-relative POSIX strings and files are ordered by descending total gaps then
  path.
- `CoverageError`: `code: CoverageErrorCode`, then `message: string`.

Coverage totals equal the sum of file counts. A passed/failed result is complete and
gate-eligible. Guidance is not gate-eligible and does not evaluate the threshold. An
error is partial, incomplete, non-gating, has null totals/empty files, a non-null
error, and threshold skip reason `evidence_error`.

For thresholds, `configured=true` requires a finite numeric value; false requires a
null value. `evaluated=true` requires a boolean result and null skip reason;
unevaluated requires null `passed` and one non-null skip reason. Every non-error result
has complete totals, null error, and a trusted supported stable Coverage version.
Error-version correlations are exact:

- `unsupported_version`: null or a stable version outside the supported range;
- `unsupported_python`, `module_unavailable`, or `preflight_invalid`: null;
- `spawn_failed` or `terminated_by_signal`: null or a supported stable version; and
- every other Coverage error: a supported stable version.

Repository-dependency and preparation correlations are also exact:

| Cause | Coverage error/version/helper |
| --- | --- |
| Missing Coverage dependency | `module_unavailable`; null version; no `coverage_json` helper. |
| Incompatible Coverage dependency | `unsupported_version`; known incompatible version; no `coverage_json` helper. |
| Shadowed, unusable, or unobserved Coverage dependency | `preflight_invalid`; null version; no `coverage_json` helper. |
| Environment/pytest preparation failure, unavailable pytest, or an early pytest setup/evidence failure that owns Coverage | `preflight_invalid`; null version; no `coverage_json` helper. |

Thus unavailable Coverage never starts the JSON helper. A pytest preparation owner
supersedes the Coverage-dependency mapping because no instrumentable pytest primary
can establish Coverage evidence.

When Coverage is available, its probed package is copied through bounded no-follow
reads into the held run workspace before pytest. The `coverage_json` argv truthfully
records an absolute pinned uv path and a staged JSON launcher; the staged package and
launcher digests are revalidated immediately before that helper starts. Repository
Coverage shadows and later mutation at the original `.venv` origin cannot supply the
trusted JSON producer.

| Coverage status | Required correlation |
| --- | --- |
| `passed` | Complete scope/evidence and eligible; a configured threshold is evaluated and true, or an unconfigured threshold is skipped as `not_configured`. |
| `failed` | Complete scope/evidence and eligible; configured threshold evaluated false with no skip reason. |
| `guidance` | Valid complete measurement but non-gating; threshold is not evaluated and has a non-error skip reason. |
| `error` | Partial/incomplete/non-gating; null totals, empty files, non-null error, and `evidence_error`. |

Report-context validation derives Coverage scope, eligibility, skip reason, and
status rather than trusting them independently. Scope is complete exactly when
planned Coverage, pytest scope, and Coverage evidence are complete. Eligibility also
requires strict-aggregate mode, target-free passing complete pytest evidence, no
scope reason, and no Test Shortcut. Skip-reason priority is `evidence_error`,
`not_configured`, `no_tests_collected`, `pytest_incomplete`, `pytest_failed`,
`partial_run`, then `focused_run`; eligible threshold false yields `failed`, eligible
otherwise yields `passed`, and non-eligible valid evidence yields `guidance`.

## Complete enum and code registry

### Planning and report values

- `PlanningErrorCodeV2`: `invalid_arguments`, `invalid_project_config`,
  `invalid_test_shortcut`, `unknown_check`, `unknown_test_shortcut`,
  `unknown_target`, `coverage_configuration_required`,
  `unsafe_unlocked_execution`, `uv_project_required`, `internal_planning_error`.
- `ProcessRole`: `primary`, `pytest_preflight`, `coverage_preflight`,
  `coverage_json`, `repository_safety`, `uv_version`, `environment_probe`,
  `dependency_probe`.
- `EnvironmentErrorCode`: `repository_lock_missing`, `uv_unavailable`,
  `repository_environment_failed`, `repository_python_unsupported`,
  `unsafe_repository_environment`, `environment_evidence_invalid`,
  `repository_state_changed`.
- `CheckErrorCodeV2`: `spawn_failed`, `terminated_by_signal`,
  `pytest_preflight_failed`, `pytest_evidence_error`, `coverage_preflight_failed`,
  `missing_primary_process`, `cleanup_failed`, `repository_environment_unavailable`,
  `check_dependency_missing`, `check_dependency_incompatible`,
  `check_dependency_shadowed`, `check_dependency_unusable`,
  `check_start_evidence_invalid`, `check_execution_failed`.
- `AdvisoryCode`: `coverage_not_configured`, `coverage_threshold_not_applied`,
  `missing_test_reason`, `output_truncated`.

### Pytest values

- `PytestErrorCode`: `unsupported_python`, `module_unavailable`,
  `unsupported_version`, `preflight_invalid`, `unsupported_parallelism`,
  `unsupported_retries`, `exit_code_mismatch`, `not_started`, `spawn_failed`,
  `terminated_by_signal`, `artifact_missing`, `artifact_invalid`,
  `artifact_not_finalized`, `session_incomplete`, `interrupted`, `internal_error`,
  `usage_error`, `unknown_exit_code`.
- `TestScopeReason`: `planned_selector`, `effective_narrowing_option`,
  `unclassified_external_option`, `deselected_tests`, `collection_reduced`,
  `incomplete_session`.
- Special outcomes: `skipped`, `xfailed`, `xpassed`.

### Coverage values

- `CoverageThresholdSkipReason`: `evidence_error`, `not_configured`, `focused_run`,
  `partial_run`, `pytest_failed`, `pytest_incomplete`, `no_tests_collected`.
- `CoverageErrorCode`: `unsupported_python`, `module_unavailable`,
  `unsupported_version`, `preflight_invalid`, `spawn_failed`,
  `terminated_by_signal`, `unsupported_parallelism`, `data_missing`,
  `unexpected_parallel_data`, `generation_failed`, `artifact_missing`,
  `artifact_invalid`.

## Cross-object invariants and correlation rules

- `overall_status: "error"` and `complete: false` follow any planning,
  environment, dependency, process, cleanup, artifact, or integrity error.
- Complete findings can produce `overall_status: "failed"`, `complete: true`.
  Otherwise a complete clean run is `passed`/`true`.
- Public exit priority is error `2`, complete failure `1`, pass `0`; a positive child
  exit remains process evidence and does not replace this result.
- Environment-wide failure accounts for every selected check as an error with no
  primary/start/execution evidence. Selected pytest/Coverage get typed nested errors.
- `repository_environment.python` and `path` stay null until syntactically valid
  environment-probe evidence exists. Once observed they remain present even if a
  later safety/version error occurs.
- `execution_environment: "repository"` proves trusted launcher dispatch inside the
  prepared environment, not that the module completed. Without valid start evidence
  it is null.
- The Repository Python in every start marker equals the prepared Repository Python.
- Missing pytest prevents pytest and requested Coverage. Missing Coverage does not
  suppress pytest; Coverage and the overall run remain errors.
- `pytest.scope: "complete"` has no scope reasons. Partial evidence lists every
  applicable reason.
- Coverage is gate-eligible only for complete evidence from a passing, complete,
  target-free pytest result with no scope reasons in `strict_aggregate` mode.
- `scope="partial"`, `status="guidance"`, and `gate_eligible=false` never proves a
  threshold pass, even when measured percentage exceeds the configured value.

## Complete examples

All examples include every field required for their shape. Paths and versions are
representative.

### Planning error

```json
{
  "schema_version": 2,
  "kind": "planning_error",
  "overall_status": "error",
  "complete": false,
  "tool_environment": {
    "pyrepo_check_version": "0.1.0",
    "python": {
      "implementation": "cpython",
      "version": [3, 13, 15],
      "executable": "/tool/bin/python"
    }
  },
  "repository_environment": null,
  "error": {
    "code": "unsafe_unlocked_execution",
    "message": "--no-frozen is incompatible with repository-safe execution.",
    "hint": "Update uv.lock explicitly, then rerun without --no-frozen."
  }
}
```

### Missing-lock run

```json
{
  "schema_version": 2,
  "kind": "run",
  "project_root": "/project",
  "mode": "focused",
  "overall_status": "error",
  "complete": false,
  "tool_environment": {
    "pyrepo_check_version": "0.1.0",
    "python": {
      "implementation": "cpython",
      "version": [3, 13, 15],
      "executable": "/tool/bin/python"
    }
  },
  "repository_environment": {
    "manager": "uv",
    "manager_version": null,
    "path": null,
    "python_selection": {"kind": "default", "request": null},
    "python": null,
    "lock": {"path": "/project/uv.lock", "status": "missing"},
    "dependency_selection": "default",
    "mutation_protection": "unobserved",
    "dependencies": [
      {
        "name": "ty",
        "module": "ty",
        "required": ">=0.0.35,<0.1",
        "status": "unobserved",
        "version": null,
        "origin": null,
        "process": null,
        "error": null
      }
    ],
    "processes": [],
    "error": {
      "code": "repository_lock_missing",
      "message": "uv.lock is required.",
      "hint": "Create and commit uv.lock outside pyrepo-check, then retry."
    }
  },
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
      "status": "error",
      "execution_environment": null,
      "analysis_python_authority": null,
      "start_evidence": null,
      "processes": [],
      "error": {
        "code": "repository_environment_unavailable",
        "message": "Ty did not run because the Repository Environment is unavailable.",
        "hint": "Resolve the Repository Environment error, then retry."
      }
    }
  ],
  "pytest": null,
  "coverage": null,
  "advisories": []
}
```

### Dependency-error continuation run

Ty is missing, but the independent Bandit check still executes and passes.

```json
{
  "schema_version": 2,
  "kind": "run",
  "project_root": "/project",
  "mode": "focused",
  "overall_status": "error",
  "complete": false,
  "tool_environment": {
    "pyrepo_check_version": "0.1.0",
    "python": {
      "implementation": "cpython",
      "version": [3, 13, 15],
      "executable": "/tool/bin/python"
    }
  },
  "repository_environment": {
    "manager": "uv",
    "manager_version": "0.10.12",
    "path": "/project/.venv",
    "python_selection": {"kind": "explicit", "request": "3.12"},
    "python": {
      "implementation": "cpython",
      "version": [3, 12, 11],
      "executable": "/project/.venv/bin/python"
    },
    "lock": {"path": "/project/uv.lock", "status": "current"},
    "dependency_selection": "default",
    "mutation_protection": "protected_files",
    "dependencies": [
      {
        "name": "ty",
        "module": "ty",
        "required": ">=0.0.35,<0.1",
        "status": "missing",
        "version": null,
        "origin": null,
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"ty\",\"module\":\"ty\",\"status\":\"missing\",\"version\":null,\"origin\":null,\"diagnostic\":\"distribution is missing\"}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": {
          "code": "check_dependency_missing",
          "message": "Ty requires repository dependency ty>=0.0.35,<0.1, but it is missing.",
          "hint": "Add a compatible ordinary ty distribution to the repository lock, then retry."
        }
      },
      {
        "name": "bandit",
        "module": "bandit",
        "required": ">=1.9,<2",
        "status": "available",
        "version": "1.9.2",
        "origin": "/project/.venv/lib/python3.12/site-packages/bandit/__init__.py",
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"bandit\",\"module\":\"bandit\",\"status\":\"available\",\"version\":\"1.9.2\",\"origin\":\"/project/.venv/lib/python3.12/site-packages/bandit/__init__.py\",\"diagnostic\":null}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": null
      }
    ],
    "processes": [
      {
        "role": "uv_version",
        "argv": ["uv", "--version"],
        "cwd": "/project",
        "outcome": "exited",
        "exit_code": 0,
        "signal": null,
        "duration_ms": 2,
        "stdout": {"captured": true, "text": "uv 0.10.12\n", "truncated": false, "omitted_bytes": 0},
        "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
        "error_message": null
      },
      {
        "role": "environment_probe",
        "argv": ["uv", "run", "--locked", "--python", "3.12", "python", "-c", "environment-probe"],
        "cwd": "/project",
        "outcome": "exited",
        "exit_code": 0,
        "signal": null,
        "duration_ms": 20,
        "stdout": {"captured": true, "text": "{\"schema_version\":1,\"implementation\":\"cpython\",\"version\":[3,12,11],\"executable\":\"/project/.venv/bin/python\",\"environment_root\":\"/project/.venv\"}\n", "truncated": false, "omitted_bytes": 0},
        "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
        "error_message": null
      }
    ],
    "error": null
  },
  "selection": {
    "checks": ["ty", "bandit"],
    "targets": [],
    "test_shortcut": null,
    "pytest_args": null,
    "planned_test_scope": "not_selected",
    "planned_coverage_scope": "not_requested"
  },
  "checks": [
    {
      "name": "ty",
      "status": "error",
      "execution_environment": null,
      "analysis_python_authority": null,
      "start_evidence": null,
      "processes": [],
      "error": {
        "code": "check_dependency_missing",
        "message": "Ty requires repository dependency ty>=0.0.35,<0.1, but it is missing.",
        "hint": "Add a compatible ordinary ty distribution to the repository lock, then retry."
      }
    },
    {
      "name": "bandit",
      "status": "passed",
      "execution_environment": "repository",
      "analysis_python_authority": null,
      "start_evidence": {
        "schema_version": 1,
        "check": "bandit",
        "module": "bandit",
        "arguments_sha256": "c38177bd00cd4bfe18ca6d92a316fd1f2d487d74bda294461b4a3d2015a15d3d",
        "python": {
          "implementation": "cpython",
          "version": [3, 12, 11],
          "executable": "/project/.venv/bin/python"
        }
      },
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "/run/check-launcher.py", "--check", "bandit", "--module", "bandit", "--", "-c", "pyproject.toml", "-r", "."],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 30,
          "stdout": {"captured": true, "text": "No issues identified.\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
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

### Successful strict aggregate

This compact complete example shows all five checks, complete pytest evidence, and a
passing native Coverage threshold.

```json
{
  "schema_version": 2,
  "kind": "run",
  "project_root": "/project",
  "mode": "strict_aggregate",
  "overall_status": "passed",
  "complete": true,
  "tool_environment": {
    "pyrepo_check_version": "0.1.0",
    "python": {
      "implementation": "cpython",
      "version": [3, 13, 15],
      "executable": "/tool/bin/python"
    }
  },
  "repository_environment": {
    "manager": "uv",
    "manager_version": "0.10.12",
    "path": "/project/.venv",
    "python_selection": {"kind": "explicit", "request": "3.12"},
    "python": {
      "implementation": "cpython",
      "version": [3, 12, 11],
      "executable": "/project/.venv/bin/python"
    },
    "lock": {"path": "/project/uv.lock", "status": "current"},
    "dependency_selection": "default",
    "mutation_protection": "protected_files",
    "dependencies": [
      {
        "name": "ruff",
        "module": "ruff",
        "required": ">=0.15,<1",
        "status": "available",
        "version": "0.15.2",
        "origin": "/project/.venv/lib/python3.12/site-packages/ruff/__init__.py",
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"ruff\",\"module\":\"ruff\",\"status\":\"available\",\"version\":\"0.15.2\",\"origin\":\"/project/.venv/lib/python3.12/site-packages/ruff/__init__.py\",\"diagnostic\":null}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": null
      },
      {
        "name": "ty",
        "module": "ty",
        "required": ">=0.0.35,<0.1",
        "status": "available",
        "version": "0.0.35",
        "origin": "/project/.venv/lib/python3.12/site-packages/ty/__init__.py",
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"ty\",\"module\":\"ty\",\"status\":\"available\",\"version\":\"0.0.35\",\"origin\":\"/project/.venv/lib/python3.12/site-packages/ty/__init__.py\",\"diagnostic\":null}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": null
      },
      {
        "name": "bandit",
        "module": "bandit",
        "required": ">=1.9,<2",
        "status": "available",
        "version": "1.9.2",
        "origin": "/project/.venv/lib/python3.12/site-packages/bandit/__init__.py",
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"bandit\",\"module\":\"bandit\",\"status\":\"available\",\"version\":\"1.9.2\",\"origin\":\"/project/.venv/lib/python3.12/site-packages/bandit/__init__.py\",\"diagnostic\":null}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": null
      },
      {
        "name": "pytest",
        "module": "pytest",
        "required": ">=8,<9",
        "status": "available",
        "version": "8.4.2",
        "origin": "/project/.venv/lib/python3.12/site-packages/pytest/__init__.py",
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"pytest\",\"module\":\"pytest\",\"status\":\"available\",\"version\":\"8.4.2\",\"origin\":\"/project/.venv/lib/python3.12/site-packages/pytest/__init__.py\",\"diagnostic\":null}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": null
      },
      {
        "name": "coverage",
        "module": "coverage",
        "required": ">=7.15,<8",
        "status": "available",
        "version": "7.15.4",
        "origin": "/project/.venv/lib/python3.12/site-packages/coverage/__init__.py",
        "process": {
          "role": "dependency_probe",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-c", "dependency-probe"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 5,
          "stdout": {"captured": true, "text": "{\"schema_version\":1,\"distribution\":\"coverage\",\"module\":\"coverage\",\"status\":\"available\",\"version\":\"7.15.4\",\"origin\":\"/project/.venv/lib/python3.12/site-packages/coverage/__init__.py\",\"diagnostic\":null}\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        "error": null
      }
    ],
    "processes": [
      {
        "role": "uv_version",
        "argv": ["uv", "--version"],
        "cwd": "/project",
        "outcome": "exited",
        "exit_code": 0,
        "signal": null,
        "duration_ms": 2,
        "stdout": {"captured": true, "text": "uv 0.10.12\n", "truncated": false, "omitted_bytes": 0},
        "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
        "error_message": null
      },
      {
        "role": "environment_probe",
        "argv": ["uv", "run", "--locked", "--python", "3.12", "python", "-c", "environment-probe"],
        "cwd": "/project",
        "outcome": "exited",
        "exit_code": 0,
        "signal": null,
        "duration_ms": 20,
        "stdout": {"captured": true, "text": "{\"schema_version\":1,\"implementation\":\"cpython\",\"version\":[3,12,11],\"executable\":\"/project/.venv/bin/python\",\"environment_root\":\"/project/.venv\"}\n", "truncated": false, "omitted_bytes": 0},
        "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
        "error_message": null
      }
    ],
    "error": null
  },
  "selection": {
    "checks": ["ruff", "annotations", "ty", "bandit", "pytest"],
    "targets": [],
    "test_shortcut": null,
    "pytest_args": [],
    "planned_test_scope": "complete",
    "planned_coverage_scope": "complete"
  },
  "checks": [
    {
      "name": "ruff",
      "status": "passed",
      "execution_environment": "repository",
      "analysis_python_authority": {"authority": "repository_tool", "pyrepo_check_override": null},
      "start_evidence": {
        "schema_version": 1,
        "check": "ruff",
        "module": "ruff",
        "arguments_sha256": "ae2a2403b9bfc3e0f06a0ceee1336152e854348bfb4cd69919e9e9d750c551b2",
        "python": {"implementation": "cpython", "version": [3, 12, 11], "executable": "/project/.venv/bin/python"}
      },
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "/run/check-launcher.py", "--evidence", "/run/ruff-start.json", "--check", "ruff", "--module", "ruff", "--", "check", "."],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 10,
          "stdout": {"captured": true, "text": "All checks passed!\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        }
      ],
      "error": null
    },
    {
      "name": "annotations",
      "status": "passed",
      "execution_environment": "repository",
      "analysis_python_authority": {"authority": "repository_tool", "pyrepo_check_override": null},
      "start_evidence": {
        "schema_version": 1,
        "check": "annotations",
        "module": "ruff",
        "arguments_sha256": "8b8c5d4551f64000ccd13f397c6267a64455f1339e4b4b9eb1bd92fde8c05e46",
        "python": {"implementation": "cpython", "version": [3, 12, 11], "executable": "/project/.venv/bin/python"}
      },
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "/run/check-launcher.py", "--evidence", "/run/annotations-start.json", "--check", "annotations", "--module", "ruff", "--", "check", ".", "--select", "ANN", "--output-format", "concise"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 10,
          "stdout": {"captured": true, "text": "All checks passed!\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        }
      ],
      "error": null
    },
    {
      "name": "ty",
      "status": "passed",
      "execution_environment": "repository",
      "analysis_python_authority": {"authority": "repository_tool", "pyrepo_check_override": null},
      "start_evidence": {
        "schema_version": 1,
        "check": "ty",
        "module": "ty",
        "arguments_sha256": "cf9959e387139d8c4d588b07647002a4cfd5afb9a07940dc204b0e9d49ab5215",
        "python": {"implementation": "cpython", "version": [3, 12, 11], "executable": "/project/.venv/bin/python"}
      },
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "/run/check-launcher.py", "--evidence", "/run/ty-start.json", "--check", "ty", "--module", "ty", "--", "check"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 10,
          "stdout": {"captured": true, "text": "All checks passed!\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        }
      ],
      "error": null
    },
    {
      "name": "bandit",
      "status": "passed",
      "execution_environment": "repository",
      "analysis_python_authority": null,
      "start_evidence": {
        "schema_version": 1,
        "check": "bandit",
        "module": "bandit",
        "arguments_sha256": "c38177bd00cd4bfe18ca6d92a316fd1f2d487d74bda294461b4a3d2015a15d3d",
        "python": {"implementation": "cpython", "version": [3, 12, 11], "executable": "/project/.venv/bin/python"}
      },
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "/run/check-launcher.py", "--evidence", "/run/bandit-start.json", "--check", "bandit", "--module", "bandit", "--", "-c", "pyproject.toml", "-r", "."],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 20,
          "stdout": {"captured": true, "text": "No issues identified.\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        }
      ],
      "error": null
    },
    {
      "name": "pytest",
      "status": "passed",
      "execution_environment": "repository",
      "analysis_python_authority": null,
      "start_evidence": {
        "schema_version": 1,
        "check": "pytest",
        "module": "coverage",
        "arguments_sha256": "b2357df350a2a4cab6ad605302eb43b08520953374836153cfa3439ede6bdc03",
        "python": {"implementation": "cpython", "version": [3, 12, 11], "executable": "/project/.venv/bin/python"}
      },
      "processes": [
        {
          "role": "primary",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "/run/check-launcher.py", "--evidence", "/run/pytest-start.json", "--check", "pytest", "--module", "coverage", "--", "run", "--rcfile=/project/pyproject.toml", "--data-file=/run/.coverage", "-m", "pytest", "-p", "_pyrepo_check_pytest_example"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 100,
          "stdout": {"captured": true, "text": "1 passed\n", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        },
        {
          "role": "coverage_json",
          "argv": ["uv", "run", "--locked", "--python", "/project/.venv/bin/python", "python", "-m", "coverage", "json", "--rcfile=/project/pyproject.toml", "--data-file=/run/report-input/coverage-data", "-o", "/run/coverage.json", "--keep-combined"],
          "cwd": "/project",
          "outcome": "exited",
          "exit_code": 0,
          "signal": null,
          "duration_ms": 20,
          "stdout": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "stderr": {"captured": true, "text": "", "truncated": false, "omitted_bytes": 0},
          "error_message": null
        }
      ],
      "error": null
    }
  ],
  "pytest": {
    "status": "passed",
    "complete": true,
    "scope": "complete",
    "scope_reasons": [],
    "pytest_version": "8.4.2",
    "exit_code": 0,
    "evidence": {
      "effective_args": [],
      "collected": 1,
      "deselected": 0,
      "counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0},
      "collection_errors": [],
      "collection_skips": [],
      "slowest": [{"nodeid": "tests/test_example.py::test_example", "duration_ms": 1}],
      "special_outcomes": []
    },
    "error": null
  },
  "coverage": {
    "status": "passed",
    "scope": "complete",
    "evidence_complete": true,
    "coverage_version": "7.15.4",
    "gate_eligible": true,
    "threshold": {
      "configured": true,
      "value": 100,
      "evaluated": true,
      "passed": true,
      "skipped_reason": null
    },
    "totals": {
      "statements": {"covered": 2, "missing": 0},
      "branches": {"covered": 0, "missing": 0}
    },
    "files": [
      {
        "path": "src/example.py",
        "statements": {"covered": 2, "missing": 0, "missing_lines": []},
        "branches": {"covered": 0, "missing": 0, "missing_arcs": []}
      }
    ],
    "error": null
  },
  "advisories": []
}
```
