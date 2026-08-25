# Repository Coverage Adoption (Milestone D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this repository consume its own C3 coverage capability by locking Coverage.py, defining a native production-code coverage policy, measuring the current line-and-branch baseline, and enforcing a stable strict-gate floor with explicit rounding semantics.

**Architecture:** Milestone D changes repository policy, not the C3 execution engine. `pyproject.toml` becomes the single source of truth for the development dependency, measured source tree, branch mode, single-process requirement, and aggregate threshold; `uv.lock` supplies reproducibility; existing configuration/planning/execution/reporting code consumes those native facts unchanged. Compatibility tests lock the repository contract, while a real `pyrepo-check --format json --all` run supplies the authoritative post-adoption baseline and threshold evidence.

**Tech Stack:** Python `>=3.13.15`, uv lock/dependency groups, Coverage.py `coverage[toml]>=7.15,<8`, pytest `>=8,<9`, TOML via stdlib `tomllib`, schema-v1 pyrepo-check JSON

**Spec:** `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`

## Global Constraints

- Base commit is `27ae28c2a6e114b08f88e90aee1d19b71f651722` on `main`; implement on `dev/repository-coverage-adoption-plan` only.
- Keep `[project].dependencies = []`. Coverage.py is development-only and must be declared exactly as `coverage[toml]>=7.15,<8`.
- Regenerate `uv.lock` after declaring the dependency and before adding `[tool.coverage]` configuration.
- Require Python `>=3.13.15`; do not change `.python-version`, Ruff target version, or consumer-version policy.
- Measure only production code under `src/pyrepo_check`; tests, generated files, worktrees, and virtual environments stay outside the denominator by source selection.
- Require `branch = true` and explicit `parallel = false`. Do not add `pytest-cov`, concurrency, patching, subprocess auto-start, combine behavior, or consumer-root artifacts.
- Use native `[tool.coverage.report].fail_under = 86.01` with `precision = 2`. The fresh preparation spike at the base commit measured 5,494 of 6,348 combined statement/branch opportunities, or `86.54694391934467%`; the final authority is a new post-configuration C3 report, not this preparation value.
- The gate compares Coverage.py's two-decimal rounded total to `86.01`. This intentionally rejects every raw total below `86%` and also the narrow raw interval from `86.000%` through `86.004999...%`; it avoids the false pass that `fail_under = 86` permits after rounding. At the measured 6,348-opportunity denominator, `5,494 / 6,388 = 86.005009...%` passes after adding 40 missing statements, while `5,494 / 6,389 = 85.991547...%` fails after adding 41.
- Preserve all C1-C3 commands, direct pytest targets, Test Shortcuts, one-primary-pytest execution, report schema version `1`, and artifact isolation.
- Do not edit runtime source under `src/pyrepo_check` unless a failing adoption test proves a C3 defect. If that happens, stop and amend this plan before broadening scope.
- Do not add dependency auditing, changed-code coverage, complexity, mutation, flaky repetition, CI, release tags, or package version changes.
- Do not edit `.agents/skills`, `.codex/skills`, or installed Codex/Antigravity skills. Skill synchronization is a separate post-D action.
- Do not merge or push. End with a clean, reviewed feature branch and request explicit integration approval.

---

## Preparation Evidence and Fixed Decisions

- Clean pre-adoption gate: Ruff, annotation enforcement, ty, Bandit, and `1,306` pytest tests passed; coverage was `null` with the expected `coverage_not_configured` advisory.
- Current Coverage.py documentation confirms that `source` includes unexecuted files, `branch = true` enables branch measurement, and `fail_under` exits `2` below the configured floor.
- A throwaway direct Coverage.py `7.15.4` run at the base commit measured:
  - statements: `3,989` covered, `483` missing, `4,472` total;
  - branches: `1,505` covered, `371` missing, `1,876` total;
  - combined: `5,494 / 6,348 = 86.54694391934467%`;
  - measured production files: `13`.
