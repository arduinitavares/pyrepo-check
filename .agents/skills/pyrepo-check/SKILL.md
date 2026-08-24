---
name: pyrepo-check
description: Use when validating Python repository changes with pyrepo-check, especially for focused typing, annotation, lint, security, pytest, or final quality-gate work.
---

# Using pyrepo-check

## Overview

Use `pyrepo-check` for fast, focused feedback while editing and the strict
repository gate before completion. The installed CLI is the command source of
truth; confirm its current interface with `pyrepo-check --help`.

## Workflow

1. Run from the target repository root, or pass `--root <path>`.
2. During editing, choose the smallest checks that cover the changed behavior.
3. Run `pyrepo-check --all` before claiming the repository is fully verified.
4. Report the exact commands, failures, and any gate that remains unrun.

If `pyrepo-check` is unavailable, report that fact. Do not install or upgrade it
unless the current task authorizes environment changes.

## Quick Reference

| Change or intent | Command |
| --- | --- |
| Type signatures or annotations | `pyrepo-check annotations ty <target>` |
| Lint | `pyrepo-check ruff <target>` |
| Security-sensitive Python | `pyrepo-check bandit <target>` |
| One test file | `pyrepo-check pytest tests/test_file.py` |
| One exact test | `pyrepo-check pytest tests/test_file.py::test_name` |
| Tests matching a name | `pyrepo-check pytest -- -k pattern` |
| Strict final validation | `pyrepo-check --all` |
| Machine-readable focused report | `pyrepo-check --format json ty` |
| Machine-readable strict report | `pyrepo-check --format json --all` |

`annotations` enforces annotation policy; `ty` checks type correctness. Run
both for typing-related edits. A target-only command such as
`pyrepo-check src/module.py` runs the file-oriented checks but does not run
pytest.

## Agent report output

Terminal is the default: native tool diagnostics stream as each check runs,
followed by a deterministic summary. Focused and strict command selection is
unchanged:

```bash
pyrepo-check ty
pyrepo-check pytest tests/test_cli.py::test_name
pyrepo-check --format json ty
pyrepo-check --format json --all
```

`--format json` writes exactly one versioned JSON document and a trailing
newline to stdout. It captures each tool's stdout and stderr inside the
document. Schema version 1 has explicit top-level `pytest: null` and
`coverage: null`; a selected pytest process still appears in `selection.checks`
and `checks`.

The first positive tool exit code remains the CLI exit code. Checks continue
after ordinary failures; when execution has only spawn or signal errors, the
CLI returns `2`.

## Mutating Fixes

`pyrepo-check annotations-fix <target>` edits files. Use it only when source
changes are authorized. Inspect the diff afterward, then rerun `annotations`
and `ty` before the final gate.

## Example

For a type-signature and behavior change in an invoice module:

```bash
pyrepo-check annotations ty src/invoice.py
pyrepo-check pytest tests/test_invoice.py::test_invoice_rounding
pyrepo-check --all
```

## Common Mistakes

- Do not guess names such as `typing`, `typecheck`, or `test`; inspect `--help`.
- Do not treat `ty` alone as proof of annotation-policy compliance.
- Do not bypass the wrapper for targeted pytest when its `pytest` check works.
- Do not use focused checks as a substitute for the final `--all` gate.
- Do not invent coverage or named-group options absent from `--help`.

For installation, configuration, and command expansion details, consult the
project `README.md`.
