# Test Shortcuts C1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, project-owned names for repeatable pytest subsets so agents
can run the relevant tests quickly without weakening direct-target or strict-gate
behavior.

**Architecture:** `config.py` eagerly parses and validates immutable Test
Shortcut definitions. `planning.py` owns shortcut selection, conflict policy,
expansion, exact pytest argv, and planned test scope. `cli.py` only adapts
`--shortcut NAME` into a `RunRequest` and maps typed planning failures.
`reporting.py` projects the planner's authoritative shortcut metadata into the
existing schema-version-1 `Selection`; it does not infer arguments from process
commands. Execution remains unchanged and still runs the one planned pytest
command.

**Tech Stack:** Python 3.11+, stdlib `argparse`, `dataclasses`, `pathlib`, `re`,
`tomllib`, and `typing`; pytest 8; Ruff; ty; Bandit; uv.

**Spec:**
[`docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`](../specs/2026-08-24-agent-guidance-reporting-design.md)

## Global Constraints

- This plan implements **C1 Test Shortcuts only**. It does not implement the
  structured pytest evidence plugin, pytest preflight, slow/special test
  metrics, Coverage.py execution, coverage guidance/thresholds, dependency or
  lockfile changes, or repository coverage adoption.
- Keep schema version `1`. During C1, top-level `pytest` and `coverage` remain
  `null`, `planned_coverage_scope` remains `not_requested`, and every ordinary
  selected check still has exactly one `primary` process.
- Preserve the six check names, canonical check order, direct targets and pytest
  node IDs, target spelling/order/duplicates, `uv run` and `--frozen` policy,
  strict root targeting, target-only four-check behavior, and opt-in
  `annotations-fix` behavior.
- A valid shortcut is only an explicit pytest-only Focused Run. It cannot be
  combined with `--all`, a distinct second check, or direct targets. Repeated
  `pytest` positionals remain valid because current selection already
  deduplicates repeated check names.
- Validate every configured shortcut eagerly when loading project
  configuration. An invalid unused shortcut therefore blocks unrelated checks
  before execution; this keeps `ProjectConfig` fully validated and prevents
  repository configuration from having hidden invalid states.
- Every error rooted under `[tool.pyrepo-check.test-shortcuts]` uses planning
  code `invalid_test_shortcut`. Existing TOML, `[tool.pyrepo-check]`, Ruff-target,
  and Bandit-target errors remain `invalid_project_config`.
- After configuration succeeds, shortcut combination errors take precedence
  over name lookup. A conflicting unknown name is therefore
  `invalid_arguments`, not `unknown_test_shortcut`.
- The version-1 shortcut grammar admits project-relative test targets and exact
  `-m EXPRESSION` / `-k EXPRESSION` pairs only. It does not admit arbitrary
  pytest options, output plugins, early-exit flags, environment assignments, or
  shell syntax.
- Validate target containment using resolved paths, including symlinks. Reject
  only effective escapes; a lexical `..` that still resolves beneath the root
  is allowed. The project-relative directory `.` is valid. Preserve the
  configured token exactly after validation.
- Keep `argparse.ArgumentParser.parse_args`. Do not broaden the whole CLI with
  `parse_intermixed_args`. Both supported forms `--shortcut unit pytest` and
  `pytest --shortcut unit` must work; tests for conflicting direct targets use
  a parseable order such as `pytest tests/a.py --shortcut unit`.
- Add no runtime or development dependency and do not modify `execution.py`.
- Do not update installed Codex/Antigravity skills or the repository skill in
  `.agents/skills/pyrepo-check/`; the user explicitly deferred skill
  synchronization until after Milestone D. Update only repository README/spec
  documentation needed to make the shipped C1 CLI truthful.
- Do not include or modify the unrelated untracked `LICENSE` file in the main
  checkout.
- Use `apply_patch` for source, tests, and documentation. Use focused tests and
  `annotations ty` during each task; run the strict `--all` gate at the C1
  milestone boundary.

## Resolved Contract Decisions