- Protected skill hashes before implementation are:
  - repository `.agents/skills/pyrepo-check/SKILL.md`: `c2d75822c0cb8b86473e9192374e53a964ce11424cf8a6d8e7b6a3b162330dbe`;
  - installed Codex `/Users/aaat/.codex/skills/pyrepo-check/SKILL.md`: `2f844a78eccb0a70d34a01f343e761cf243d54041304062da9eaf37757d955fc`;
  - `/Users/aaat/.agents/skills/pyrepo-check/SKILL.md` is absent and must not be created during D.
- Native policy is deliberately minimal:

```toml
[tool.coverage.run]
branch = true
source = ["src/pyrepo_check"]
parallel = false

[tool.coverage.report]
fail_under = 86.01
precision = 2
```

- `relative_files`, `show_missing`, and `omit` are unnecessary here: the run is local and isolated, JSON already carries exact gaps, and `source` excludes non-production trees. `precision = 2` is required because Coverage.py rounds before comparing the native threshold.

## File Responsibility Map

| File | Responsibility in D |
| --- | --- |
| `pyproject.toml` | Declares the development-only Coverage.py range and the native run/report policy. |
| `uv.lock` | Locks the supported Coverage.py artifact selected by uv. |
| `tests/test_compatibility.py` | Proves dependency placement, lock presence/range, source scope, branch mode, single-process policy, and threshold value. |
| `README.md` | Shows that this repository now auto-enables coverage on strict aggregate runs and records the verified baseline/floor. |
| `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md` | Marks D and acceptance criterion 12 implemented only after the real gate passes. |
| `docs/superpowers/plans/2026-08-25-repository-coverage-adoption-d.md` | Provides the task contract and evidence sequence. |

## Task Dependency Map

```text
Task 1: development dependency + lock
  -> Task 2: native measurement policy + fresh C3 baseline
    -> Task 3: strict threshold adoption
      -> Task 4: documentation + milestone boundary gate
```

Each task gets an implementation commit followed by an independent specification and quality review. Fix review findings before starting the next task.

## Plan Gate: Commit the Reviewed Execution Contract

Before Task 1:

1. Complete an independent read-only review of this plan against the design spec and current checkout.
2. Fix every material plan finding and repeat the review until it returns `SPEC PASS`, `QUALITY APPROVED`, and `ready for implementation`.
3. Verify only this plan is untracked and the diff is clean:

```bash
(
set -e
test "$(git status --short)" = "?? docs/superpowers/plans/2026-08-25-repository-coverage-adoption-d.md"
git diff --check
)
```

4. Commit the approved plan:

```bash
git add docs/superpowers/plans/2026-08-25-repository-coverage-adoption-d.md
git commit -m "docs: plan repository coverage adoption"
```

Task 1 begins only from that clean plan commit. This makes the complete branch review include its own execution contract.

### Task 1: Declare and Lock Coverage.py Before Configuration

**Files:**
- Modify: `tests/test_compatibility.py` near `test_pytest_fixture_dependencies_are_development_only`
- Modify: `pyproject.toml` under `[dependency-groups].dev`
- Modify: `uv.lock` through `uv lock`

**Interfaces:**
- Consumes: the existing development-only dependency policy and uv lock format.
- Produces: an installed, locked Coverage.py `7.x` with minor version at least `15`, available to later strict runs without changing runtime dependencies.

- [ ] **Step 1: Write the failing repository dependency/lock test**

Add this test beside the existing development-fixture dependency test:

```python
def test_repository_coverage_dependency_is_development_only_and_locked() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == []
    assert "coverage[toml]>=7.15,<8" in pyproject["dependency-groups"]["dev"]
    coverage_packages = [
        package for package in lockfile["package"] if package["name"] == "coverage"
    ]
    assert len(coverage_packages) == 1
    major, minor, *_ = (
        int(piece) for piece in coverage_packages[0]["version"].split(".")
    )
    assert major == 7
    assert minor >= 15
```

- [ ] **Step 2: Run the test and record RED**

Run:

```bash
uv run --frozen python -m pytest tests/test_compatibility.py::test_repository_coverage_dependency_is_development_only_and_locked -vv
```

