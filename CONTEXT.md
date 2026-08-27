# pyrepo-check

pyrepo-check provides repeatable quality validation for Python repositories, with fast focused feedback during development and a strict final gate before completion.

## Language

**Check**:
A named kind of repository validation, such as linting, annotation enforcement, type consistency, security scanning, or tests.
_Avoid_: Step, tool

**Target**:
A file, directory, or test identifier that narrows where selected checks run.
_Avoid_: Scope, path

**Focused Run**:
A run limited to chosen checks or targets for fast development feedback; it is not final completion evidence.
_Avoid_: Partial gate, quick gate

**Strict Aggregate Gate**:
The full repository validation that runs every required non-mutating check and the complete test suite before completion.
_Avoid_: Full run, all checks

**Coverage Guidance**:
A report that identifies production behavior not exercised by tests and directs attention to exact coverage gaps.
_Avoid_: Coverage score, coverage gate

**Agent Report**:
A structured account of a run's selected checks, outcomes, and actionable findings.
_Avoid_: Raw log, dashboard

**Tool Environment**:
The isolated Python environment in which the installed pyrepo-check command runs.
_Avoid_: Global environment, pyrepo-check environment

**Repository Environment**:
The uv-managed Python environment whose interpreter and installed packages execute Checks for the repository being validated.
_Avoid_: Target environment, consumer environment

**Repository Python**:
The exact Python interpreter selected by uv for one validation run; it is observed execution evidence, not inferred from a supported-version range.
_Avoid_: Target Python, minimum Python

**Analysis Python**:
The Python language version whose syntax and typing rules a static Check applies without executing repository code; it may differ from the Repository Python.
_Avoid_: Target Python, checker Python

**Check Dependency**:
A package that must be importable in the Repository Environment for a selected Check to execute, such as Ruff, Ty, Bandit, pytest, or Coverage.py.
_Avoid_: Global library, bundled tool

**Environment Evidence**:
The facts in an Agent Report that distinguish the Tool Environment from the Repository Environment and identify the Repository Python that produced each result.
_Avoid_: Python version, environment metadata

**Test Shortcut**:
A project-defined name for a repeatable subset of tests used in a Focused Run; direct test targets remain available.
_Avoid_: Test group, built-in category, test suite