| Question | C1 decision |
| --- | --- |
| Configuration representation | `ProjectConfig.test_shortcuts` is an immutable tuple of frozen `TestShortcut` values, each carrying a name and exact pytest-argument tuple. |
| Validation timing | All definitions are validated eagerly, including definitions not selected by the current command. |
| Error ownership | Any invalid `test-shortcuts` table/name/value/grammar/path is `invalid_test_shortcut`; unrelated project configuration remains `invalid_project_config`. |
| Selector operand | It must contain non-whitespace text and must not begin with `-`; accepted text is forwarded exactly. |
| Path containment | Resolve the path portion before the first `::`; require existence under the resolved project root and reject symlink/effective `..` escapes. Do not parse the node-ID suffix further. |
| Path inspection failure | Translate `ValueError`, `OSError`, or `RuntimeError` from path resolution/existence into `InvalidTestShortcutError` with `target path cannot be inspected safely`; no shortcut-rooted path failure may fall through to another planning code. |
| Unknown name | Message is `Unknown Test Shortcut: NAME`; configured names are Unicode-code-point sorted in the hint. With no definitions, the hint is `No Test Shortcuts are configured.` |
| CLI conflict | Code `invalid_arguments`; message `--shortcut requires an explicit pytest-only run with no direct targets or --all.`; hint `Use: pyrepo-check pytest --shortcut NAME`. |
| Conflict precedence | Eager config validation first, then CLI combination validation, then shortcut lookup. |
| Repeated pytest | `pytest pytest --shortcut unit` is valid and plans one pytest check. Only distinct selected checks create a conflict. |
| Option order | Preserve ordinary `parse_args`; support the two valid no-target placements without changing all option/positional intermixing semantics. |
| Planning authority | `RunPlan` carries `test_shortcut`, exact `pytest_args`, and `planned_test_scope`; reporting projects them directly. |
| Schema seam | `Selection.test_shortcut` may be non-null in schema v1, while top-level `pytest` and `coverage` remain null until their later feature slices. |
| Documentation boundary | README and delivery status are updated when C1 ships; all agent-skill copies remain deferred through Milestone D. |

## Baseline Status

| State | Evidence |
| --- | --- |
| Merged and pushed | Milestones A-B are on `main` at `a288256`; planner/executor separation, terminal/JSON report projection, schema-v1 validation, and 239 tests pass. |
| Planning branch | Delivery status was corrected in documentation-only commit `4e5f671`; no runtime behavior changed. |
| Designed only | No `TestShortcut` config model, parser, `--shortcut` option, planner expansion, or non-null `selection.test_shortcut` exists. |
| Explicitly deferred | Structured pytest evidence, coverage execution/guidance, coverage dependency/config adoption, and all installed/repository skill updates. |

## Plan Publication Gate

Commit this plan after independent review. Do not begin runtime Task 1 on the
planning branch unless the user explicitly chooses an execution workflow.

---

### Task 1: Load and Eagerly Validate Test Shortcut Configuration

**Files:**

- Modify: `src/pyrepo_check/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**

- Add frozen `TestShortcut(name: str, pytest_args: tuple[str, ...])`.
- Add trailing defaulted
  `test_shortcuts: tuple[TestShortcut, ...] = ()` to `ProjectConfig` so existing
  positional constructors remain valid.
- Add `InvalidTestShortcutError(ValueError)` as the typed boundary consumed by
  `cli.py` in Task 4.
- Parse `[tool.pyrepo-check.test-shortcuts]` in TOML insertion order and
  normalize list values to immutable tuples without changing token spelling,
  order, or duplicates.

- [ ] **Step 1: Write failing valid/default configuration tests**

Add tests proving an absent shortcut table yields `()` and this configuration:

```toml
[tool.pyrepo-check.test-shortcuts]
unit = ["tests/unit"]
integration = ["-m", "integration"]
cli = ["tests/test_cli.py", "-k", "json", "tests/test_cli.py"]
```

loads exactly as:

```python
(
    TestShortcut("unit", ("tests/unit",)),
    TestShortcut("integration", ("-m", "integration")),
    TestShortcut(
        "cli",
        ("tests/test_cli.py", "-k", "json", "tests/test_cli.py"),
    ),
)
```

Create the referenced files/directories before loading. Assert duplicate target
tokens and definition/token order are preserved. Add valid-name boundary cases
such as `a`, `a0`, `unit-tests`, and `unit_tests`.

Run:

```bash
uv run --frozen python -m pytest tests/test_config.py -q
```

Expected: RED because `TestShortcut` and `ProjectConfig.test_shortcuts` do not
exist.

- [ ] **Step 2: Write failing table/name/value validation tests**

Parameterize exact failures for:

- `test-shortcuts` as a string/list rather than a table;
- names `Unit`, `1unit`, `unit.test`, `unit test`, and a quoted empty key;
- a scalar value, empty list, and a list containing a non-string.

Assert `InvalidTestShortcutError` by type and stable messages:

```text
[tool.pyrepo-check.test-shortcuts] must be a TOML table
Invalid Test Shortcut name 'Unit': must match [a-z][a-z0-9_-]*
Invalid Test Shortcut 'unit': value must be a non-empty list of strings
```

Do not convert malformed non-shortcut settings from their existing `ValueError`
contract.

- [ ] **Step 3: Write failing grammar and containment tests**

Cover all accepted grammar shapes and rejection boundaries:

- target only; `-m` only; `-k` only; both selectors in either order; selectors
  interleaved with one or more targets;
- a file, directory, node ID, `.`, duplicate target, and an internal lexical
  `tests/../tests` path that still resolves under root;
- repeated `-m` or `-k`; missing, empty, whitespace-only, or leading-hyphen
  selector operand;
- unknown option-like tokens including `--maxfail=1`, `-x`, and `-p`;
- empty target, missing target, absolute target, effective `..` escape, and a
  symlink inside the project that resolves outside it;
- a target containing an embedded NUL and a symlink loop, both of which must
  become the typed safe-inspection error rather than escape from `pathlib`.

Assert every invalid case raises `InvalidTestShortcutError` while loading the
entire config, even when that definition is never selected. The symlink case
may skip only when the platform cannot create symlinks.

- [ ] **Step 4: Implement immutable parsing and eager validation**

Add the model and exception after the existing constants:

```python
TEST_SHORTCUT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
TEST_SHORTCUT_SELECTORS = frozenset(("-m", "-k"))