Expected: FAIL because the development group has no Coverage.py entry and `uv.lock` has no `coverage` package.

- [ ] **Step 3: Add only the development dependency**

Insert the dependency alphabetically after Bandit:

```toml
[dependency-groups]
dev = [
    "bandit>=1.9,<2",
    "coverage[toml]>=7.15,<8",
    "pytest>=8,<9",
    "pytest-rerunfailures>=16.6,<17",
    "pytest-xdist>=3.8,<4",
    "ruff>=0.15,<1",
    "ty>=0.0.35,<0.1",
]
```

Do not add `[tool.coverage]` in this task.

- [ ] **Step 4: Regenerate and inspect the lock**

Run:

```bash
(
set -e
uv lock
uv sync --frozen
uv run --frozen python -m coverage --version
uv lock --check
)
```

Expected: uv locks one Coverage.py version satisfying `>=7.15,<8`; the module command succeeds; the project still has no native coverage configuration.

- [ ] **Step 5: Run focused verification**

Run:

```bash
(
set -e
uv run --frozen python -m pytest tests/test_compatibility.py::test_repository_coverage_dependency_is_development_only_and_locked tests/test_compatibility.py::test_pytest_fixture_dependencies_are_development_only -vv
uv run --frozen pyrepo-check annotations ty tests/test_compatibility.py
git diff --check
)
```

Expected: both tests pass; annotations and ty pass; `[project].dependencies` remains empty.

- [ ] **Step 6: Commit Task 1**

```bash
git add pyproject.toml uv.lock tests/test_compatibility.py
git commit -m "build: lock coverage for repository checks"
```

- [ ] **Step 7: Pass the Task 1 review gate**

Generate a review package from the clean plan commit to the Task 1 commit. Give a fresh read-only reviewer the Task 1 brief, implementer report, diff package, and Global Constraints. Require both `Spec compliant` and `Task quality: Approved`, with file/line findings grouped by severity. Fix and re-review every Critical, Important, or confirmed specification finding before Task 2. Record the reviewed range and exact resolved Coverage.py version in the SDD ledger.

### Task 2: Adopt Native Branch Measurement and Record the Fresh Baseline

**Files:**
- Modify: `tests/test_compatibility.py` beside the Task 1 contract test
- Modify: `pyproject.toml` after `[tool.pytest.ini_options]`

**Interfaces:**
- Consumes: Task 1's installed Coverage.py and C3's existing native configuration loader.
- Produces: `ProjectConfig.coverage` for this repository, automatic strict-aggregate coverage, and authoritative schema-v1 baseline counts with no configured threshold yet.

- [ ] **Step 1: Write the failing native measurement-policy test**

Add:

```python
def test_repository_native_coverage_measurement_policy_is_explicit() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["coverage"]["run"] == {
        "branch": True,
        "source": ["src/pyrepo_check"],
        "parallel": False,
    }
```

- [ ] **Step 2: Run the test and record RED**

Run:

```bash
uv run --frozen python -m pytest tests/test_compatibility.py::test_repository_native_coverage_measurement_policy_is_explicit -vv
```

Expected: FAIL because `[tool.coverage]` is absent.

- [ ] **Step 3: Add measurement configuration without a threshold**

Add exactly:

```toml
[tool.coverage.run]
branch = true
source = ["src/pyrepo_check"]
parallel = false
```

Do not add `[tool.coverage.report]` yet. Do not regenerate `uv.lock`; Task 1 already locked the dependency before configuration activation.

- [ ] **Step 4: Run focused configuration/planning verification**

Run:

```bash
(
set -e
uv run --frozen python -m pytest tests/test_compatibility.py::test_repository_native_coverage_measurement_policy_is_explicit tests/test_config.py tests/test_planning.py -q
uv run --frozen pyrepo-check annotations ty tests/test_compatibility.py
uv lock --check
)
```

Expected: the policy test and existing C3 configuration/planning matrix pass; the lock remains unchanged.

- [ ] **Step 5: Run the real C3 baseline and assert the complete evidence contract**

Run:

