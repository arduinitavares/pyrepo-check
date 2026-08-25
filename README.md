# pyrepo-check

`pyrepo-check` is a reusable Python quality-gate wrapper. Install it once as an
editable global tool, then run it from any Python project root.

## Install

Python 3.13.15 or newer is required.

```bash
uv tool install --editable /Users/aaat/projects/pyrepo-check
```

## Agent Skill

The repository includes a focused usage skill at
[`.agents/skills/pyrepo-check/SKILL.md`](.agents/skills/pyrepo-check/SKILL.md).
Codex discovers it automatically while working inside this checkout.

To make the skill available while agents work in other repositories, run the
following from this repository root:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/.agents/skills/pyrepo-check" \
  "$HOME/.agents/skills/pyrepo-check"
```

The skill teaches agents which focused checks to choose, how to run one pytest
test, and when the strict `--all` gate is still required. The CLI `--help`
output remains the source of truth for supported commands. Repository and
installed Agent Skill synchronization is the next separate post-D action and
remains unchanged in this milestone.

## Usage

```bash
pyrepo-check --all
pyrepo-check ruff
pyrepo-check annotations
pyrepo-check annotations-fix
pyrepo-check ty
pyrepo-check bandit
pyrepo-check pytest
pyrepo-check ruff annotations ty bandit pytest
pyrepo-check api.py
pyrepo-check annotations api.py
pyrepo-check annotations-fix api.py
pyrepo-check ruff api.py
pyrepo-check ruff annotations ty bandit api.py
pyrepo-check --all api.py
pyrepo-check --format json ty
pyrepo-check --format json --all
pyrepo-check pytest --shortcut unit
pyrepo-check --format json pytest --shortcut unit
```

No arguments behaves the same as `--all`.

`--all` is the strict repository gate. Without explicit target paths, it runs
Ruff, annotation reporting, and Bandit against the repository root (`.`), even
when a project configures narrower focused-check targets.

Any positional argument that is not a check name is treated as a target path.
When only target paths are provided, the file-oriented checks run against those
targets: `ruff`, `annotations`, `ty`, and `bandit`. Use `--all <target>` to
include `pytest`.

When multiple checks are selected, `pyrepo-check` runs every selected check and
returns a non-zero exit code if any check fails. This keeps focused runs like
`pyrepo-check --all api.py` from hiding later Ruff, annotation, ty, Bandit, or
pytest diagnostics behind the first failing tool.

## Agent report output

Terminal output is the default. It streams each tool's native diagnostics as
the checks run, then adds one deterministic post-run summary. These commands
cover the public pytest and coverage forms:

```bash
pyrepo-check ty
# An explicitly selected, target-free pytest coverage run is focused guidance.
pyrepo-check pytest --coverage

# A direct target remains a focused, partial coverage run.
pyrepo-check pytest --coverage tests/test_example.py::test_name

# A repository-owned shortcut is also a focused coverage run.
pyrepo-check pytest --shortcut unit --coverage

# With native Coverage.py configuration, this is the target-free strict aggregate.
pyrepo-check --coverage

# In that configured repository, the normal target-free aggregate auto-enables coverage.
pyrepo-check --all

# A target-bearing --all request is focused coverage guidance, not a threshold gate.
pyrepo-check --all --coverage tests/test_example.py

# JSON uses the same report and selection rules.
pyrepo-check --format json pytest --coverage tests/test_example.py::test_positive

