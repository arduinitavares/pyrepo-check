# Windows file safety implementation plan

> **For agentic workers:** Use Superpowers systematic-debugging, test-driven-development, and verification-before-completion. Execute the dependent steps in this task; use independent review before completion.

**Goal:** Resolve GitHub issue #2 by running native Windows checks without dropping no-follow, identity, private-artifact, or cleanup guarantees.

**Architecture:** Preserve the POSIX implementation behind a private filesystem adapter. On Windows use native handle-relative operations, reject reparse points, create artifacts with a protected current-user DACL, and delete only verified handles. Windows-specific cleanup retains the existing containment and resource budgets.

**Tech stack:** Python 3.13.15+, ctypes, Windows NT/Win32 APIs, uv, pytest.

**Spec:** https://github.com/arduinitavares/pyrepo-check/issues/2

## Constraints

- No safety-flag bypass, repository policy reduction, or tracked-source changes during a check.
- Native Windows errors must report platform/environment limitations accurately.
- Existing Linux/macOS behavior and tests must remain intact.
- Changes stay on `alex/fix-windows-file-safety`; no deployment or issue closure is part of this implementation.

## Steps

- [x] Reproduce installed-controller failure and record Windows regression failures before implementation.
- [x] Implement native no-follow file/directory opening, private creation, relative stat and enumeration in `_windows_files.py`, exposed through `filesystem.py`.
- [x] Route artifact safety, repository snapshots, launcher, pytest, and coverage operations through the adapter. Preserve descriptor identity/content validation.
- [x] Implement Windows workspace verification and bounded cleanup with held handles, rejecting replacement and reparse-point traversal.
- [x] Run `uv run --frozen pytest tests/test_windows_safety.py -q`; resolve each failure without weakening assertions.
- [x] Make platform-specific existing tests collect correctly and add a Windows CI gate for the installed controller.
- [x] Install the changed controller in an isolated uv tool directory; run the issue reproduction and a real pytest/coverage check, verifying tracked-file hashes before and after.
- [x] Run focused lint, annotations and typing, then JSON and terminal target-free `pyrepo-check --all`; inspect every check result.
- [x] Obtain independent correctness review, address findings, and report verified results and any platform validation limits.

## Review outcome

Independent Sol xhigh review accepted the final native safety implementation. Review regressions cover concurrent writers, namespace aliases, missing file/ACL capabilities, cleanup replacement and resource budgets, and rollback after rejected workspace creation. File-symlink tests skip only the Windows privilege error; junction tests run without that privilege.

## Validation evidence

- Final native Windows target-free terminal `--all`: 1486 passed, 99 narrow platform/matrix/privilege skips in 926.79 seconds. Ruff, annotations, Ty, Bandit and pytest passed; no repository-state or execution error remained. The earlier target-free JSON aggregate was inspected to identify and correct the Windows fixture failures.
- The Windows-only strict aggregate exits 1 solely for coverage: 80.98% against 86.01%. It cannot exercise the POSIX cleanup backend; the combined Linux/Windows gate below passes without lowering the threshold. This limit is documented in README.
- Final Linux source snapshot: 1530 passed, 55 platform/matrix skips. Ruff and Ty passed.
- Final integration-fixture portability checks on Linux: 42 passed; the last startup-identity and tampering-witness changes were then rechecked with 4 passed. Windows storage fixtures use junctions and native default paths; proxy fixtures execute a real `uv.exe` entry point.
- Native Windows CI selection: 192 passed, 48 narrow POSIX or symlink-privilege skips; includes a separately installed controller and real pytest/coverage.
- Native compatibility fixtures: 48 passed; repository environment: 145 passed, 25 narrow skips; check launcher: 52 passed, 4 narrow skips.
- Separately installed final package matches working source bytes; terminal `--all` on a clean locked consumer passed all checks and 100% coverage with protected hashes unchanged.
- Final combined line/branch coverage: 86.22%, above unchanged 86.01% threshold.
- Final Ruff, annotation, Ty, Bandit and diff checks passed.
- Windows CI uses uv 0.12.8: its downloadable catalog includes CPython 3.13.15, while the existing uv 0.10.12 catalog does not. Linux jobs retain their existing uv version.
- All 26 production Python files still match the source hashes used for the reviewed Linux run, native coverage selection and isolated installed-controller verification.

## Native references

- https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile
- https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle

## Rollback

The original checkout was clean at `98247dd`. Reverting this branch's changes restores the previous implementation; tests and installations use disposable tool directories, preserving the user's global controller.