```bash
(
set -e
d_coverage_version="$(uv run --frozen python -c 'import coverage; print(coverage.__version__)')"
set -o pipefail
uv run --frozen pyrepo-check --format json --all |
  jq -e --arg coverage_version "$d_coverage_version" '
    select(
      .schema_version == 1 and
      .kind == "run" and
      .overall_status == "passed" and
      .complete == true and
      .selection.planned_coverage_scope == "complete" and
      [.checks[].name] == ["ruff", "annotations", "ty", "bandit", "pytest"] and
      ([.checks[].status] | all(. == "passed")) and
      .pytest.status == "passed" and
      .pytest.complete == true and
      .pytest.evidence.collected == 1308 and
      .pytest.evidence.counts == {
        "passed": 1308, "failed": 0, "errors": 0,
        "skipped": 0, "xfailed": 0, "xpassed": 0
      } and
      .coverage.status == "passed" and
      .coverage.scope == "complete" and
      .coverage.evidence_complete == true and
      .coverage.coverage_version == $coverage_version and
      .coverage.gate_eligible == true and
      .coverage.threshold == {
        "configured": false, "value": null, "evaluated": false,
        "passed": null, "skipped_reason": "not_configured"
      } and
      .coverage.totals == {
        "statements": {"covered": 3989, "missing": 483},
        "branches": {"covered": 1505, "missing": 371}
      } and
      (.coverage.files | length) == 13 and
      (all(.coverage.files[]; .path | startswith("src/pyrepo_check/"))) and
      ([.coverage.files[].statements.missing_lines | length] | add) == 483 and
      ([.coverage.files[].branches.missing_arcs | length] | add) == 371
    )
    | {
        schema_version, overall_status, complete,
        pytest: {status: .pytest.status, collected: .pytest.evidence.collected},
        coverage: {
          status: .coverage.status, coverage_version: .coverage.coverage_version,
          scope: .coverage.scope, evidence_complete: .coverage.evidence_complete,
          gate_eligible: .coverage.gate_eligible, threshold: .coverage.threshold,
          totals: .coverage.totals, measured_files: (.coverage.files | length),
          missing_line_entries: ([.coverage.files[].statements.missing_lines | length] | add),
          missing_arc_entries: ([.coverage.files[].branches.missing_arcs | length] | add)
        }
      }
  '
)
```

Expected:

- the pipeline exits `0`; `pipefail` makes either a failed pyrepo-check run or a failed JSON assertion fail the step;
- schema version `1`, run kind, `overall_status: "passed"`, and `complete: true`;
- `selection.planned_coverage_scope: "complete"`;
- all five checks pass; pytest is complete with `1,308` collected/passed tests and no other outcomes;
- coverage `status: "passed"`, `scope: "complete"`, `evidence_complete: true`, and `gate_eligible: true`;
- coverage version equals the exact Task 1 locked version;
- threshold `configured: false`, `value: null`, `evaluated: false`, `passed: null`, `skipped_reason: "not_configured"`;
- statements `3,989` covered / `483` missing and branches `1,505` covered / `371` missing;
- exactly `13` production files, with missing-line and missing-arc arrays totaling `483` and `371` entries respectively.

If post-configuration counts differ, stop and investigate scope/configuration differences. Do not silently choose a lower threshold or copy the preparation spike into documentation.

- [ ] **Step 6: Prove artifact isolation and clean status**

Run:

```bash
(
set -e
d_coverage_artifacts="$(find . \
  \( -path './.git' -o -path './.venv' -o -path './.worktrees' \) -prune -o \
  \( -name '.coverage*' -o -name 'coverage.json' \) -print)"
test -z "$d_coverage_artifacts"
test "$(git status --short)" = $' M pyproject.toml\n M tests/test_compatibility.py'
git diff --check
)
```

Expected: the artifact assertion passes; status lists only `pyproject.toml` and `tests/test_compatibility.py`; diff check passes.

- [ ] **Step 7: Commit Task 2**

```bash
git add pyproject.toml tests/test_compatibility.py
git commit -m "build: adopt native coverage measurement"
```

