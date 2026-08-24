# pyrepo-check

`pyrepo-check` is a reusable Python quality-gate wrapper. Install it once as an
editable global tool, then run it from any Python project root.

## Install

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
output remains the source of truth for supported commands.

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
the checks run, then adds one deterministic post-run summary. Focused and
strict selection commands are unchanged:

```bash
pyrepo-check ty
pyrepo-check pytest tests/test_cli.py::test_name
pyrepo-check --format json ty
pyrepo-check --format json --all
```

Use `--format json` when an agent needs a machine-readable result. Its stdout
is exactly one versioned JSON document followed by a newline; native tool
stdout and stderr are captured inside that document. In schema version 1, the
top-level `pytest` and `coverage` sections are explicitly `null`. The selected
pytest process is still listed in `selection.checks` and `checks`.

The CLI keeps the first positive tool exit code in planned execution order.
Checks continue after ordinary failures. If execution has only spawn or
signal errors, the CLI returns `2`.

## Project Configuration

Each project can optionally configure focused-check paths in its
`pyproject.toml`:

```toml
[tool.pyrepo-check]
ruff_targets = ["src/cartola", "src/tests", "scripts"]
bandit_targets = ["src/cartola"]
```

When a project has `uv.lock`, `pyrepo-check` runs checks through
`uv run --frozen python -m ...`. Without `uv.lock`, it runs through
`uv run python -m ...`.

Configured targets apply to focused commands like `pyrepo-check ruff` and
`pyrepo-check annotations`. They do not narrow the no-argument or `--all` gate.

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
