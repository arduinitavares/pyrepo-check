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

`annotations` enforces annotation policy; `ty` checks type correctness. Run
both for typing-related edits. A target-only command such as
`pyrepo-check src/module.py` runs the file-oriented checks but does not run
pytest.

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
- Do not invent coverage, JSON, or named-group options absent from `--help`.

For installation, configuration, and command expansion details, consult the
project `README.md`.