- [ ] **Step 8: Pass the Task 2 review gate**

Generate a review package from the Task 1 commit to the Task 2 commit. Give a fresh read-only reviewer the Task 2 brief, implementer report, diff package, and Global Constraints. Require both `Spec compliant` and `Task quality: Approved`. The reviewer must verify the asserted public JSON evidence in the report, not rerun the full suite. Fix and re-review every Critical, Important, or confirmed specification finding before Task 3.

### Task 3: Enforce the Verified Repository Coverage Floor

**Files:**
- Modify: `tests/test_compatibility.py` beside the native measurement-policy test
- Modify: `pyproject.toml` after `[tool.coverage.run]`

**Interfaces:**
- Consumes: Task 2's complete coverage evidence and measured `86.54694391934467%` baseline.
- Produces: native `fail_under = 86.01` with `precision = 2`, evaluated only by eligible target-free strict aggregate runs through existing C3 policy.

- [ ] **Step 1: Write the failing threshold-policy test**

Add:

```python
def test_repository_coverage_threshold_is_explicit() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["coverage"]["report"] == {
        "fail_under": 86.01,
        "precision": 2,
    }
```

- [ ] **Step 2: Run the test and record RED**

Run:

```bash
uv run --frozen python -m pytest tests/test_compatibility.py::test_repository_coverage_threshold_is_explicit -vv
```

Expected: FAIL because `[tool.coverage.report]` is absent.

- [ ] **Step 3: Add the native threshold**

Add exactly:

```toml
[tool.coverage.report]
fail_under = 86.01
precision = 2
```

- [ ] **Step 4: Run focused tests and typing checks**

Run:

```bash
(
set -e
uv run --frozen python -m pytest tests/test_compatibility.py::test_repository_coverage_dependency_is_development_only_and_locked tests/test_compatibility.py::test_repository_native_coverage_measurement_policy_is_explicit tests/test_compatibility.py::test_repository_coverage_threshold_is_explicit tests/test_config.py tests/test_planning.py tests/test_coverage_evidence.py tests/test_reporting.py -q
uv run --frozen pyrepo-check annotations ty tests/test_compatibility.py
uv lock --check
)
```

Expected: all focused tests, annotations, ty, and lock validation pass.

- [ ] **Step 5: Prove the strict threshold through public JSON**

Run:

```bash
(
set -e
d_coverage_version="$(uv run --frozen python -c 'import coverage; print(coverage.__version__)')"
set -o pipefail
uv run --frozen pyrepo-check --format json --all |
  jq -e --arg coverage_version "$d_coverage_version" '
    select(
      .schema_version == 1 and
      .kind == "run" and
      .overall_status == "passed" and
      .complete == true and
      [.checks[].name] == ["ruff", "annotations", "ty", "bandit", "pytest"] and
      ([.checks[].status] | all(. == "passed")) and
      .pytest.status == "passed" and
      .pytest.complete == true and
      .pytest.evidence.collected == 1309 and
      .pytest.evidence.counts == {
        "passed": 1309, "failed": 0, "errors": 0,
        "skipped": 0, "xfailed": 0, "xpassed": 0
      } and
      .coverage.status == "passed" and
      .coverage.scope == "complete" and
      .coverage.evidence_complete == true and
      .coverage.coverage_version == $coverage_version and
      .coverage.gate_eligible == true and
      .coverage.threshold == {
        "configured": true, "value": 86.01, "evaluated": true,
        "passed": true, "skipped_reason": null
      } and
      .coverage.totals == {
        "statements": {"covered": 3989, "missing": 483},
        "branches": {"covered": 1505, "missing": 371}
      } and
      (.coverage.files | length) == 13 and
      ([.coverage.files[].statements.missing_lines | length] | add) == 483 and
      ([.coverage.files[].branches.missing_arcs | length] | add) == 371
    )
    | {
        overall_status, complete,
        pytest: {status: .pytest.status, collected: .pytest.evidence.collected},
        coverage: {
          status: .coverage.status, scope: .coverage.scope,
          gate_eligible: .coverage.gate_eligible, threshold: .coverage.threshold,
          totals: .coverage.totals, measured_files: (.coverage.files | length)
        }
      }
  '
)
```