# The agent-readable form of this repository's normal strict self-check.
pyrepo-check --format json --all
```

Terminal coverage uses a compact Coverage.py-style table: at most the three
highest-gap files plus one `TOTAL` row. The summary states whether the run is
focused or strict, whether coverage is partial or complete, and whether the
configured minimum was applied. Raw missing line numbers and branch arcs stay
out of terminal output; use `--format json` when an agent needs every exact
gap. Files with no gaps do not occupy a focus row.

Use `--format json` when an agent needs a machine-readable result. Its stdout
is exactly one versioned JSON document followed by a newline; native tool
stdout and stderr are captured inside that document. With pytest selected, the
report has trusted pytest evidence and, when coverage is configured and
requested, a non-null coverage result. The complete, schema-valid `coverage`
member below was copied from an actual partial direct-target report; its integer
counts and gaps are not illustrative placeholders.

```bash
pyrepo-check --format json pytest --coverage tests/test_example.py::test_positive
```

```json
{
  "status": "guidance",
  "scope": "partial",
  "evidence_complete": true,
  "coverage_version": "7.15.4",
  "gate_eligible": false,
  "threshold": {
    "configured": true,
    "value": 100,
    "evaluated": false,
    "passed": null,
    "skipped_reason": "partial_run"
  },
  "totals": {
    "statements": {
      "covered": 3,
      "missing": 1
    },
    "branches": {
      "covered": 1,
      "missing": 1
    }
  },
  "files": [
    {
      "path": "src/example.py",
      "statements": {
        "covered": 3,
        "missing": 1,
        "missing_lines": [4]
      },
      "branches": {
        "covered": 1,
        "missing": 1,
        "missing_arcs": [[2, 4]]
      }
    }
  ],
  "error": null
}
```

`scope_reasons` explains partial evidence (for example, a planned selector,
deselection, collection reduction, or an incomplete session). `counts` covers
passed, failed, errors, skipped, xfailed, and xpassed outcomes;
`special_outcomes` lists skipped/XFAIL/XPASS nodes; and `slowest` contains up
to ten nodes in deterministic order. `pytest` is `null` only when pytest was
not selected. `coverage` is `null` only when its planned scope is
`not_requested` or `unavailable`. A focused run without `--coverage` is
`not_requested`. An implicit target-free aggregate without native Coverage.py
configuration is `unavailable` and emits the typed `coverage_not_configured`
advisory. With valid native configuration, `pyrepo-check --all` auto-enables
coverage and may enforce its threshold. An explicit `--coverage` request without
that configuration instead fails planning with
`coverage_configuration_required`. Files are ranked by `missing statements +
missing branches`; there is no separate `missing_opportunities` JSON field.

Coverage guidance uses a project's native Coverage.py configuration, for
example:

```toml
[tool.coverage.run]
branch = true
source = ["src"]
parallel = false

