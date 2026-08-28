# pyrepo-check

`pyrepo-check` is an install-once controller for Python repository quality gates.
Python 3.13.15 or newer is required for the controller, while every selected Check
that can execute runs in the target repository's own locked uv environment and under
one repository-selected CPython.

## Install the controller

Install the CLI globally once. It does not need to be a dependency of every project
that it checks.

```bash
uv tool install --python 3.13.15 \
  git+https://github.com/arduinitavares/pyrepo-check.git
```

For development from a clone:

```bash
uv tool install --python 3.13.15 --editable /path/to/pyrepo-check
```

The global Python belongs only to the **Tool Environment** that imports and runs the
controller. It does not replace the target project's Python or tool dependencies.

## Repository requirements

The target must be a uv project with `pyproject.toml` and a present, current
`uv.lock`. uv's default dependency selection must provide compatible,
repository-owned dependencies required by the selected Checks. Coverage is also
required when explicitly requested with `--coverage`, or when valid native Coverage
configuration auto-enables it for a target-free strict aggregate:

| Check | Locked distribution | Supported version |
| --- | --- | --- |
| Ruff and annotations | `ruff` | `>=0.15,<1` |
| Type checking | `ty` | `>=0.0.35,<0.1` |
| Security | `bandit` | `>=1.9,<2` |
| Tests | `pytest` | `>=8,<9` |
| Requested/configured coverage | `coverage` | `>=7.15,<8` |

pyrepo-check does not inject, install, or upgrade these dependencies to make a run
green. Fix a missing, incompatible, shadowed, or unusable dependency in the target
repository's configuration and lock, with the user's authority, then rerun.

The repository's ignored, untracked `.venv` may be created or synchronized from the
current lock. Ordinary controller, preparation, and Check command construction does
not request changes to `pyproject.toml`, `uv.lock`, or tracked source.
Repository-controlled build backends, tests, and plugins may still write.
Before/after evidence detects tracked or protected-file changes and reports
`repository_state_changed`. This is not a sandbox: it does not prevent or roll back
writes. Inspect changes and restore them
only with user authority. A tracked, unignored, or symlinked `.venv` is rejected.

## Usage

Run from the target repository root or pass `--root <path>`.

```bash
# Repository-native default Python and locked default dependencies
pyrepo-check --all

# One explicit Repository Python for one CI job or run
pyrepo-check --python 3.12 --all

# Focused typing under that same Repository Environment
pyrepo-check --python 3.12 annotations ty src/

# Complete Environment Evidence for agents
pyrepo-check --python 3.12 --format json --all
```

Omitting `--python` delegates selection to normal uv repository rules, including
`.python-version`. The accepted Repository Python selectors are CPython 3.10, 3.11,
3.12, and 3.13, optionally with an exact patch release. One invocation proves one
Repository Python; a CI matrix invokes pyrepo-check separately for each version.

No arguments behaves like a target-free `--all`. That strict aggregate runs Ruff,
annotation reporting, Ty, Bandit, and pytest in fixed order. It continues independent
checks after a check-local failure so later evidence is not hidden.

Focused examples:

```bash
pyrepo-check ruff src/package/
pyrepo-check annotations ty src/package/
pyrepo-check bandit src/package/
pyrepo-check pytest tests/test_file.py
pyrepo-check pytest tests/test_file.py::test_name
pyrepo-check pytest -- -k pattern
pyrepo-check pytest --shortcut unit
pyrepo-check --format json ty
```

A target-only command runs the file-oriented checks: Ruff, annotations, Ty, and
Bandit. `annotations` explicitly enforces Ruff's `ANN` rules; `ty` checks type
correctness. Use both for typing work. `annotations-fix` mutates source and must be
explicitly authorized and selected alone. Its mutation allowance covers only tracked
regular-file content under the exact requested targets:

```bash
pyrepo-check annotations-fix src/package/
pyrepo-check annotations ty src/package/
```

`--no-frozen` remains recognized only to return the typed planning error
`unsafe_unlocked_execution`. Update `uv.lock` explicitly outside pyrepo-check, with
user authority, then rerun without the flag.

Run `pyrepo-check --help` for the syntax supported by the installed CLI version.

## Which Python means what?

- **Tool Python** runs the globally installed pyrepo-check controller and must be
  Python 3.13.15 or newer.