class InvalidTestShortcutError(ValueError):
    """Raised when configured Test Shortcut data violates version 1."""


@dataclass(frozen=True)
class TestShortcut:
    name: str
    pytest_args: tuple[str, ...]
```

Append the defaulted field after `frozen`:

```python
@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    ruff_targets: tuple[str, ...]
    bandit_targets: tuple[str, ...]
    frozen: bool
    test_shortcuts: tuple[TestShortcut, ...] = ()
```

Have `load_project_config` call
`_configured_test_shortcuts(table, root=resolved_root)`. Use this validation
shape; keep accepted raw tokens unchanged:

```python
def _configured_test_shortcuts(
    table: dict[str, Any],
    *,
    root: Path,
) -> tuple[TestShortcut, ...]:
    raw_shortcuts = table.get("test-shortcuts")
    if raw_shortcuts is None:
        return ()
    if not isinstance(raw_shortcuts, dict):
        raise InvalidTestShortcutError(
            "[tool.pyrepo-check.test-shortcuts] must be a TOML table"
        )

    shortcuts: list[TestShortcut] = []
    for name, raw_args in raw_shortcuts.items():
        if TEST_SHORTCUT_NAME_PATTERN.fullmatch(name) is None:
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut name {name!r}: "
                "must match [a-z][a-z0-9_-]*"
            )
        if (
            not isinstance(raw_args, list)
            or not raw_args
            or not all(isinstance(arg, str) for arg in raw_args)
        ):
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: "
                "value must be a non-empty list of strings"
            )
        pytest_args = tuple(raw_args)
        _validate_test_shortcut(name, pytest_args, root=root)
        shortcuts.append(TestShortcut(name=name, pytest_args=pytest_args))
    return tuple(shortcuts)
```

Implement the scanner with an index so selector operands are never
reinterpreted as targets:

```python
def _validate_test_shortcut(
    name: str,
    pytest_args: tuple[str, ...],
    *,
    root: Path,
) -> None:
    seen_selectors: set[str] = set()
    index = 0
    while index < len(pytest_args):
        token = pytest_args[index]
        if token in TEST_SHORTCUT_SELECTORS:
            if token in seen_selectors:
                raise InvalidTestShortcutError(
                    f"Invalid Test Shortcut {name!r}: "
                    f"selector {token} may appear at most once"
                )
            operand_index = index + 1
            if (
                operand_index >= len(pytest_args)
                or not pytest_args[operand_index].strip()
                or pytest_args[operand_index].lstrip().startswith("-")
            ):
                raise InvalidTestShortcutError(
                    f"Invalid Test Shortcut {name!r}: selector {token} "
                    "requires one non-empty expression that does not begin with '-'"
                )
            seen_selectors.add(token)
            index += 2
            continue

        if not token.strip():
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: target must not be empty"
            )
        if token.startswith("-"):
            raise InvalidTestShortcutError(
                f"Invalid Test Shortcut {name!r}: unsupported option token: {token}"
            )
        _validate_test_shortcut_target(name, token, root=root)
        index += 1
```

For targets, split only once at `::`, preserve the original token for the
eventual command, and validate the path portion:

```python
def _validate_test_shortcut_target(name: str, token: str, *, root: Path) -> None:
    path_text = token.split("::", 1)[0]
    path = Path(path_text)
    if not path_text or path.is_absolute():
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target must be project-relative: {token}"
        )

    try:
        resolved_root = root.resolve()
        resolved_target = (resolved_root / path).resolve(strict=False)
    except (ValueError, OSError, RuntimeError) as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target path cannot be inspected safely: {path_text}"
        ) from error
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: target escapes project root: {token}"
        ) from error
    try:
        target_exists = resolved_target.exists()
    except (ValueError, OSError, RuntimeError) as error:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target path cannot be inspected safely: {path_text}"
        ) from error
    if not target_exists:
        raise InvalidTestShortcutError(
            f"Invalid Test Shortcut {name!r}: "
            f"target path does not exist beneath project root: {path_text}"
        )
