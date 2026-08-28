# Separate Tool and Repository Environments

**Status:** Implemented and verified
**Date:** 2026-08-26
**Issue:** [#1 — Global pyrepo-check cannot run the full gate for projects on older Python versions](https://github.com/arduinitavares/pyrepo-check/issues/1)
**Related context:** [`CONTEXT.md`](../../../CONTEXT.md)
**Decision:**
[`ADR 0001`](../../adr/0001-separate-tool-and-repository-environments.md)

## Summary

pyrepo-check runs as an install-once controller under Python 3.13.15 or newer,
while every selected Check that can execute runs in the uv-managed Repository
Environment. The Repository Environment is expected to supply one Repository Python
and the repository-owned, locked Check Dependencies. A normal command keeps its
current shape; CI may select one supported Repository Python explicitly with
`--python`.

The planner describes Check intent without embedding uv commands. A deep Repository
Executor proves lock freshness, prepares and observes the Repository Environment,
runs independent Checks, and returns typed execution evidence. Reporting emits
schema version 2 only and identifies the Tool Environment, Repository Environment,
Repository Python, Check Dependencies, and Analysis Python authority without
conflating those facts.

This implemented design supersedes the earlier document's Repository
Python minimum, pytest/Coverage dependency preflights, frozen/unfrozen execution and
`--no-frozen` behavior, schema-v1-only output, missing-Coverage no-fallback rule,
inherited-`PYTHONPATH` rule, and positive-child-exit precedence in
[`2026-08-24-agent-guidance-reporting-design.md`](2026-08-24-agent-guidance-reporting-design.md).
Existing selection, pytest, Coverage, artifact-safety, and quality-policy contracts
remain in force unless this document explicitly changes them.

## Pre-implementation problem

Before this implementation, the `pyrepo-check` package correctly required Python
3.13.15 or newer. Planning expanded each Check into `uv run ... python -m ...`, but
pytest and Coverage preflights also required the Repository Python to be at least
3.13.15. A repository that supported an older Python therefore had to either raise
its runtime requirement or forgo the complete gate, even when its own Ruff, Ty,
Bandit, pytest, and Coverage versions supported that Python.

This coupled two independent responsibilities:

- the Tool Environment imports and runs pyrepo-check's controller code; and
- the Repository Environment executes code and Checks for the repository under
  validation.

It also made Agent Reports ambiguous. A report could show a Python version from a
pytest preflight without explicitly distinguishing the Python running the controller,
the Python executing repository code, and the Python language rules used by static
analysis.

## Goals

1. Preserve an isolated, install-once pyrepo-check Tool Environment on Python
   3.13.15 or newer.
2. Run every executable selected Check in the uv-managed Repository Environment and
   account explicitly for every Check that cannot execute.
3. Validate exactly one uv-selected Repository Python per run.
4. Initially support CPython 3.10, 3.11, 3.12, and 3.13 as Repository Python.
5. Require a present, current `uv.lock` without allowing pyrepo-check to update it.
6. Let uv create or synchronize ignored `.venv` state from the current lock.
7. Use uv's default dependency selection without guessing dependency-group names.
8. Require compatible, repository-owned Check Dependencies rather than injecting
   packages from the Tool Environment.
9. Preserve repository-configured Ruff and Ty Analysis Python behavior.
10. Continue independent Checks after a Check-local dependency or execution error.
11. Emit concise terminal evidence and complete schema-v2 Environment Evidence.
12. Preserve the current strict annotation, Ty, security, pytest, Coverage, target,
    Test Shortcut, and aggregate-gate policies except where stated here.

## Non-goals

- Supporting package managers other than uv.
- Running a Python-version matrix inside one pyrepo-check invocation.
- Supporting Repository Python 3.14 in this milestone.
- Installing, upgrading, or injecting Ruff, Ty, Bandit, pytest, Coverage, or pytest
  plugins into a repository.
- Supporting editable or local-path installations of Ruff, Ty, Bandit, pytest, or
  Coverage as Check Dependencies. The repository project itself may remain editable.
- Guessing dependency groups named `dev`, `test`, `quality`, or similar.
- Making pyrepo-check importable from the Repository Environment.
- Redesigning Ruff, annotation, Ty, Bandit, pytest, Coverage, target, or Test
  Shortcut policy.
- Normalizing Ruff, Ty, or Bandit diagnostics into a new common finding schema.
- Generalizing execution behind a package-manager plugin system.
- Claiming support for PyPy or another Python implementation in the initial matrix.
- Updating personal Codex or Antigravity Skill installations as part of the code
  implementation. Runtime-copy deployment remains a separate, hash-verified action.

## Accepted domain contract

- **Tool Environment:** the isolated environment that imports and executes
  pyrepo-check. Its Python must satisfy pyrepo-check's package requirement.
- **Repository Environment:** the uv-managed environment that executes every Check.
- **Repository Python:** the exact CPython executable observed for this run. It is
  execution evidence, not an inference from `project.requires-python`.
- **Analysis Python:** the Python language version whose syntax and typing rules Ruff
  or Ty applies without executing repository code. It may differ from Repository
  Python.
- **Check Dependency:** a selected Check's required repository-installed package.
- **Environment Evidence:** the typed facts distinguishing orchestration from Check
  execution in an Agent Report.

One run proves one Repository Python. CI proves a supported matrix by invoking
pyrepo-check separately for each selected Python.

## Considered approaches

### Selected: intent-only planning plus a deep Repository Executor

The planner returns semantic Check intent. The Repository Executor owns uv lock
validation, environment preparation, interpreter observation, dependency checks,
command construction, execution, continuation, and Environment Evidence. Reporting
remains a separate projection of typed results.

This approach gives callers a small Interface while concentrating environment
knowledge behind one seam. It preserves current CLI use and avoids a generic
package-manager abstraction.

### Rejected: patch environment probing into current command planning

This is the smallest initial diff, but it retains repeated `uv run` prefixes and
environment policy across planning, pytest, Coverage, and reporting. The next
environment change would again cross those modules.

### Rejected: one module for planning, execution, reporting, and rendering

A single `RepositoryRun` entry point makes the caller small but combines pure policy,
side effects, artifact protocols, schema validation, and presentation. That module
would be difficult to understand and test through one coherent Interface.

### Rejected: generic environment-manager or matrix framework

Only uv and one Repository Python per run are required. A manager registry,
environment array, or internal matrix scheduler adds unsupported flexibility without
current leverage.

## Architecture

The dependency direction is:

```text
CLI adapter -> configuration + planner -> environment-neutral RunPlan
                                        -> Repository Executor
                                           -> typed ExecutionResult
                                           -> schema-v2 Agent Report
                                           -> terminal or JSON renderer
```

### CLI adapter

The CLI parses syntax into a typed `RunRequest`, loads repository configuration,
invokes the planner, executes the plan, and renders one report. It does not construct
uv or Check command vectors, choose dependency groups, infer Python versions, or
classify subprocess output as environment evidence.

### Planning module

Planning remains pure. Its Interface consumes user intent, project configuration,
and filesystem facts and produces an ordered `RunPlan`. A planned Check describes
semantic Check arguments, not its module executable or complete process command.

Illustrative types:

```python
@dataclass(frozen=True)
class DefaultRepositoryPython:
    kind: Literal["default"] = "default"


@dataclass(frozen=True)
class ExplicitRepositoryPython:
    request: str
    kind: Literal["explicit"] = "explicit"


RepositoryPythonSelection = DefaultRepositoryPython | ExplicitRepositoryPython


@dataclass(frozen=True)
class CheckInvocation:
    name: CheckName
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class RunPlan:
    root: Path
    repository_python: RepositoryPythonSelection
    checks: tuple[CheckInvocation, ...]
    # Existing mode, target, pytest, Coverage, and reporting intent follows.
```

Raw `uv` prefixes, a consumer-Python tuple, executable paths, and prepared
environment state do not belong in the planner's Interface.

Concretely, implementation replaces `PlannedCheck.command` with
`CheckInvocation.arguments`, removes `consumer_python` from pytest and Coverage
plans, and puts the resolved project root plus `RepositoryPythonSelection` on
`RunPlan`. Existing mode, target, shortcut, pytest-argument, Coverage-scope,
Coverage-threshold, and artifact-protocol fields retain their current meaning.

### Repository Executor

The Repository Executor is the deep module for side effects. Its public Interface
remains the existing workflow shape:

```python
def execute_plan(
    plan: RunPlan,
    *,
    runner: ProcessRunner | None = None,
) -> ExecutionResult: ...
```

Its Implementation owns:

- Tool Environment observation;
- uv project and lock validation;
- Repository Environment preparation;
- Repository Python observation and pinning;
- selected Check Dependency observation and compatibility classification;
- per-Check process commands and environment variables;
- standalone Check launcher staging and inner-start evidence validation;
- existing bounded capture, timing, signal, and spawn behavior;
- continue-after-error behavior;
- standalone pytest reporter preparation, injection, validation, and cleanup;
- Coverage execution and artifact handling; and
- environment-wide and Check-local execution observations.

The subprocess seam remains internal and justified by two adapters:

- the production adapter executes bounded local subprocesses; and
- the scripted test adapter returns deterministic observations.

uv is invoked through this seam as a true external local dependency. There is no
public `EnvironmentManager`, `UvClient`, or manager registry.

### Reporting module

Reporting consumes only the plan and typed execution observations. It validates one
schema-v2 model and renders terminal or JSON projections from that model. It never
executes a command, reopens the repository environment, or infers evidence from
`project.requires-python`.

## Public CLI contract

The following commands retain their meaning:

```bash
pyrepo-check --all
pyrepo-check ty
pyrepo-check annotations ty src/
pyrepo-check pytest tests/test_example.py
pyrepo-check pytest --shortcut unit
pyrepo-check pytest --coverage
pyrepo-check --format json --all
```

### Repository Python selection

The optional selector is:

```bash
pyrepo-check --python 3.12 --all
```

Accepted values are `3.10`, `3.11`, `3.12`, `3.13`, or an exact patch release
within one of those minors. Values outside that grammar fail during planning and run
no processes with existing code `invalid_arguments`. Omitting the option delegates
selection to normal uv repository rules, including the repository's
`.python-version` when present.

The CLI request is only selection intent. The report always records the exact
interpreter uv actually selected, including its patch version and executable path.

### Removal path for `--no-frozen`

Unlocked execution contradicts the no-tracked-dependency-mutation contract. The next
release continues to recognize `--no-frozen` only to produce a stable planning error:

```text
--no-frozen is incompatible with repository-safe execution.
Hint: Update uv.lock explicitly, then rerun without --no-frozen.
```

The error code is `unsafe_unlocked_execution`; no subprocess runs. A later release
may remove the recognized option after this transition. The flag must not be silently
mapped to a different behavior.

## uv and Repository Environment contract

### Preconditions

The resolved project root must contain `pyproject.toml`. Its absence is the planning
error `uv_project_required`; no subprocess runs. A selected plan may then reach the
executor, where a missing `uv.lock` is the environment-wide error
`repository_lock_missing`.

Configuration is parsed only from a bounded, nonblocking, no-follow stable
regular-file read. The exact parsed bytes are SHA-256-bound to the later repository
baseline; malformed, oversized, non-UTF-8, non-regular, aliased, or replaced
configuration is `invalid_project_config`. Configured, auto-detected default, and
direct Check targets must be nonempty, NUL-free, non-option-like, project-relative,
existing, free of `..`, and contained after resolution. For pytest node selectors,
validation applies to the filesystem prefix before the first `::` while preserving
the suffix.

The executor checks for `uv.lock` directly before spawning any process. If it is
missing, the run reports `repository_lock_missing` without trying `uv --version` or
preparing an environment.

When the lock exists, a safe `uv` executable must be available outside the selected
project. Before any repository-cwd process, the controller resolves `uv` and Git once
from absolute non-empty PATH entries, skips lexical or resolved project-contained
candidates, and pins each canonical path and stable file identity for the run. Every
recorded helper argv uses that exact absolute path. Every construction and use
revalidates the pinned identity. Identity loss is `unsafe_repository_environment`;
already attempted dependency and Check evidence remains present, while only the
unattempted suffix is synthesized as unavailable. The executor captures and parses
`uv --version` before preparing the
repository. A missing executable is `uv_unavailable`; malformed version output is
`environment_evidence_invalid`. The exact uv version is part of Repository
Environment evidence, but runtime compatibility is capability-based rather than a
hard minor-version gate: uv must successfully perform the locked command contract
defined below.

CI initially pins uv `0.10.12` for deterministic integration tests. Updating that pin
requires the real uv fixtures and compatibility matrix to pass; normal repositories
are not rejected merely because their uv minor version is newer.

### Lock proof and environment preparation

After the uv-version probe, the executor runs exactly one environment-preparation
command from the resolved project root:

```text
uv run --locked [--python <request>] python -c <environment-probe>
```

A zero exit plus valid probe evidence proves that `uv.lock` was current for that
preparation. `current` must not be reported from file presence, `--frozen`, or a
failed locked run. If `uv.lock` exists but the locked run fails before valid probe
evidence, lock status is `unverified`; the report retains uv's bounded stderr and
uses `repository_environment_failed` instead of guessing whether the cause was a
stale lock, resolution, Python acquisition, or environment preparation.

Consequently, an observed non-current lock has this exact public behavior: the locked
probe exits nonzero, no Check starts, lock status remains `unverified`, the environment
error is `repository_environment_failed`, and the uv diagnostic remains in bounded
process stderr. pyrepo-check does not parse unstable uv prose into a stronger stale-lock
claim; the error hint tells the user to run `uv lock --check` and repair the lock
outside pyrepo-check.

The locked run may create or synchronize `.venv` but cannot update `uv.lock`. uv uses
its normal default dependency selection; pyrepo-check supplies no group, extra,
`--with`, active-environment, isolated-environment, or no-sync option.

This contract follows uv's official guarantees: [`--locked` requires an up-to-date
lock and exits instead of updating it](https://docs.astral.sh/uv/reference/cli/#uv-run),
[`uv run` synchronizes the project environment before the command](https://docs.astral.sh/uv/concepts/projects/run/),
and [the repository controls uv's default groups](https://docs.astral.sh/uv/concepts/projects/dependencies/#default-groups).

### Child-process environment

Controller environment variables must not silently change Repository Environment
selection. Before any uv or Check child, the executor removes every variable whose
name begins with `PYTHON`, case-insensitively, plus `VIRTUAL_ENV`, `CONDA_PREFIX`, and
`__PYVENV_LAUNCHER__`. It removes all `UV_*` variables, then restores only this exact
allowlist:

- index and authentication: `UV_INDEX`, `UV_DEFAULT_INDEX`, `UV_INDEX_URL`,
  `UV_EXTRA_INDEX_URL`, `UV_FIND_LINKS`, `UV_INDEX_STRATEGY`,
  `UV_KEYRING_PROVIDER`, and variables matching
  `^UV_INDEX_[A-Z0-9_]+_(USERNAME|PASSWORD)$`;
- transport: `UV_NATIVE_TLS`, `UV_SYSTEM_CERTS`, `UV_OFFLINE`, and
  `UV_INSECURE_HOST`;
- cache and installation mechanics: `UV_CACHE_DIR`, `UV_NO_CACHE`, `UV_LINK_MODE`,
  `UV_COMPILE_BYTECODE`, `UV_NO_PROGRESS`, `UV_NO_BUILD`,
  `UV_NO_BUILD_PACKAGE`, `UV_NO_BUILD_ISOLATION`, `UV_NO_BINARY`, and
  `UV_NO_BINARY_PACKAGE`; and
- Python acquisition policy and storage: `UV_PYTHON_DOWNLOADS`,
  `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_CACHE_DIR`, and `UV_PYTHON_BIN_DIR`.

All non-`UV_*` variables other than the explicitly removed Python/environment
variables remain available to repository commands; this preserves ordinary project
credentials and application configuration. Values from restored variables are never
copied into reports. Project, working-directory, environment-path, Python-selection,
dependency-group, extra-package, config-file, lock-mode, frozen-mode,
active-environment, isolated-environment, source-selection, and no-sync overrides
are not restored. The executor supplies the project root, locked mode, and optional
Python request explicitly.

The only controller-supplied `PYTHONPATH` is the existing invocation-owned pytest
reporter directory for a selected pytest primary. It contains standalone reporter
code, never the pyrepo-check package or an inherited controller path, and remains
subject to the existing digest and cleanup contract.

Repository-safety Git probes use a stricter derivative of this environment: remove
every `GIT_*` variable, then set only `GIT_OPTIONAL_LOCKS=0` and `LC_ALL=C`. This
prevents `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, configuration injection, or
another inherited Git override from redirecting a supposedly read-only proof.

`UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`, `UV_PYTHON_PREFERENCE`, and
`UV_PYTHON_SEARCH_PATH` are selection overrides and are not restored. Automatic
Python download is allowed only when uv's surviving acquisition and network policy
allow it. A download is explicitly authorized to write uv's persistent managed-Python
installation directory and optional Python cache/bin locations, including locations
selected by the preserved storage variables or uv's platform defaults derived from
the ordinary home/XDG environment. This external uv state is distinct from the
repository and its `.venv`; it is not mislabeled as ordinary cache-only state. CI can
forbid downloads through `UV_PYTHON_DOWNLOADS=never` or `UV_OFFLINE=1`. See uv's
[storage documentation](https://docs.astral.sh/uv/reference/storage/) for the
managed-Python and cache locations.

The environment probe is standalone source compatible with CPython 3.10 through
3.13. It imports no `pyrepo_check` code. It emits exactly these JSON fields:
`schema_version`, `implementation`, `version`, `executable`, and
`environment_root`. `version` is exactly three integers; both paths are absolute,
lexically normalized strings.

Probe stdout is at most 65,536 bytes of UTF-8 JSON with no duplicate or unknown keys,
maximum nesting depth eight, and no trailing non-whitespace bytes. The controller
validates exact types and requires CPython 3.10 through 3.13. The executable must be
lexically contained in the project's non-symlink `.venv`; a symlinked or external
environment root is `unsafe_repository_environment`. Malformed, truncated, or
contradictory evidence is `environment_evidence_invalid`.

After the probe, every Check is launched through `uv run --locked` with the observed
Repository Python pinned as the Python request. The complete command vector appears
in JSON process evidence. A later process must not silently select another
interpreter; an observed mismatch or invalidated environment is an error.

The ordinary Check command shape is:

```text
uv run --locked --python <observed-executable> python <standalone-launcher> \
  --evidence <start-marker> --check <check-name> --module <check-module> -- <arguments>
```

The executor owns the stable mapping from Check name to `ruff`, `ty`, `bandit`,
`pytest`, or `coverage` for an instrumented pytest primary. Existing pytest reporter
injection and Coverage command variations remain inside their dedicated execution
components, but use the same prefix and sanitized environment.

### Inner Check start proof

An outer uv exit cannot prove that `python -m <check>` started: uv itself can return a
positive exit while synchronizing the environment. Every primary Check therefore
uses a controller-owned standalone launcher compatible with CPython 3.10 through
3.13. The controller copies its digest-validated source into the exclusive run
directory; the launcher imports no `pyrepo_check` code. Immediately before
dispatching the selected module with
`runpy.run_module(..., run_name="__main__", alter_sys=True)`, it writes one
invocation-owned start marker, resets `sys.path[0]` to the repository working
directory, and establishes the native-module startup state. Before `runpy` takes
over `sys.argv[0]`, the launcher supplies the module arguments and sets
`sys.orig_argv` to `[sys.executable, "-m", module, *arguments]`. Together with
`alter_sys=True`, the observed `sys.path[0]`, `sys.argv`, `sys.orig_argv`, and
`__main__.__spec__` must match the equivalent direct `python -m <module> ...`
invocation.

The marker contains exactly `schema_version`, `check`, `module`,
`arguments_sha256`, and Repository Python evidence. The argument digest is SHA-256
over a length-prefixed UTF-8 encoding of the exact module arguments. Marker output is
at most 4,096 bytes and follows the existing invocation-owned regular-file, exclusive
creation, writer-identity, bounded-read, duplicate-key, unknown-key, type, snapshot,
and cleanup rules. Its Python evidence must exactly equal the prepared Repository
Python and its Check, module, and argument digest must equal the plan.

The launcher returns tool `SystemExit` codes unchanged, returns zero when the module
returns normally, and converts an uncaught ordinary Python exception during module
resolution or execution to reserved launcher exit `120` after writing a traceback.
A missing, malformed, contradictory, changed, or absent marker is
`check_start_evidence_invalid`; no exit code is classified as findings without valid
start evidence. A validated marker proves only that the trusted launcher reached the
primary-module dispatch boundary inside Repository Environment. It does not prove
that module code began, nor that collection, analysis, or tests completed. Marker
plus a primary-process exit of `0` or `1` proves that `runpy` returned normally or
propagated the module's `SystemExit`; other exits and signals remain execution
errors.

Successful terminal banners retain the logical `python -m <check-module>` command and
hide launcher paths and marker plumbing. JSON retains the actual process argv and the
typed start evidence.

### Repository mutation

For a Git worktree, `.venv` must be ignored and untracked before uv may create or
synchronize it. A tracked, unignored, or symlinked environment path is an environment
error. In a non-Git uv project, `.venv` must still be a non-symlink path inside the
project root.

The executor detects a Git worktree from the repository root and records bounded
`repository_safety` processes for these read-only command shapes:

```text
git -C <root> rev-parse --show-toplevel
git -C <root> ls-files -z -- .venv
git -C <root> check-ignore --no-index -q -- .venv .venv/
git -C <root> ls-files --stage -z -- .
```

The last command runs before preparation and after execution. Any nonzero-stage entry
in the initial index is `unsafe_repository_environment`; no environment or Check
starts from an unmerged index. A nonzero-stage entry appearing only in the final
snapshot is `repository_state_changed` and follows the post-execution integrity
contract. A Git marker with an unavailable or failed Git executable is
`unsafe_repository_environment` rather than silently treated as a non-Git project.
Existing `.venv` state is inspected with `lstat`; pyrepo-check never follows a
`.venv` symlink.

The executor streams SHA-256 fingerprints of regular, non-symlink `pyproject.toml`
and `uv.lock` files before preparation and after the run. In a Git worktree it also
builds a content snapshot for every stage-zero tracked regular file or symlink under
the project root: repository-relative path, index mode and object id, working-tree
file kind and mode, and SHA-256 of file bytes or symlink-target bytes. Missing paths
remain explicit entries. The executor rebuilds the tracked-file list and snapshot
after every non-mutating Run. This detects changes even when a file was already dirty
before the run while preserving its initial bytes as the baseline. Gitlinks record
their index object but are not dereferenced; nested submodule contents are outside
this mutation proof.

Any snapshot or protected-file mismatch is `repository_state_changed`; pyrepo-check
reports it but does not attempt an unsafe rollback. `annotations-fix` is explicit-only
and exclusive. Its exemption covers only byte changes to tracked regular files
lexically within its exact validated file/directory targets; kind, mode, symlink,
unrelated-file, and protected dependency-file changes still fail.
Because a non-Git project has no authoritative tracked-file set, it receives only the
protected-file proof and must not be reported as having tracked-file protection.

These controls prove what pyrepo-check requests and detects; they are not a sandbox.
A repository build backend, test, plugin, or Check executes repository-controlled
code and can attempt arbitrary writes. Ordinary Check command construction never
requests a source fix, lock update, or dependency-config update.

## Check Dependency contract

The Repository Environment owns all selected Check Dependencies. Initial supported
ranges are:

| Check | Distribution | Import module | Supported range |
| --- | --- | --- | --- |
| `ruff`, `annotations`, `annotations-fix` | `ruff` | `ruff` | `>=0.15,<1` |
| `ty` | `ty` | `ty` | `>=0.0.35,<0.1` |
| `bandit` | `bandit` | `bandit` | `>=1.9,<2` |
| `pytest` | `pytest` | `pytest` | `>=8,<9` |
| requested or configured aggregate Coverage | `coverage` | `coverage` | `>=7.15,<8` |

Only dependencies needed by selected Checks are required or reported. Stable numeric
releases in these ranges are supported; an absent, unparsable, prerelease, or
out-of-range version is unavailable or incompatible evidence rather than an assumed
success.

The five Check Dependencies must be ordinary installed distributions whose import
module is represented by their distribution metadata. Editable or local-path
installations of these tools are intentionally unsupported in this milestone because
they cannot provide the same provenance proof. They report `unusable` with
`check_dependency_unusable` and a remediation hint to install a locked ordinary
distribution. `shadowed` is reserved for a resolved module origin that conflicts with
the ordinary distribution. This restriction does not apply to the repository's own
package.

After the environment probe, the executor runs one isolated dependency probe for
each unique dependency required by the selected Checks, in first-required Check
order. Each probe uses the same sanitized child environment and runs from the real
repository working directory with this command shape:

```text
uv run --locked --python <observed-executable> python -c <dependency-probe>
```

The standalone probe imports no `pyrepo_check` code. It:

1. resolves the import module without importing it;
2. resolves the named distribution and installed version and rejects Check-tool
   `direct_url.json` metadata for editable or local-path installs;
3. proves that the resolved module origin is represented by the normalized installed
   file set of that distribution rather than a repository file or unrelated path; and
4. imports the verified module, catching ordinary import failures.

The dependency is `missing`, `incompatible`, `shadowed`, or `unusable` if any proof
fails. Each dependency has its own bounded `dependency_probe` process, so one broken
dependency does not prevent evidence for another. Module origin is reported; imported
module contents are not.

The probe emits exactly these JSON fields: `schema_version`, `distribution`,
`module`, `status`, `version`, `origin`, and `diagnostic`. It uses the environment
probe's 65,536-byte, UTF-8, duplicate-key, unknown-key, nesting, and trailing-data
bounds. Expected dependency states produce valid JSON and exit zero. Spawn, signal,
nonzero exit, malformed output, or truncated output records the attempted process,
sets the dependency to `unobserved`, and attaches `check_dependency_unusable`.
`unobserved` with a null process and null error is reserved for a probe that could
not be attempted because an environment-wide error occurred first.

These probes replace the Python-floor, availability, and version portions of the
current pytest and Coverage preflights. The duplicate `_MINIMUM_PYTHON_VERSION`
decisions are removed. Pytest and Coverage keep only their specialized execution and
artifact validation. The pytest artifact's reported version must equal the
authoritative dependency evidence.

pytest-xdist, pytest-rerunfailures, and other pytest plugins remain repository-owned
and optional. pyrepo-check neither installs nor enables them. Existing structured
pytest rules continue to reject observed unsupported parallelism or retries.

A missing, incompatible, shadowed, or unusable dependency prevents only its dependent
Checks:

- missing Ruff affects Ruff and annotation Checks;
- missing Ty affects only Ty;
- missing Bandit affects only Bandit;
- missing pytest prevents pytest and its Coverage run; and
- missing Coverage does not suppress pytest. Pytest runs without instrumentation,
  Coverage reports an error, and the overall run remains an error.

For available Coverage, the executor boundedly copies the probed package tree into
the held run workspace before pytest. The post-pytest JSON helper revalidates every
staged file plus its standalone launcher and imports Coverage with that staged root
ahead of the repository. Its environment removes pytest's invocation-owned
`PYTHONPATH` and disables bytecode writes. Before importing non-built-in modules, the
launcher removes its writable workspace/script entry; it also excludes the repository
and starts with `-S`, so repository-environment `sitecustomize` and `.pth` startup code
cannot run. The launcher places the staged Coverage root first, retains stdlib paths,
and appends the original validated import root only for Coverage's transitive modules
(including Python 3.10 `tomli`) and normally installed Coverage plugins. It does not
restore editable/project-local plugin roots that depend on `.pth` processing; such a
configured plugin produces typed `generation_failed` Coverage evidence. The staged
package and launcher are revalidated again after the helper returns and before
`coverage.json` is parsed or trusted. A pytest-created repository shadow, mutation at
the original Coverage package tree, or mutation of the staged copy cannot replace the
staged Coverage producer. Installed transitive and plugin modules remain
repository-owned and are not claimed byte-bound by this protocol.

All other independent Checks continue in their established order.

## Analysis Python contract

Ruff and Ty execute under Repository Python but retain their repository-native static
analysis configuration. pyrepo-check must not pass a controller-derived target
version or rewrite `[tool.ruff].target-version`,
`[tool.ty.environment].python-version`, or another repository setting.

Ruff can resolve different configuration files for different targets, and Ty owns its
own resolution rules. This milestone therefore does not claim one effective numeric
Analysis Python. For every Ruff, annotations, annotations-fix, or Ty primary that has
valid start evidence and completes with exit `0` or `1`, the
`analysis_python_authority` field contains:

```json
{"authority":"repository_tool","pyrepo_check_override":null}
```

This is evidence that pyrepo-check supplied no target-version override and left
effective resolution to the repository's tool configuration. The field is null for
runtime/security Checks and when a static primary does not have both valid start
evidence and a completed exit of `0` or `1`, including signals and reserved or other
positive error exits. The field is not itself an Analysis Python version; exact
per-target effective-version reporting requires separate tool-native evidence and is
outside this milestone.

## Schema version 2

The release emits schema version 2 only. It does not offer a parallel schema-v1
mode. The v1 `Selection`, `ProcessResult`, `PytestResult`, `CoverageResult`,
`Advisory`, captured-output, and their nested shapes retain their exact fields.
`CheckResult` and `CheckError` are explicitly replaced by the v2 shapes below;
`CheckErrorV2` adds the nullable remediation `hint`. The normative v2 additions and
top-level unions are below; serialized tuples become JSON arrays and dataclass field
order is JSON key order.

```python
PythonVersion = tuple[int, int, int]

PlanningErrorCodeV2 = PlanningErrorCode | Literal[
    "uv_project_required",
    "unsafe_unlocked_execution",
]

CheckErrorCodeV2 = CheckErrorCode | Literal[
    "repository_environment_unavailable",
    "check_dependency_missing",
    "check_dependency_incompatible",
    "check_dependency_shadowed",
    "check_dependency_unusable",
    "check_start_evidence_invalid",
    "check_execution_failed",
]


@dataclass(frozen=True)
class PythonEvidence:
    implementation: str
    version: PythonVersion
    executable: str


@dataclass(frozen=True)
class ToolEnvironmentEvidence:
    pyrepo_check_version: str
    python: PythonEvidence


@dataclass(frozen=True)
class PlanningErrorV2:
    code: PlanningErrorCodeV2
    message: str
    hint: str | None


@dataclass(frozen=True)
class RepositoryPythonSelectionEvidence:
    kind: Literal["default", "explicit"]
    request: str | None


@dataclass(frozen=True)
class LockEvidence:
    path: str
    status: Literal["current", "missing", "unverified"]


@dataclass(frozen=True)
class EnvironmentError:
    code: Literal[
        "repository_lock_missing",
        "uv_unavailable",
        "repository_environment_failed",
        "repository_python_unsupported",
        "unsafe_repository_environment",
        "environment_evidence_invalid",
        "repository_state_changed",
    ]
    message: str
    hint: str | None


@dataclass(frozen=True)
class CheckErrorV2:
    code: CheckErrorCodeV2
    message: str
    hint: str | None


@dataclass(frozen=True)
class DependencyEvidence:
    name: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    module: str
    required: str
    status: Literal[
        "available",
        "missing",
        "incompatible",
        "shadowed",
        "unusable",
        "unobserved",
    ]
    version: str | None
    origin: str | None
    process: ProcessResult | None
    error: CheckErrorV2 | None


@dataclass(frozen=True)
class RepositoryEnvironmentEvidence:
    manager: Literal["uv"]
    manager_version: str | None
    path: str | None
    python_selection: RepositoryPythonSelectionEvidence
    python: PythonEvidence | None
    lock: LockEvidence
    dependency_selection: Literal["default"]
    mutation_protection: Literal["unobserved", "protected_files", "tracked_files"]
    dependencies: tuple[DependencyEvidence, ...]
    processes: tuple[ProcessResult, ...]
    error: EnvironmentError | None


@dataclass(frozen=True)
class AnalysisPythonAuthorityEvidence:
    authority: Literal["repository_tool"]
    pyrepo_check_override: None


@dataclass(frozen=True)
class CheckStartEvidence:
    schema_version: Literal[1]
    check: CheckName
    module: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    arguments_sha256: str
    python: PythonEvidence


@dataclass(frozen=True)
class CheckResultV2:
    name: CheckName
    status: CheckStatus
    execution_environment: Literal["repository"] | None
    analysis_python_authority: AnalysisPythonAuthorityEvidence | None
    start_evidence: CheckStartEvidence | None
    processes: tuple[ProcessResult, ...]
    error: CheckErrorV2 | None


@dataclass(frozen=True)
class PlanningErrorReportV2:
    schema_version: Literal[2]
    kind: Literal["planning_error"]
    overall_status: Literal["error"]
    complete: Literal[False]
    tool_environment: ToolEnvironmentEvidence
    repository_environment: None
    error: PlanningErrorV2


@dataclass(frozen=True)
class RunReportV2:
    schema_version: Literal[2]
    kind: Literal["run"]
    project_root: str
    mode: RunMode
    overall_status: OverallStatus
    complete: bool
    tool_environment: ToolEnvironmentEvidence
    repository_environment: RepositoryEnvironmentEvidence
    selection: Selection
    checks: tuple[CheckResultV2, ...]
    pytest: PytestResult | None
    coverage: CoverageResult | None
    advisories: tuple[Advisory, ...]


AgentReportV2 = PlanningErrorReportV2 | RunReportV2
```

`RepositoryPythonSelectionEvidence.request` is null exactly when `kind` is
`default`; it is the validated CLI string exactly when `kind` is `explicit`.
`RepositoryEnvironmentEvidence.processes` contains attempted pre-execution
`repository_safety` processes in command order, then `uv_version`, then
`environment_probe`; the post-run tracked-file listing is last. Dependencies are
ordered by the first selected Check that requires them, with shared Ruff evidence
deduplicated and Coverage after pytest. A dependency's `process` is null only when
its probe was not attempted.

`repository_environment.python` and `repository_environment.path` are null until a
syntactically valid environment-probe payload exists. Once observed, they retain the
actual values even when an unsupported Python or unsafe path produces an environment
error. Lock status is `current` only after the locked environment-probe process exits
zero with syntactically valid evidence, `missing` only after the direct file
precondition fails, and `unverified` otherwise. Environment and dependency processes
reuse the exact bounded `ProcessResult` contract. Successful preparation details are
hidden in terminal mode and retained in JSON.

`mutation_protection` is `tracked_files` only after both Git snapshots can be built,
`protected_files` for a non-Git run whose dependency-file hashes were built, and
`unobserved` before either proof is available. It describes detection coverage, not
sandboxing.

`ProcessRole` adds `repository_safety`, `uv_version`, `environment_probe`, and
`dependency_probe` to its existing values. Repository environment processes use the
first three roles; `DependencyEvidence.process` uses the fourth.

`CheckResultV2.execution_environment` is `repository` only when valid
`start_evidence` proves the trusted launcher reached the inner primary-module
dispatch boundary in the prepared Repository Environment. This field identifies the
environment of the trusted dispatch attempt; it does not claim that module code
began. An outer positive error, signal, or spawn failure without that marker may have
failed inside uv before the launcher, so its execution field is null.
`analysis_python_authority` has a stronger rule: it is non-null only for a static
primary with valid start evidence and exit `0` or `1`; it is otherwise null.
`arguments_sha256` is exactly 64 lowercase hexadecimal characters, and all
start-evidence fields must equal the validated invocation.

A `passed` or `failed` Check has at least one exited primary process,
valid start evidence, `execution_environment: "repository"`, and `error: null`. An
`error` Check always has a non-null error. A synthesized environment or dependency
error has no Check process and null start/execution/analysis fields. A spawn failure
retains its `spawn_failed` process but also has those three fields null. Signal or
positive outer errors without valid start evidence likewise keep them null and use
`check_start_evidence_invalid`. With valid start evidence, signal and positive
execution errors retain `execution_environment: "repository"` because the trusted
launcher reached the dispatch boundary, but they keep
`analysis_python_authority: null`. Artifact or later-process failures retain the
authority field only when the static primary had already completed with exit `0` or
`1`.

For dependency evidence, `available` requires non-null version, origin, successful
probe process, and null error. `missing` attaches `check_dependency_missing`;
`incompatible` requires a known version and attaches
`check_dependency_incompatible`; `shadowed` requires the conflicting origin and
attaches `check_dependency_shadowed`; `unusable` attaches
`check_dependency_unusable`. These four states require an attempted probe. The two
allowed `unobserved` forms are the attempted-probe evidence failure and the
environment-wide not-attempted form defined above.

`RunReportV2.complete` is true when every selected Check and requested pytest or
Coverage result has complete execution evidence and no environment, dependency,
process, or artifact error exists. Completed findings or a Coverage threshold failure
may therefore be `overall_status: "failed"` with `complete: true`. Any typed error or
post-execution integrity error requires `overall_status: "error"` and
`complete: false`; otherwise the overall status is `passed`.

The new Check error codes are:

```text
repository_environment_unavailable
check_dependency_missing
check_dependency_incompatible
check_dependency_shadowed
check_dependency_unusable
check_start_evidence_invalid
check_execution_failed
```

Existing Check error codes remain. A dependency error object names the dependency,
installed version and origin when known, required range, and one remediation hint.
Missing pytest or Coverage uses its existing `module_unavailable` nested error;
incompatible versions use `unsupported_version`; shadowed, unusable, or unobserved
evidence uses `preflight_invalid`. An environment-wide error synthesizes
`preflight_invalid` for selected pytest or Coverage because neither preflight could
produce evidence. Their existing result fields and nullability remain unchanged.
When pytest itself is unavailable, requested Coverage uses `preflight_invalid`
because no instrumentable test process can start. When only Coverage is unavailable,
pytest executes once without instrumentation and keeps its actual result while
Coverage uses `module_unavailable`; the top-level run remains incomplete and in
error.

A complete planning-error payload has this shape:

```json
{
  "schema_version": 2,
  "kind": "planning_error",
  "overall_status": "error",
  "complete": false,
  "tool_environment": {
    "pyrepo_check_version": "0.1.0",
    "python": {
      "implementation": "cpython",
      "version": [3, 13, 15],
      "executable": "/tool/bin/python"
    }
  },
  "repository_environment": null,
  "error": {
    "code": "unsafe_unlocked_execution",
    "message": "--no-frozen is incompatible with repository-safe execution.",
    "hint": "Update uv.lock explicitly, then rerun without --no-frozen."
  }
}
```

For a pre-execution environment-wide failure, the report is a `run`, retains the
complete selection, uses a non-null partial `repository_environment`, and gives every
selected Check `status: "error"`, `execution_environment: null`,
`analysis_python_authority: null`, `start_evidence: null`, no Check processes, and
`repository_environment_unavailable`. Selected pytest and Coverage fields contain
their existing typed error result with no evidence rather than being silently null.
For example, a focused Ty run with a missing lock contains:

```json
{
  "schema_version": 2,
  "kind": "run",
  "project_root": "/project",
  "mode": "focused",
  "overall_status": "error",
  "complete": false,
  "tool_environment": {
    "pyrepo_check_version": "0.1.0",
    "python": {
      "implementation": "cpython",
      "version": [3, 13, 15],
      "executable": "/tool/bin/python"
    }
  },
  "repository_environment": {
    "manager": "uv",
    "manager_version": null,
    "path": null,
    "python_selection": {"kind": "default", "request": null},
    "python": null,
    "lock": {"path": "/project/uv.lock", "status": "missing"},
    "dependency_selection": "default",
    "mutation_protection": "unobserved",
    "dependencies": [
      {
        "name": "ty",
        "module": "ty",
        "required": ">=0.0.35,<0.1",
        "status": "unobserved",
        "version": null,
        "origin": null,
        "process": null,
        "error": null
      }
    ],
    "processes": [],
    "error": {
      "code": "repository_lock_missing",
      "message": "uv.lock is required.",
      "hint": "Create and commit uv.lock outside pyrepo-check, then retry."
    }
  },
  "selection": {
    "checks": ["ty"],
    "targets": [],
    "test_shortcut": null,
    "pytest_args": null,
    "planned_test_scope": "not_selected",
    "planned_coverage_scope": "not_requested"
  },
  "checks": [
    {
      "name": "ty",
      "status": "error",
      "execution_environment": null,
      "analysis_python_authority": null,
      "start_evidence": null,
      "processes": [],
      "error": {
        "code": "repository_environment_unavailable",
        "message": "Ty did not run because the Repository Environment is unavailable.",
        "hint": "Resolve the Repository Environment error, then retry."
      }
    }
  ],
  "pytest": null,
  "coverage": null,
  "advisories": []
}
```

Every successful run has `repository_environment.error: null`, current lock evidence,
valid Repository Python, one available dependency entry for every selected Check
requirement, valid start evidence, and `execution_environment: "repository"` for every
executed Check.

The report builder and validator use v2-specific types such as `RunReportV2` and
`PlanningErrorReportV2`. V1-only type aliases, validators, and serializers are
removed rather than kept as a second public mode.

## Terminal contract

After successful environment preparation and before Check banners, terminal mode
prints one concise line:

```text
==> environment: tool Python 3.13.15 -> repository Python 3.12.11 (uv, locked)
```

Successful dependency versions are not printed. Missing, incompatible, shadowed,
unusable, or unobserved dependency diagnostics identify the affected Check, installed
version and origin when known, required range, and remediation. Existing tool output
and the final compact summary remain.

JSON mode emits exactly one JSON document on stdout. Environment preparation and
Check output remain captured within the report; incidental diagnostics must not
corrupt stdout.

## Error and exit contract

Errors fall into four categories.

### Planning errors

Invalid CLI combinations, invalid `--python`, invalid project configuration, and
`--no-frozen` fail before subprocess execution. A missing `pyproject.toml` uses
`uv_project_required`; `--no-frozen` uses `unsafe_unlocked_execution`. They produce a
schema-v2 `planning_error` and exit `2`.

### Pre-execution environment-wide errors

Examples include:

- `repository_lock_missing`;
- `uv_unavailable`;
- `repository_environment_failed`;
- `repository_python_unsupported`;
- `unsafe_repository_environment`;
- `environment_evidence_invalid`.

No Check can execute without a valid Repository Environment. Every selected Check is
accounted for as `status: "error"` with no primary process, the run is incomplete,
the overall status is `error`, and the exit code is `2`. Available preparation
process evidence remains in the report.

### Post-execution integrity errors

`repository_state_changed` is detected after one or more Checks may have completed.
Their actual results and process evidence remain unchanged; pyrepo-check does not
rewrite them as if they never ran. The repository-environment error is set, the
overall status becomes `error`, `complete` becomes false, and the public exit is `2`.
No rollback is attempted.

### Check-local outcomes

- Missing, incompatible, shadowed, or unusable Check Dependency: affected Check is
  `error`; later independent Checks continue; run is incomplete; exit `2`.
- Spawn, signal, pytest/Coverage artifact, or other evidence failure: affected Check
  is `error`; later independent Checks continue; run is incomplete; exit `2`.
- A completed Check that identifies source, type, security, or test failures is
  `failed`; its evidence is complete; exit `1` unless another error requires `2`.
- All selected Checks pass with complete evidence: exit `0`.

Exit priority is error `2`, then completed failure `1`, then pass `0`. A positive
child exit code remains captured in process evidence but does not replace this stable
public exit contract.

For ordinary non-pytest Checks, process exits are classified before aggregation only
after start evidence validates. Without it, any exited or signaled outcome is
`check_start_evidence_invalid`, not a completed finding:

| Check command | Exit `0` | Exit `1` | Other positive exit, signal, or spawn failure |
| --- | --- | --- | --- |
| Ruff and annotations | passed | completed findings: failed | tool/config/execution error |
| `annotations-fix` | passed | remaining findings: failed | tool/config/execution error |
| Ty | passed | completed findings: failed | tool/config/execution error |
| Bandit | passed | completed findings: failed | tool/config/execution error |

Pytest and Coverage retain their existing structured exit/artifact classification;
usage, interruption, internal, artifact, or incompatible-evidence outcomes are errors,
not completed test failures. Tests lock every mapping to the supported tool ranges.

## Testing strategy

### Fast contract tests

The scripted subprocess adapter covers:

- intent-only planning and stable Check order;
- default and explicit Repository Python selection;
- `--no-frozen` rejection without a process;
- direct missing-lock failure before any process;
- repository-safety, uv-version, and locked environment-process construction;
- uv version evidence, capability failure, and every allowed or blocked
  environment-variable category;
- conservative inner-Check execution attribution for outer uv failures;
- standalone-launcher Python 3.10 syntax, marker bounds, exact-field validation,
  argument digest, interpreter match, tamper handling, and cleanup;
- native-startup parity for `sys.path[0]`, `sys.argv`, `sys.orig_argv`, and
  `__main__.__spec__` under direct pytest and Coverage-wrapped pytest;
- exit `0`, `1`, other positive, signal, and spawn outcomes with and without valid
  start evidence;
- strict probe bounds and schema validation;
- CPython 3.10 through 3.13 acceptance and other versions/implementations rejection;
- selected dependency presence, absence, incompatibility, provenance, import failure,
  editable/local-path rejection, shadowing, and sharing;
- interpreter pinning across all Check processes;
- independent continuation and exit priority;
- pytest fallback when Coverage is unavailable;
- Analysis Python authority and absence of pyrepo-check overrides;
- per-tool exit classification;
- exact schema-v2 key, type, nullability, and enum contracts; and
- concise terminal snapshots and JSON stdout isolation.

Tests exercise planning and execution through their Interfaces. They do not require a
public environment-manager Interface or assert private helper structure.

### Real uv integration tests

Temporary fixture repositories verify:

- missing `.venv` reconstruction from a current lock;
- missing and non-current lock failures;
- Git-marker/Git-executable handling, ignored/untracked `.venv` enforcement, and
  unsafe-path or unmerged-index rejection;
- protected-file and already-dirty tracked-file content change detection;
- managed-Python download storage authorization and disabled-download behavior;
- repository-owned dependencies and default dependency selection;
- controller environment-variable isolation;
- absence of pyrepo-check from the Repository Environment;
- valid Check start evidence from the standalone launcher and rejection of an outer uv
  exit `1` before launcher dispatch;
- exact Repository Python evidence;
- explicit Ruff and Ty Analysis Python preservation;
- standalone pytest reporter and Coverage execution from the Repository Environment;
- missing dependency continuation; and
- hostile, malformed, truncated, or contradictory environment/artifact evidence.

Tracked fixture hashes are captured before and after runs. Only an explicit
`annotations-fix` test may expect source mutation.

### Controller/repository compatibility matrix

CI runs four separate jobs or invocations:

```text
Tool Python 3.13.15 -> Repository CPython 3.10
Tool Python 3.13.15 -> Repository CPython 3.11
Tool Python 3.13.15 -> Repository CPython 3.12
Tool Python 3.13.15 -> Repository CPython 3.13
```

Each fixture owns a current lock and compatible Check Dependencies. Each invocation
asserts:

- one exact Repository Python was selected;
- pyrepo-check is unavailable for import in the Repository Environment;
- all executed selected Checks identify `execution_environment: "repository"`;
- Ruff, annotations, Ty, Bandit, pytest, and Coverage return valid evidence;
- Tool and Repository Environment fields are correct; and
- tracked fixture bytes remain unchanged.

Python 3.14 is outside this matrix and receives no support claim.

### Repository completion gate

After focused tests and the compatibility matrix pass, the implementation branch
must pass the repository's existing strict aggregate command and native Coverage
threshold. The threshold and strict typing policy do not change in this milestone.

## Documentation and rollout

Implementation must update:

- README installation and usage guidance;
- CLI help and examples;
- the repository Agent Skill under `.agents/skills/pyrepo-check`;
- JSON schema documentation and examples; and
- the older reporting design's delivery status or successor note.

The documentation must explain that pyrepo-check is installed once, while each
repository supplies its own uv environment and compatible Check Dependencies. It
must show normal default selection and an explicit CI `--python` example.

Personal Codex or Antigravity Skill copies are not updated automatically. Any later
deployment must first compare repository and installed hashes and preserve
`SKILL.md` exactly.

## Acceptance criteria

1. The pyrepo-check package still requires Python 3.13.15 or newer.
2. A uv repository does not need pyrepo-check installed in its Repository
   Environment.
3. Every executed selected Check runs through the repository-owned uv environment and
   has validated inner-start evidence; every Check that cannot execute is represented
   by typed error evidence rather than inferred from an outer uv exit.
4. A normal run selects one repository-native CPython; `--python` selects one
   supported explicit version for CI.
5. Repository Python 3.10, 3.11, 3.12, and 3.13 pass the real compatibility matrix;
   Python 3.14 is not claimed.
6. A missing lock reports `repository_lock_missing`. An observed non-current lock
   reports `repository_environment_failed`, `lock.status: "unverified"`, and bounded
   uv diagnostics before any Check runs. `current` is reported only after a successful
   locked environment probe.
7. uv may rebuild a safe ignored `.venv`; protected dependency files are fingerprinted,
   Git tracked-file bytes are snapshotted even when initially dirty, the reported
   mutation-protection scope is accurate, unmerged indexes are rejected, and the
   report does not claim sandboxing. Allowed Python downloads may write only the
   documented uv-managed external storage, never tracked repository state.
8. Only selected, compatible, metadata-backed repository-owned Check Dependencies are
   used; editable/local-path Check tool installs are rejected and none are injected
   automatically.
9. A Check-local dependency error does not suppress independent Checks.
10. Missing Coverage does not suppress pytest, but leaves Coverage and the overall
    run in error.
11. Ruff and Ty retain repository-controlled Analysis Python semantics, and the report
    makes no unsupported scalar effective-version claim.
12. Schema version 2 identifies Tool Environment, Repository Environment, exact
    Repository Python, dependency versions, lock proof, and each Check's execution
    environment.
13. Terminal output exposes one concise environment line and actionable errors.
14. Schema version 1 is not emitted by the new release.
15. `--no-frozen` is recognized only to fail safely and clearly; it never enables
    tracked dependency mutation.
16. Existing focused targets, Test Shortcuts, strict aggregate ordering, typing,
    Bandit, pytest evidence, Coverage guidance, and artifact-safety behavior remain.
17. Fast tests, real uv integration tests, the four-version compatibility matrix, and
    the repository strict aggregate gate all pass.

## Implementation evidence

The environment implementation reached Task 9 HEAD
`709e6b4b0890c8b1680da768cdaa3dd87f007f1d`. Its real uv integration suite passed
all 42 cases. Separate Tool-Python-3.13.15 runs selected Repository CPython 3.10,
3.11, 3.12, and 3.13 successfully. The exact-HEAD strict gate passed 1,466 tests
with one intentional matrix-selector skip and complete Coverage of 86.56%, above
the configured 86.01% threshold.

Task 10 then published the controller/repository usage contract, repository Agent
Skill, and complete schema-v2 reference. Fresh final verification passed 1,471 tests
with the same one intentional selector skip. The strict aggregate passed all checks
and complete Coverage at 86.56%, again evaluating and passing the 86.01% threshold.
The schema-v2 JSON gate reported current lock evidence, Tool and Repository Python
3.13.15, all five required dependencies available, repository execution attribution
for every Check, complete pytest and Coverage evidence, and no environment error.

Merge, push, release, worktree cleanup, and personal Skill deployment remain separate
explicit actions.