- **Repository Python** is the exact CPython selected by uv for this run. Every
  executable check runs in that Repository Environment.
- **Analysis Python** is the language target interpreted by Ruff or Ty from the
  repository's own configuration. It may differ from Repository Python because
  Ruff and Ty perform static analysis rather than execute the checked code.

pyrepo-check does not pass a controller-derived target version to Ruff or Ty and
does not rewrite their configuration.

## Terminal and agent reports

Terminal output is the human projection. It streams native diagnostics, shows the
Tool-to-Repository Python relationship, and finishes with a compact summary.

`--format json` emits exactly one schema version 2 document plus a trailing newline.
Native stdout and stderr are captured inside process evidence. Agents should inspect:

- `tool_environment` for the controller version and Tool Python;
- `repository_environment` for uv, exact Repository Python, lock proof, mutation
  protection, dependency states, preparation processes, and environment errors;
- every check's `execution_environment`, `analysis_python_authority`,
  `start_evidence`, processes, status, and typed error; and
- nested `pytest` and `coverage` evidence when selected.

A check-local dependency error does not suppress independent checks. Read every
check result, not only `overall_status` or the first error.

The complete field order, types, nullability, enums, invariants, error codes, and
examples are in
[`docs/reference/agent-report-schema-v2.md`](docs/reference/agent-report-schema-v2.md).

Public exit codes are stable:

- `0`: complete pass;
- `1`: complete findings or threshold failure; and
- `2`: planning, environment, dependency, execution, or evidence error.

Child exit codes remain available inside process evidence but do not replace this
public exit contract.

## Pytest and coverage

Structured pytest evidence reports collection, counts, slow tests, skips, XFAIL,
XPASS, scope, completeness, and typed errors. Direct targets, shortcuts, selectors,
deselection, collection reduction, and incomplete sessions can make test scope
partial.

Coverage uses the repository's native Coverage.py configuration:

```toml
[tool.coverage.run]
branch = true
source = ["src"]
parallel = false

[tool.coverage.report]
fail_under = 90
```

```bash
# Focused, partial coverage guidance
pyrepo-check pytest --coverage tests/test_file.py

# Strict aggregate; valid native configuration auto-enables Coverage
pyrepo-check --all

# Full machine-readable evidence, including exact gaps
pyrepo-check --format json --all
```

Only a complete, target-free strict aggregate can apply the configured threshold.
`scope="partial"`, `status="guidance"`, and `gate_eligible=false` is useful measured
evidence, but it is not proof that the repository-wide threshold passed.

If Coverage is missing or invalid, pytest still runs when pytest itself is usable;
the Coverage result and overall run remain errors. Pytest runs once under Coverage
instrumentation rather than being rerun for reporting.

## Project configuration

Focused target defaults and Test Shortcuts are repository-owned:

```toml
[tool.pyrepo-check]
ruff_targets = ["src/package", "tests"]
bandit_targets = ["src/package"]

[tool.pyrepo-check.test-shortcuts]
unit = ["tests/unit"]
integration = ["-m", "integration"]
cli = ["tests/test_cli.py", "-k", "json"]
```

Configured targets affect focused commands, not the target-free strict gate. A Test
Shortcut is valid only for a pytest-only focused run and cannot be combined with
`--all`, another check, or direct targets.

Configured and direct targets must name existing project-contained relative paths;
absolute, option-like, missing, `..`, NUL-containing, and symlink-escaping targets are
rejected before execution. Pytest node selectors preserve their suffix after validating
the filesystem path before the first `::`.

## Agent Skill

The repository Skill is
[`.agents/skills/pyrepo-check/SKILL.md`](.agents/skills/pyrepo-check/SKILL.md).
It teaches focused editing checks, repository-environment remediation, schema-v2
interpretation, and the required final `--all` gate. Personal Codex or Antigravity
copies are not updated automatically; deployment is a separate, hash-verified action.

## Developing pyrepo-check

From this repository:

```bash
uv run --frozen python -m pytest -q
uv run --frozen python -m ruff check .
uv run --frozen python -m ty check
uv run --frozen pyrepo-check --all
uv run --frozen pyrepo-check --format json --all
```

The current strict gate measures `src/pyrepo_check` with line and branch coverage,
`parallel = false`, `fail_under = 86.01`, and `precision = 2`. See the implemented
design for the fresh final verification evidence.