```

- [ ] **Step 5: Verify the config slice and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_config.py -q
uv run --frozen pyrepo-check annotations ty src/pyrepo_check/config.py tests/test_config.py
git diff --check
```

Expected: config tests, annotation policy, typing, and diff check pass.

Commit:

```bash
git add src/pyrepo_check/config.py tests/test_config.py
git commit -m "feat: validate test shortcut configuration"
```

---

### Task 2: Plan Shortcut Selection, Expansion, and Test Scope

**Files:**

- Modify: `src/pyrepo_check/planning.py`
- Modify: `tests/test_planning.py`

**Interfaces:**

- Add `unknown_test_shortcut` and `invalid_test_shortcut` to
  `PlanningErrorCode`.
- Move the authoritative `PlannedTestScope` alias to `planning.py`.
- Append `test_shortcut: str | None = None` to `RunRequest`.
- Append `test_shortcut`, `pytest_args`, and `planned_test_scope` defaults to
  `RunPlan` so legacy executor/runner construction remains valid.
- Add a `pytest_args` override to `build_checks`; direct-target behavior remains
  its default.

- [ ] **Step 1: Write failing request/plan metadata tests**

Extend `tests/test_planning.py::make_config` with a defaulted immutable shortcut
tuple. Add tests showing:

```python
request = RunRequest(
    root=tmp_path,
    positionals=("pytest",),
    all_selected=False,
    no_frozen=False,
    output_format="json",
    test_shortcut="unit",
)
```

plans:

```python
assert plan.mode == "focused"
assert plan.targets == ()
assert plan.test_shortcut == "unit"
assert plan.pytest_args == ("tests/unit", "-m", "not slow")
assert plan.planned_test_scope == "partial"
assert plan.checks[0].command == (
    "uv", "run", "python", "-m", "pytest",
    "tests/unit", "-m", "not slow",
)
```

Repeat once with `frozen=True` to prove only `--frozen` changes. Assert exact
configured order and duplicates remain in the pytest suffix.

Run:

```bash
uv run --frozen python -m pytest tests/test_planning.py -q
```

Expected: RED because the request/plan fields and shortcut expansion do not
exist.

- [ ] **Step 2: Write the complete CLI-combination planning matrix**

Parameterize these cases at the pure planner boundary:

| Positionals | `--all` | Shortcut | Expected |
| --- | ---: | --- | --- |
| `pytest` | false | known | valid, one focused pytest |
| `pytest pytest` | false | known | valid, one focused pytest |
| `pytest TARGET` | false | any | `invalid_arguments` |
| `pytest ruff` | false | any | `invalid_arguments` |
| no positionals | false | any | `invalid_arguments` |
| target only | false | any | `invalid_arguments` |
| any positionals | true | any | `invalid_arguments` |
| `pytest` | false | unknown | `unknown_test_shortcut` |

For every conflict, assert the exact message and hint from the Resolved Contract
table. Use an unknown name in at least one conflict case to prove combination
validation wins over lookup.

For an unknown name with configured definitions in non-sorted TOML order,
assert:

```python
assert raised.value.code == "unknown_test_shortcut"
assert str(raised.value) == "Unknown Test Shortcut: smoke"
assert raised.value.hint == "Available Test Shortcuts: cli, integration, unit"
```

With no configured shortcuts, assert the hint is exactly
`No Test Shortcuts are configured.`

- [ ] **Step 3: Write scope and backward-compatibility planner tests**

Prove all four states explicitly:

| Selection | `test_shortcut` | `pytest_args` | Scope |
| --- | --- | --- | --- |
| no pytest | null | null | `not_selected` |
| target-free pytest | null | `()` | `complete` |
| direct pytest target | null | exact direct targets | `partial` |
| shortcut pytest | selected name | exact configured tokens | `partial` |

Keep the existing command-contract matrix unchanged. Retain explicit assertions
that direct pytest files/node IDs are forwarded verbatim and that known direct
pytest targets are not subjected to the new shortcut path validator.

- [ ] **Step 4: Implement shortcut resolution before command construction**

Use trailing defaults to preserve existing constructors:

```python
PlannedTestScope = Literal["not_selected", "partial", "complete"]


@dataclass(frozen=True)
class RunRequest:
    root: Path
    positionals: tuple[str, ...]
    all_selected: bool
    no_frozen: bool
    output_format: OutputFormat = "terminal"
    test_shortcut: str | None = None


@dataclass(frozen=True)
class RunPlan:
    mode: RunMode
    targets: tuple[str, ...]
    checks: tuple[PlannedCheck, ...]
    output_format: OutputFormat = "terminal"
    test_shortcut: str | None = None
    pytest_args: tuple[str, ...] | None = None
    planned_test_scope: PlannedTestScope = "not_selected"
```

Resolve conflicts before lookup:

```python
def _resolve_test_shortcut(
    request: RunRequest,
    config: ProjectConfig,
    *,
    requested: tuple[CheckName, ...],
    targets: tuple[str, ...],
) -> tuple[str, ...] | None:
    name = request.test_shortcut
    if name is None:
        return None

    if request.all_selected or targets or set(requested) != {"pytest"}:
        raise PlanningFailure(
            "invalid_arguments",
            "--shortcut requires an explicit pytest-only run "
            "with no direct targets or --all.",
            hint="Use: pyrepo-check pytest --shortcut NAME",
        )

    shortcuts = {shortcut.name: shortcut.pytest_args for shortcut in config.test_shortcuts}
    try:
        return shortcuts[name]
    except KeyError as error:
        available = sorted(shortcuts)
        hint = (
            "Available Test Shortcuts: " + ", ".join(available)
            if available
            else "No Test Shortcuts are configured."
        )
        raise PlanningFailure(
            "unknown_test_shortcut",
            f"Unknown Test Shortcut: {name}",
            hint=hint,
        ) from error
```

Call this immediately after `_split_positionals`. Pass its non-null result as a
pytest-only argument override to `build_checks`:

```python
def build_checks(
    config: ProjectConfig,
    *,
    targets: Sequence[str] = (),
    strict_all: bool = False,
    pytest_args: Sequence[str] | None = None,
) -> dict[str, PlannedCheck]:
    explicit_targets = tuple(targets)
    effective_pytest_args = (
        explicit_targets if pytest_args is None else tuple(pytest_args)
    )
    # Existing Ruff/annotations/ty/Bandit construction stays unchanged.
    # The pytest command ends with *effective_pytest_args.
```

After check selection, compute authoritative plan metadata once:

```python
pytest_selected = any(check.name == "pytest" for check in selected)
if not pytest_selected:
    planned_pytest_args = None
    planned_test_scope: PlannedTestScope = "not_selected"
elif shortcut_args is not None:
    planned_pytest_args = shortcut_args
    planned_test_scope = "partial"
elif targets:
    planned_pytest_args = targets
    planned_test_scope = "partial"
else:
    planned_pytest_args = ()
    planned_test_scope = "complete"
```

Populate `RunPlan.test_shortcut` only when the valid shortcut is selected.
`targets` remains the raw direct-target tuple and is therefore empty for a
shortcut run.