Expected: exit `0`; coverage status `passed`; scope `complete`; gate eligible; threshold exactly `{"configured":true,"value":86.01,"evaluated":true,"passed":true,"skipped_reason":null}`; pytest has `1,309` collected/passed tests; counts and exact gap-array totals match Task 2.

- [ ] **Step 6: Prove the rounding boundary with real eligible runs**

Use `apply_patch` to create untracked `src/pyrepo_check/_milestone_d_boundary_probe.py` containing exactly 40 executable assignment statements, `VALUE_01 = 1` through `VALUE_40 = 40`. Do not import it. Run:

```bash
(
set -e
set -o pipefail
uv run --frozen pyrepo-check --format json --all |
  jq -e '
    .schema_version == 1 and
    .kind == "run" and
    .overall_status == "passed" and
    .complete == true and
    .pytest.status == "passed" and
    .pytest.evidence.collected == 1309 and
    .coverage.status == "passed" and
    .coverage.scope == "complete" and
    .coverage.evidence_complete == true and
    .coverage.gate_eligible == true and
    .coverage.threshold == {
      "configured": true, "value": 86.01, "evaluated": true,
      "passed": true, "skipped_reason": null
    } and
    .coverage.totals == {
      "statements": {"covered": 3989, "missing": 523},
      "branches": {"covered": 1505, "missing": 371}
    } and
    (.coverage.files | length) == 14
  '
)
```

Expected: exit `0`; `5,494 / 6,388 = 86.005009...%` rounds to `86.01` and passes.

Use `apply_patch` to append exactly `VALUE_41 = 41`. Capture the expected nonzero command without hiding it:

```bash
d_failure_json="$(mktemp "${TMPDIR:-/tmp}/pyrepo-check-d-failure.XXXXXX")"
d_failure_status=0
uv run --frozen pyrepo-check --format json --all > "$d_failure_json" || d_failure_status=$?
d_assert_status=0
jq -e '
  .schema_version == 1 and
  .kind == "run" and
  .overall_status == "failed" and
  .complete == true and
  .pytest.status == "passed" and
  .pytest.evidence.collected == 1309 and
  .coverage.status == "failed" and
  .coverage.scope == "complete" and
  .coverage.evidence_complete == true and
  .coverage.gate_eligible == true and
  .coverage.threshold == {
    "configured": true, "value": 86.01, "evaluated": true,
    "passed": false, "skipped_reason": null
  } and
  .coverage.totals == {
    "statements": {"covered": 3989, "missing": 524},
    "branches": {"covered": 1505, "missing": 371}
  } and
  (.coverage.files | length) == 14
' "$d_failure_json" || d_assert_status=$?
d_unlink_status=0
unlink "$d_failure_json" || d_unlink_status=$?
test "$d_failure_status" -eq 2 && \
  test "$d_assert_status" -eq 0 && \
  test "$d_unlink_status" -eq 0
```

Expected: the pyrepo-check process exits exactly `2`; the JSON assertions pass; the raw total is `5,494 / 6,389 = 85.991547...%`, which rounds to `85.99` and fails.

Delete only the untracked probe file with `apply_patch`. Then repeat Step 5's clean public JSON assertion and the repository-wide artifact search. Require the original 13-file counts, a passing threshold, and no probe/artifact in `git status --short` before committing.

- [ ] **Step 7: Commit Task 3**

```bash
git add pyproject.toml tests/test_compatibility.py
git commit -m "build: set repository coverage floor"
```

- [ ] **Step 8: Pass the Task 3 review gate**

Generate a review package from the Task 2 commit to the Task 3 commit. Give a fresh read-only reviewer the Task 3 brief, implementer report (including the 40/41-statement boundary evidence), diff package, and Global Constraints. Require both `Spec compliant` and `Task quality: Approved`. Fix and re-review every Critical, Important, or confirmed specification finding before Task 4.