[tool.coverage.report]
fail_under = 90
```

Focused, target-bearing, failed-test, and incomplete pytest runs neutralize
`fail_under`: they retain valid coverage as guidance but do not evaluate the
native threshold. Only an eligible target-free strict aggregate evaluates that
threshold. When eligible Coverage JSON exits `2` with valid evidence, the
report records a threshold failure rather than a coverage-artifact error, and
the public exit is `2` under the normal first-positive-exit rule.

Pytest runs once under Coverage instrumentation; it is not rerun for the
coverage report. Pyrepo-check-owned coverage, plugin, and report artifacts are
run-owned and outside the consumer root, so a completed run leaves consumer
bytes and its worktree unchanged.

## Repository coverage baseline

This repository declares `coverage[toml]>=7.15,<8` in its development group;
the frozen lock resolves Coverage.py `7.15.4`. Its native configuration measures
`src/pyrepo_check` with `branch = true` and `parallel = false`.

For this repository, `pyrepo-check --all` is the normal strict self-check, and
`pyrepo-check --format json --all` is the agent-readable form. The verified
target-free strict run passed all 1,314 tests and measured 13 files: 4,031
covered and 484 missing statements, plus 1,522 covered and 372 missing
branches. The combined fresh baseline is 5,553 / 6,409 =
86.64378218130754%.

`[tool.coverage.report]` sets `fail_under = 86.01` with `precision = 2`. This
floor is below the fresh baseline and rejects totals that round to 86.00%. The
compact terminal table identifies the highest-gap files; schema-v1 JSON retains
every exact missing line and arc.

The CLI keeps the first positive tool exit code in planned execution order.
Checks continue after ordinary failures. If execution has only spawn or
signal errors, the CLI returns `2`.

Captured process streams retain only their final 65,536 raw bytes. Production
capture drains stdout and stderr concurrently with bounded tail buffers;
reader-construction, reader-start, drain, and wait failures make a best-effort
terminate/wait/kill/wait attempt for the direct child within one shared cleanup
deadline, then continue later checks through the existing `spawn_failed`
result. Reader threads are daemons. A started reader remains the sole closer of
its pipe; if it is still blocked after the deadline, cleanup returns promptly
without claiming that inherited descendant handles were reaped or closed.
Injected test runners are outside that production memory guarantee. Structured
pytest artifacts and writer markers also have fixed read, nesting, and
directory-inventory limits. Their descriptor-safe reads are nonblocking, so a
FIFO or other non-regular path fails closed instead of waiting for a writer.

Structured pytest evidence requires descriptor-safe no-follow file opening and
bounded descriptor-relative recursive removal. Cleanup validates the complete
tree before deletion, then atomically moves each manifest-matched leaf into a
fresh owner-only sibling quarantine. The moved device, inode, and exact type
must still match before cleanup unlinks it from the quarantine descriptor.
Symlinks, FIFOs, sockets, and devices are never opened or followed. Each pass
accepts at most 4,096 entries and depth 64; the complete cleanup has a
five-second monotonic deadline. A single kernel call cannot be interrupted by
that deadline. Supported platforms must provide descriptor-relative `mkdir`
and `rename` and must prove that an opened directory was unlinked after
`rmdir`: Linux uses zero link count, while Darwin uses a missing absolute
`F_GETPATH` target. Unsupported or unproven platforms fail closed before pytest
starts; pyrepo-check does not fall back to path-based cleanup.

Plugin and writer preparation is bound to a securely opened run-directory
descriptor. The recorded parent and run identities are reverified before and
after preparation, before preflight, after preflight, and after primary pytest
before artifact snapshot. A failed gate preserves real process observations
without trusting a same-path replacement.

Cleanup failure keeps an already-captured pytest snapshot but makes the check
an incomplete `cleanup_failed` error. The diagnostic reports retained run and
quarantine paths separately, and only while descriptor-relative and lexical
observations still name their recorded identities. These are best-effort
current observations, not permanent location guarantees. A validation failure
leaves the run tree untouched; a race or I/O failure during deletion can leave
the verified run root partially emptied and can retain quarantined content for
manual inspection. Portable Python cannot unlink by inode. Final unlink assumes
exclusive host control of the fresh private quarantine between its identity
check and unlink; same-UID discovery or mutation cannot be eliminated with the
standard library.

## Project Configuration

Each project can optionally configure focused-check paths in its
`pyproject.toml`:

```toml
[tool.pyrepo-check]
ruff_targets = ["src/cartola", "src/tests", "scripts"]
bandit_targets = ["src/cartola"]