- [ ] **Step 5: Verify planner behavior and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_planning.py tests/test_compatibility.py::test_direct_pytest_node_id_is_forwarded_verbatim -q
uv run --frozen pyrepo-check annotations ty src/pyrepo_check/planning.py tests/test_planning.py
git diff --check
```

Expected: shortcut planning and all existing command matrices pass; direct
pytest compatibility remains unchanged.

Commit:

```bash
git add src/pyrepo_check/planning.py tests/test_planning.py
git commit -m "feat: plan named pytest shortcuts"
```

---

### Task 3: Project Shortcut Metadata into Agent Report Schema v1

**Files:**

- Modify: `src/pyrepo_check/reporting.py`
- Modify: `tests/test_reporting.py`

**Interfaces:**

- Import `PlannedTestScope` from `planning.py` instead of defining reporting
  policy locally.
- Change `Selection.test_shortcut` from `None` to `str | None`.
- Have `build_run_report` copy `test_shortcut`, `pytest_args`, and
  `planned_test_scope` from `RunPlan` exactly.
- Permit the two new planning-error codes in the producer validator.
- Relax only the selection invariants needed for C1. Keep schema version,
  top-level null fields, check/process rules, coverage scope, and key order
  unchanged.

- [ ] **Step 1: Write a failing exact shortcut report test**

Extend the local `run_plan` test helper with safe defaults for new plan fields.
Build a successful pytest observation from a plan carrying:

```python
test_shortcut="unit"
pytest_args=("tests/unit", "-m", "not slow")
planned_test_scope="partial"
```

Assert the exact `Selection`:

```python
Selection(
    checks=("pytest",),
    targets=(),
    test_shortcut="unit",
    pytest_args=("tests/unit", "-m", "not slow"),
    planned_test_scope="partial",
    planned_coverage_scope="not_requested",
)
```

Assert serialized JSON retains the normative member order, schema version `1`,
and explicit top-level `"pytest": null` and `"coverage": null`.

Run:

```bash
uv run --frozen python -m pytest tests/test_reporting.py -q
```

Expected: RED because the current model and validator require
`test_shortcut=null` and derive pytest arguments from direct targets.

- [ ] **Step 2: Write failing schema consistency cases**

Add invalid-producer cases for:

- shortcut name without pytest selected;
- shortcut combined with non-empty direct targets;
- shortcut selection containing a second check;
- shortcut with null or empty `pytest_args`;
- shortcut with a name that does not match `[a-z][a-z0-9_-]*`;
- shortcut `pytest_args` that is not a tuple of strings;
- shortcut with scope other than `partial`;
- non-shortcut pytest whose args no longer equal direct targets.

Separately add positive RED tests showing planning-error reports with
`unknown_test_shortcut` and `invalid_test_shortcut` validate and serialize as
schema v1. Retain the existing truly unknown sentinel-code case as the negative
validator test.

Retain current invalid cases for non-null top-level pytest/coverage,
non-`not_requested` coverage scope, ordinary process roles, and all status/
completeness rules.

- [ ] **Step 3: Implement direct plan projection**

Replace the current target-based inference in `build_run_report` with direct
projection:

```python
selection=Selection(
    checks=tuple(check.name for check in plan.checks),
    targets=plan.targets,
    test_shortcut=plan.test_shortcut,
    pytest_args=plan.pytest_args,
    planned_test_scope=plan.planned_test_scope,
    planned_coverage_scope="not_requested",
),
```

Update the planning-code allow-list with
`unknown_test_shortcut` and `invalid_test_shortcut`. Keep the serializer's
existing `_selection_payload` order unchanged.

Rename the stale guard diagnostics without changing their behavior:

```text
pytest must be null before structured pytest evidence
coverage must be null before coverage execution
planned_coverage_scope must be not_requested before coverage planning
```

- [ ] **Step 4: Implement fail-closed C1 selection validation**

Replace the Milestone-B-only null rule with this branch structure:

```python
shortcut = selection.test_shortcut
if shortcut is not None and (
    not isinstance(shortcut, str)
    or TEST_SHORTCUT_NAME_PATTERN.fullmatch(shortcut) is None
):
    _invalid("test_shortcut must be null or a valid Test Shortcut name")

if selection.pytest_args is not None and (
    not isinstance(selection.pytest_args, tuple)
    or any(not isinstance(arg, str) for arg in selection.pytest_args)
):
    _invalid("pytest_args must be null or a tuple of strings")

pytest_selected = "pytest" in selection.checks
if not pytest_selected:
    if shortcut is not None:
        _invalid("test_shortcut requires pytest selection")
    if selection.pytest_args is not None:
        _invalid("pytest_args must be null when pytest is not selected")
    if selection.planned_test_scope != "not_selected":
        _invalid("planned_test_scope must be not_selected when pytest is not selected")
elif shortcut is not None:
    if selection.checks != ("pytest",):
        _invalid("test_shortcut requires a pytest-only selection")
    if selection.targets:
        _invalid("test_shortcut cannot coexist with direct targets")
    if not selection.pytest_args:
        _invalid("test_shortcut requires non-empty pytest_args")
    if selection.planned_test_scope != "partial":
        _invalid("test_shortcut requires partial planned test scope")
else:
    if selection.pytest_args != selection.targets:
        _invalid("pytest_args must exactly match targets without a Test Shortcut")
    expected_scope: PlannedTestScope = (
        "partial" if selection.targets else "complete"
    )
    if selection.planned_test_scope != expected_scope:
        _invalid("planned_test_scope is inconsistent with pytest selection")
```

Import the shared name pattern from `config.py` rather than duplicating its
regex. Do not revalidate shortcut grammar or filesystem paths in reporting;
those are configuration facts, while this validator checks report consistency.

- [ ] **Step 5: Verify report projection and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_reporting.py -q
uv run --frozen pyrepo-check annotations ty src/pyrepo_check/reporting.py tests/test_reporting.py
git diff --check
```

Expected: exact report, JSON, planning-code, and invalid-producer tests pass.

Commit:

```bash
git add src/pyrepo_check/reporting.py tests/test_reporting.py
git commit -m "feat: report test shortcut selection"
```

---

### Task 4: Expose `--shortcut` Through the CLI and Preserve Compatibility

**Files:**

- Modify: `src/pyrepo_check/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_compatibility.py`

**Interfaces:**

- Add optional `--shortcut NAME` without changing positional `checks` syntax.
- Copy `args.shortcut` into `RunRequest.test_shortcut`; the CLI never expands
  it.
- Catch `InvalidTestShortcutError` before generic `ValueError` and map it to
  `invalid_test_shortcut` with a stable configuration hint.