### Task 4: Document D and Run the Milestone Boundary Gate

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Verify unchanged: `.agents/skills/pyrepo-check/SKILL.md`
- Verify unchanged: installed Codex/Antigravity skill locations

**Interfaces:**
- Consumes: Tasks 1-3 plus the authoritative public JSON report.
- Produces: truthful D status, copy-paste self-coverage commands, recorded baseline/floor, and acceptance-criterion-12 evidence without performing post-D skill synchronization.

- [ ] **Step 1: Run the complete pre-documentation D gate**

Run:

```bash
(
set -e
uv run --frozen python -m pytest -q
uv run --frozen pyrepo-check --all
uv lock --check
git diff --check
)
```

Expected: Ruff, annotation enforcement, ty, Bandit, pytest, and coverage pass; pytest executes once in the strict gate; coverage reports a complete result and an evaluated passing threshold.

- [ ] **Step 2: Update README only with verified D behavior**

Make these exact content changes:

- state that this repository now declares Coverage.py in the development group;
- show `pyrepo-check --all` as the normal strict self-check and `pyrepo-check --format json --all` as the agent-readable form;
- record the verified post-configuration statement/branch counts, combined baseline, and `fail_under = 86.01` / `precision = 2` floor;
- explain that the floor is below the fresh baseline, rejects raw totals below 86% despite native rounding, and exact missing lines/arcs remain the action payload;
- replace the pre-D skill-deferral wording with: repository and installed Agent Skill synchronization is the next separate post-D action and remains unchanged in this milestone.

- [ ] **Step 3: Mark only D and criterion 12 implemented in the design spec**

Update both status tables and delivery prose to say Milestones A-D are implemented and verified. Record:

- exact dependency range and resolved Coverage.py version;
- native `branch`, `source`, `parallel`, threshold, and precision settings;
- authoritative post-configuration totals and passing threshold evidence;
- full pytest and strict-gate count of `1,309` collected/passed tests;
- Agent Skill synchronization remains a separate next action;
- acceptance criterion 13 remains outside scope.

Do not change the normative C1-C3 behavior contract.

- [ ] **Step 4: Run documentation and protected-boundary checks**

Run:

```bash
(
set -e
rg -n "Milestone D|fail_under|coverage\[toml\]|Agent Skill" README.md docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git diff --check
git diff --exit-code 27ae28c2a6e114b08f88e90aee1d19b71f651722 -- .agents/skills .codex/skills
test -z "$(git status --short -- .agents/skills .codex/skills)"
test "$(shasum -a 256 .agents/skills/pyrepo-check/SKILL.md | awk '{print $1}')" = "c2d75822c0cb8b86473e9192374e53a964ce11424cf8a6d8e7b6a3b162330dbe"
test "$(shasum -a 256 /Users/aaat/.codex/skills/pyrepo-check/SKILL.md | awk '{print $1}')" = "2f844a78eccb0a70d34a01f343e761cf243d54041304062da9eaf37757d955fc"
test ! -e /Users/aaat/.agents/skills/pyrepo-check/SKILL.md
)
```

Expected: D is consistently implemented, the floor and dependency are documented, and skill paths have no diff.

- [ ] **Step 5: Run the exact final tree twice through public boundaries**

Run:

```bash
(
set -e
uv run --frozen pyrepo-check --all
d_coverage_version="$(uv run --frozen python -c 'import coverage; print(coverage.__version__)')"
set -o pipefail
uv run --frozen pyrepo-check --format json --all |
  jq -e --arg coverage_version "$d_coverage_version" '
    select(
      .schema_version == 1 and
      .kind == "run" and
      .overall_status == "passed" and
      .complete == true and
      [.checks[].name] == ["ruff", "annotations", "ty", "bandit", "pytest"] and
      ([.checks[].status] | all(. == "passed")) and
      .pytest.status == "passed" and
      .pytest.complete == true and
      .pytest.evidence.collected == 1309 and
      .pytest.evidence.counts == {
        "passed": 1309, "failed": 0, "errors": 0,
        "skipped": 0, "xfailed": 0, "xpassed": 0
      } and
      .coverage.status == "passed" and
      .coverage.scope == "complete" and
      .coverage.evidence_complete == true and
      .coverage.coverage_version == $coverage_version and
      .coverage.gate_eligible == true and
      .coverage.threshold == {
        "configured": true, "value": 86.01, "evaluated": true,
        "passed": true, "skipped_reason": null
      } and
      .coverage.totals == {
        "statements": {"covered": 3989, "missing": 483},
        "branches": {"covered": 1505, "missing": 371}
      } and
      (.coverage.files | length) == 13 and
      (all(.coverage.files[]; .path | startswith("src/pyrepo_check/"))) and
      ([.coverage.files[].statements.missing_lines | length] | add) == 483 and
      ([.coverage.files[].branches.missing_arcs | length] | add) == 371
    )
    | {
        schema_version, overall_status, complete,
        pytest: {status: .pytest.status, collected: .pytest.evidence.collected},
        coverage: {
          status: .coverage.status, coverage_version: .coverage.coverage_version,
          scope: .coverage.scope, evidence_complete: .coverage.evidence_complete,
          gate_eligible: .coverage.gate_eligible, threshold: .coverage.threshold,
          totals: .coverage.totals, measured_files: (.coverage.files | length),
          missing_line_entries: ([.coverage.files[].statements.missing_lines | length] | add),
          missing_arc_entries: ([.coverage.files[].branches.missing_arcs | length] | add)
        }
      }
  '
uv lock --check
git diff --check
d_coverage_artifacts="$(find . \
  \( -path './.git' -o -path './.venv' -o -path './.worktrees' \) -prune -o \
  \( -name '.coverage*' -o -name 'coverage.json' \) -print)"
test -z "$d_coverage_artifacts"
test "$(git status --short)" = $' M README.md\n M docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md'
)
```

Expected: both public runs pass with `1,309` collected/passed tests, complete non-null coverage, all exact totals/gap counts, and threshold `86.01` evaluated/passing; no coverage artifacts remain; only README/spec changes are uncommitted.

- [ ] **Step 6: Commit Task 4**

```bash
git add README.md docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "docs: record repository coverage adoption"
```

- [ ] **Step 7: Pass the Task 4 review gate**

Generate a review package from the Task 3 commit to the Task 4 commit. Give a fresh read-only reviewer the Task 4 brief, implementer report, diff package, and Global Constraints. Require both `Spec compliant` and `Task quality: Approved`. Fix and re-review every Critical, Important, or confirmed specification finding before the whole-branch review.

- [ ] **Step 8: Obtain independent final whole-branch review**

Review the complete range from `27ae28c2a6e114b08f88e90aee1d19b71f651722` to the Task 4 commit. Require:

- SPEC PASS / FAIL against Milestone D and acceptance criterion 12;
- QUALITY APPROVED / CHANGES REQUIRED;
- exact findings with file and line evidence;
- independent `uv run --frozen pyrepo-check --all` evidence;
- dependency/lock/config/status validation;
- proof `.agents/skills` and installed skills were not changed;
- explicit merge-readiness verdict.

Fix every material finding with RED/GREEN evidence and repeat review before calling D complete.

## Completion Evidence

Milestone D is complete only when all of these are simultaneously true at one clean branch HEAD:

- development dependency declares `coverage[toml]>=7.15,<8` and uv locks one supported version;
- runtime dependencies remain empty;
- native config measures `src/pyrepo_check` statements and branches in single-process mode;
- post-adoption public JSON is complete, non-null, and contains exact line/branch gaps;
- native threshold `86.01` with two-decimal precision is evaluated and passes on a target-free strict aggregate run, with a real 40/41-statement probe proving the intended rounding boundary;
- Ruff, annotation enforcement, ty, Bandit, pytest, coverage, lock, and diff gates pass;
- documentation records the fresh baseline rather than the preparation spike;
- Agent Skill files remain unchanged;
- independent review says SPEC PASS, QUALITY APPROVED, and ready to merge;
- the branch remains unmerged and unpushed pending explicit user approval.