[tool.pyrepo-check.test-shortcuts]
unit = ["tests/unit"]
integration = ["-m", "integration"]
cli = ["tests/test_cli.py", "-k", "json"]
```

When a project has `uv.lock`, `pyrepo-check` runs checks through
`uv run --frozen python -m ...`. Without `uv.lock`, it runs through
`uv run python -m ...`.

Configured targets apply to focused commands like `pyrepo-check ruff` and
`pyrepo-check annotations`. They do not narrow the no-argument or `--all` gate.

### Test Shortcuts

A Test Shortcut is a repository-owned safe name for a repeatable pytest subset.
Its name must match `[a-z][a-z0-9_-]*`, and its value must be a non-empty list
of strings. Run one only with explicit pytest:

```bash
pyrepo-check pytest --shortcut unit
pyrepo-check --format json pytest --shortcut unit
```

A shortcut cannot be combined with `--all`, another check, or direct pytest
targets. Definition tokens may appear in any order. They may be existing
project-relative test paths or node IDs, plus at most one `-m VALUE` pair and
one `-k VALUE` pair. Selector values must be non-empty and cannot begin with
`-`; no other option tokens are allowed. Definitions are validated eagerly, so
an invalid configured shortcut blocks execution even when it is not selected.
Invalid or unknown shortcut requests are planning errors: they run zero
processes and exit with code `2`.

Direct test paths and node IDs remain supported and are preferable for one-off
tests:

```bash
pyrepo-check pytest tests/test_cli.py::test_invalid_shortcut_config_renders_typed_planning_error_without_spawning
```

Repository and installed Agent Skills remain unchanged in this milestone. Their
synchronization is the next separate post-D action; do not copy this C3 guidance
into either skill location here.

## Type Annotation Enforcement

`pyrepo-check` owns the strict annotation workflow explicitly. It does not rely
on each project remembering direct Ruff commands or enabling `ANN` in its normal
Ruff configuration.

Use `annotations` for the focused report:

```bash
pyrepo-check annotations
pyrepo-check annotations api.py
```

Use `annotations-fix` for Ruff's mechanical annotation fixer:

```bash
pyrepo-check annotations-fix
pyrepo-check annotations-fix api.py
```

`annotations-fix` mutates files, so it is never included in `--all`.

The full strict workflow is:

```bash
pyrepo-check --all
pyrepo-check annotations
pyrepo-check annotations-fix
pyrepo-check annotations
pyrepo-check --all
```

`--all` runs `ruff`, `annotations`, `ty`, `bandit`, then `pytest`. Ruff,
annotations, and Bandit use `.` for the aggregate gate, so top-level Python
files are included. That means the full gate explicitly proves annotation policy
even if normal Ruff configuration also enables `ANN`.

## Checks

| Check | Command Shape |
| --- | --- |
| `ruff` | `uv run python -m ruff check <ruff_targets>` |
| `annotations` | `uv run python -m ruff check <ruff_targets> --select ANN --output-format concise` |
| `annotations-fix` | `uv run python -m ruff check <ruff_targets> --select ANN --fix --unsafe-fixes` |
| `ty` | `uv run python -m ty check` |
| `bandit` | `uv run python -m bandit -c pyproject.toml -r <bandit_targets>` |
| `pytest` | `uv run python -m pytest` |

For `pyrepo-check --all` and no-argument `pyrepo-check`, the aggregate command
shapes are stricter:

| Check | Aggregate Command Shape |
| --- | --- |
| `ruff` | `uv run python -m ruff check .` |
| `annotations` | `uv run python -m ruff check . --select ANN --output-format concise` |
| `ty` | `uv run python -m ty check` |
| `bandit` | `uv run python -m bandit -c pyproject.toml -r .` |
| `pytest` | `uv run python -m pytest` |

When target paths are passed on the command line, those targets override the
configured defaults:

| Example | Command Shape |
| --- | --- |
| `pyrepo-check ruff api.py` | `uv run python -m ruff check api.py` |
| `pyrepo-check annotations api.py` | `uv run python -m ruff check api.py --select ANN --output-format concise` |
| `pyrepo-check annotations-fix api.py` | `uv run python -m ruff check api.py --select ANN --fix --unsafe-fixes` |
| `pyrepo-check ty api.py` | `uv run python -m ty check api.py` |
| `pyrepo-check bandit api.py` | `uv run python -m bandit -c pyproject.toml api.py` |
| `pyrepo-check pytest tests/test_cli.py` | `uv run python -m pytest tests/test_cli.py` |

## Maintenance Workflow

Edit the tool in one place:

```bash
cd /Users/aaat/projects/pyrepo-check
```

Run its own tests:

```bash
uv run pytest -q
uv run ruff check .
```

Use the latest local source from any project:

```bash
cd /path/to/a/python/project
pyrepo-check --all
```

Because the tool is installed with `uv tool install --editable`, source changes
inside `/Users/aaat/projects/pyrepo-check` are picked up without reinstalling.

## GitHub Workflow

Keep this folder as the source of truth, then push it to GitHub:

```bash
cd /Users/aaat/projects/pyrepo-check
git remote add origin git@github.com:<you>/pyrepo-check.git
git push -u origin main
```

On another machine, clone it and install the editable tool from the local clone:

```bash
git clone git@github.com:<you>/pyrepo-check.git /Users/aaat/projects/pyrepo-check
uv tool install --editable /Users/aaat/projects/pyrepo-check
```