- Keep argparse-owned missing-option syntax as text; validly parsed semantic
  conflicts use terminal/JSON planning reports and exit `2` without spawning.

- [ ] **Step 1: Write failing parser/request wiring tests**

Update the exact help snapshot to this complete output; the new option changes
both `usage` and wrapped help text:

```text
usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen]
                    [--format {terminal,json}] [--shortcut NAME]
                    [checks ...]

Run Python repository quality checks.

positional arguments:
  checks                Optional check names and target paths. Checks: ruff,
                        annotations, annotations-fix, ty, bandit, pytest.

options:
  -h, --help            show this help message and exit
  --all                 Run all checks.
  --root ROOT           Project root to check. Defaults to the current working
                        directory.
  --no-frozen           Run uv without --frozen even when uv.lock exists.
  --format {terminal,json}
                        Output terminal diagnostics or one JSON document.
  --shortcut NAME       Run a configured Test Shortcut in a pytest-only
                        focused run.
```

Test both supported valid placements:

```python
parse_args(["--shortcut", "unit", "pytest"])
parse_args(["pytest", "--shortcut", "unit"])
```

Assert both produce `checks == ["pytest"]` and `shortcut == "unit"`. Extend the
CLI adapter test to assert the resulting `RunRequest` carries the name. Do not
switch to `parse_intermixed_args`.

Run:

```bash
uv run --frozen python -m pytest tests/test_cli.py tests/test_compatibility.py -q
```

Expected: RED because argparse does not recognize `--shortcut`.

- [ ] **Step 2: Write terminal and JSON no-spawn error tests**

For both output formats, prove exit `2` and `runner.calls == []` for:

- an eagerly invalid shortcut definition, even when the command selects `ty`;
- a NUL-containing target and a symlink loop, both reported as
  `invalid_test_shortcut` in terminal and JSON with zero runner calls;
- a valid definition used with a direct target;
- a valid definition used with `ruff pytest`;
- a valid definition used with `--all` or implicit aggregate selection;
- an unknown name with sorted configured-name hint;
- an unknown name when no shortcuts are configured.

Assert terminal writes message plus hint to stderr and no stdout. Assert JSON
writes exactly one planning-error document plus newline to stdout and no stderr.
For the config failure, assert code `invalid_test_shortcut` and hint exactly:

```text
Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.
```

Add an argparse test proving a missing `--shortcut` operand remains conventional
text syntax failure with `SystemExit(2)`.

- [ ] **Step 3: Write valid end-to-end CLI tests**

Using a temporary `pyproject.toml` and existing target directory, run:

```text
pyrepo-check pytest --shortcut unit
pyrepo-check --format json pytest --shortcut unit
```

With `RecordingRunner`, assert one exact command is attempted. In JSON mode,
assert:

```json
{
  "checks": ["pytest"],
  "targets": [],
  "test_shortcut": "unit",
  "pytest_args": ["tests/unit", "-m", "not slow"],
  "planned_test_scope": "partial",
  "planned_coverage_scope": "not_requested"
}
```

Also retain the existing compatibility assertions for a direct pytest node ID,
target-free `--all`, terminal banners/summaries, output format, exit precedence,
and the legacy runner exception boundary.

- [ ] **Step 4: Implement the thin CLI adapter**

Add the option before the positional argument:

```python
parser.add_argument(
    "--shortcut",
    metavar="NAME",
    help="Run a configured Test Shortcut in a pytest-only focused run.",
)
```

Add `test_shortcut=args.shortcut` to the keyword-built `RunRequest`. Import and
catch the typed config exception first:

```python
except InvalidTestShortcutError as error:
    return _write_planning_error(
        "invalid_test_shortcut",
        str(error),
        hint="Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.",
        output_format=output_format,
    )
except ValueError as error:
    # Existing invalid_project_config mapping remains unchanged.
```

Do not add combination logic, name lookup, or command construction to `cli.py`.

- [ ] **Step 5: Verify CLI integration and commit**

Run:

```bash
uv run --frozen python -m pytest tests/test_cli.py tests/test_compatibility.py -q
uv run --frozen python -m pytest tests/test_config.py tests/test_planning.py tests/test_reporting.py -q
uv run --frozen pyrepo-check annotations ty src/pyrepo_check/cli.py tests/test_cli.py tests/test_compatibility.py
git diff --check
```

Expected: both render modes, every semantic error envelope, help compatibility,
and all C1 unit slices pass.

Commit:

```bash
git add src/pyrepo_check/cli.py tests/test_cli.py tests/test_compatibility.py
git commit -m "feat: expose test shortcuts in cli"
```

---

### Task 5: Document C1 and Run the Milestone Boundary Gate

