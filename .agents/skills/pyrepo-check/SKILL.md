---
name: pyrepo-check
description: Use when validating Python repository changes with pyrepo-check, especially for focused typing, annotation, lint, security, pytest, coverage, or final quality-gate work.
---

# Using pyrepo-check

## Core contract

Use focused checks while editing and target-free `pyrepo-check --all` before
completion. `pyrepo-check --help` is the installed-version syntax authority.

The globally installed Python 3.13.15+ CLI is only the **Tool Environment**
controller. The target must be a uv project with a present, current `uv.lock`.
Every selected Check that can execute runs in one uv-managed **Repository
Environment** under one Repository Python; pyrepo-check is not injected there.

## Workflow

1. Inspect `git status --short`, `pyproject.toml`, `uv.lock`, and
   `pyrepo-check --help`.
2. Run the smallest focused check for the edit.
3. Fix code or repository configuration without weakening policy.
4. Inspect `pyrepo-check --format json --all`, then run `pyrepo-check --all` before
   claiming completion.

## Commands

| Intent | Command |
| --- | --- |
| Strict repository-native gate | `pyrepo-check --all` |
| One CI-selected Repository Python | `pyrepo-check --python 3.12 --all` |
| Focused typing | `pyrepo-check --python 3.12 annotations ty src/` |
| Strict schema-v2 evidence | `pyrepo-check --python 3.12 --format json --all` |
| One test | `pyrepo-check pytest tests/test_file.py::test_name` |

Omit `--python` for uv's repository-native selection. Use it only when the user or
CI chooses one Python for that run. A matrix requires separate invocations.

Run `annotations` and `ty` together for typing work. `annotations-fix` mutates files:
select it alone, use it only with source-change authority, inspect the diff, then rerun
both checks. Use only existing project-relative targets; pytest node selectors remain
valid after their filesystem prefix passes the same containment check.

## Repository ownership and remediation

uv's default selection must contain compatible repository-owned dependencies needed
by the selected Checks. Coverage is also required when requested with `--coverage` or
auto-enabled by valid native configuration for a target-free strict aggregate. Fix
dependency errors in repository configuration/lock only with user authority. Never
inject packages merely to pass. Independent checks still run.

uv may synchronize a safe ignored, untracked `.venv` from the current lock. Ordinary
controller/preparation command construction does not request `pyproject.toml`,
`uv.lock`, or tracked-source changes, but repository-controlled build backends, tests,
or plugins may write. Before/after evidence detects and reports
`repository_state_changed`; it does not prevent or roll back writes. Inspect and
restore only with user authority.
`--no-frozen` is recognized only to return `unsafe_unlocked_execution`; update the
lock explicitly with user authority, then rerun without it.

Ruff and Ty retain repository-configured **Analysis Python** semantics, which may
differ from Repository Python. Do not rewrite their target from controller evidence.

## Read schema version 2

For JSON, inspect:

- `tool_environment` for controller version and Tool Python;
- `repository_environment` for exact Repository Python, current lock, mutation
  protection, dependencies, processes, and environment error;
- every check's `execution_environment`, `analysis_python_authority`,
  `start_evidence`, processes, status, and error; and
- nested `pytest` and `coverage` evidence.

Do not stop at `overall_status`: a local error can coexist with useful later evidence.
Coverage with `scope="partial"`, `status="guidance"`, and `gate_eligible=false` is
useful guidance, not a repository-wide threshold gate.

Inside pyrepo-check source, use `docs/reference/agent-report-schema-v2.md` for every
field, enum, invariant, and example.
