# pyrepo-check

`pyrepo-check` is a reusable Python quality-gate wrapper. Install it once as an
editable global tool, then run it from any Python project root.

## Install

```bash
uv tool install --editable /Users/aaat/projects/pyrepo-check
```

## Usage

```bash
pyrepo-check --all
pyrepo-check ruff
pyrepo-check ty
pyrepo-check bandit
pyrepo-check pytest
pyrepo-check ruff pytest
```

No arguments behaves the same as `--all`.

## Project Configuration

Each project can optionally configure paths in its `pyproject.toml`:

```toml
[tool.pyrepo-check]
ruff_targets = ["src/cartola", "src/tests", "scripts"]
bandit_targets = ["src/cartola"]
```

When a project has `uv.lock`, `pyrepo-check` runs checks through
`uv run --frozen python -m ...`. Without `uv.lock`, it runs through
`uv run python -m ...`.

## Checks

| Check | Command Shape |
| --- | --- |
| `ruff` | `uv run python -m ruff check <ruff_targets>` |
| `ty` | `uv run python -m ty check` |
| `bandit` | `uv run python -m bandit -c pyproject.toml -r <bandit_targets>` |
| `pytest` | `uv run python -m pytest` |

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