**Files:**

- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md`
- Verify only: `.agents/skills/pyrepo-check/SKILL.md`
- Verify only: `src/pyrepo_check/execution.py`
- Verify only: `uv.lock`

**Documentation contract:**

- README becomes the public source for shortcut configuration, valid grammar,
  explicit pytest-only usage, conflicts, error behavior, and JSON selection.
- Split the delivery-status description so C1 is implemented while structured
  pytest evidence and coverage remain designed/unimplemented.
- Do not edit either the repository skill or installed skill copies; record
  their synchronization as deferred until after Milestone D.

- [ ] **Step 1: Update README with copy-pasteable C1 usage**

Add these public examples:

```bash
pyrepo-check pytest --shortcut unit
pyrepo-check --format json pytest --shortcut unit
```

Extend the project configuration example:

```toml
[tool.pyrepo-check.test-shortcuts]
unit = ["tests/unit"]
integration = ["-m", "integration"]
cli = ["tests/test_cli.py", "-k", "json"]
```

Explain in plain language:

- a shortcut is a repository-owned safe name for a repeatable pytest subset;
- it is valid only with explicit pytest and cannot combine with `--all`, another
  check, or direct targets;
- definitions accept existing project-relative test paths/node IDs plus at most
  one `-m` and one `-k` pair;
- definitions are validated eagerly, so an invalid configured shortcut blocks
  execution even when not selected;
- direct paths/node IDs remain supported and are preferable for one-off tests;
- JSON reports `test_shortcut`, exact `pytest_args`, and partial planned scope,
  while top-level pytest/coverage evidence remains null in C1.

- [ ] **Step 2: Synchronize delivery status without claiming later slices**

Replace the single Milestone-C status row with explicit sub-slice evidence:

| Milestone | State | Evidence |
| --- | --- | --- |
| C1 — Test Shortcuts | Implemented and verified | Eager config/grammar/path validation, planner expansion/conflict tests, schema-v1 selection, CLI terminal/JSON tests, and strict gate. |
| C2 — structured pytest evidence | Designed; not implemented | No pytest preflight/plugin or structured result exists. |
| C3 — coverage execution and guidance | Designed; not implemented | No coverage preflight/execution/result exists. |

Leave Milestone D as designed/not implemented. Do not change the normative C2,
C3, or D contract to make C1 appear more complete than it is.

Also update the staged pytest-result seam to say **Milestone B and C1** leave
top-level `pytest` null and **C2 onward** requires a selected pytest result.
Likewise describe C1 coverage as null/not-requested until C3. This keeps the
normative prose consistent with the C1 schema validator instead of leaving
stale “Milestone B only” wording.

- [ ] **Step 3: Run focused regression and static gates**

Run:

```bash
uv run --frozen python -m pytest tests/test_config.py tests/test_planning.py tests/test_reporting.py tests/test_cli.py tests/test_compatibility.py -q
uv run --frozen python -m pytest tests/test_execution.py tests/test_runner.py -q
uv run --frozen pyrepo-check annotations ty
git diff --check
```

Expected: all C1, execution, legacy-runner, annotation, and type checks pass.

- [ ] **Step 4: Prove deferred boundaries stayed untouched**

Inspect:

```bash
git diff --name-only a288256 --
git diff --exit-code a288256 -- src/pyrepo_check/execution.py uv.lock .agents/skills/pyrepo-check/SKILL.md
git status --short
```

Expected: the first command lists only C1 source/tests/README/spec/plan files;
the second command is silent and exits `0`; status contains only the expected
tracked C1 edits and no untracked implementation/artifact paths. Together they
must show no pytest evidence plugin, coverage implementation, dependency,
lockfile, or skill change.

- [ ] **Step 5: Commit C1 documentation**

```bash
git add README.md docs/superpowers/specs/2026-08-24-agent-guidance-reporting-design.md
git commit -m "docs: document test shortcut workflow"
```

- [ ] **Step 6: Run the strict C1 milestone gate from the committed tree**

Run:

```bash
uv run --frozen pyrepo-check --all
git diff --check
git status --short --branch
```

Expected: Ruff, annotations, ty, Bandit, and the complete pytest suite pass.
The feature branch is clean after the documentation commit, and the main
checkout's unrelated `LICENSE` remains untouched.

## C1 Completion Evidence

Before requesting merge/push approval, report:

- the feature branch/worktree and commit list;
- exact focused commands and their results;
- the final strict-gate result and test count;
- one terminal and one JSON shortcut example;
- proof that direct pytest node IDs still work;
- proof that semantic errors execute zero processes;
- the unchanged `execution.py`, `uv.lock`, and skill paths;
- explicit deferral of C2, C3, D, and skill synchronization.
