# Structured pytest reporting dependency spike

**Date:** 2026-08-24

**Milestone:** C2 — structured pytest evidence

**Decision status:** Historical research recommendation, now implemented by C2
with the approved standalone plugin and no external reporting dependency

## Conclusion

Do **not** add an external pytest reporting dependency for the approved C2
contract.

`pytest-reportlog` is the closest candidate because it preserves individual
setup/call/teardown reports and a session-finish event. It still does not
provide C2's scope proof, deselection evidence, retry rejection policy, or
atomic artifact. It is compatible with the Python `>=3.13.15` floor, so Python
compatibility is not a reason to reject it. A companion plugin would still
have to implement most of the hard parts, while loading the dependency into
the consumer's pytest environment would introduce a second environment
resolution boundary and a version-sensitive serialization boundary.

The strongest result remains the design already in the approved specification:
a small standalone plugin, loaded into the consumer pytest process, that owns a
versioned raw artifact; `pyrepo-check` then validates that artifact and builds
its public `PytestResult`. External projects are useful comparison fixtures,
but none improves the final result enough to justify its deployment and trust
cost.

If the product later relaxes C2 to “best-effort structured test output” and
drops exact scope, atomic finalization, and fail-closed retries,
`pytest-reportlog` should be reconsidered first. That would be a different
contract; changing the Python floor is unnecessary.

## Question and acceptance boundary

This spike asks whether a maintained library can improve C2's final result,
not merely whether it can emit JSON.

The approved [reporting design](../superpowers/specs/2026-08-24-agent-guidance-reporting-design.md)
requires one selected pytest run to produce authoritative evidence for:

- collected and deselected counts;
- passed, failed, error, skipped, XFAIL, and XPASS outcomes;
- collection errors and collection skips;
- setup/call/teardown durations and the ten slowest complete nodes;
- final effective pytest arguments and semantic narrowing options;
- complete versus partial scope, including plugin-driven deselection or
  unreported collection reduction;
- a finalized, versioned artifact whose pytest version and exit code reconcile
  with preflight and the subprocess;
- explicit rejection of active xdist and observable retry protocols in schema
  version 1;
- consumer Python `>=3.13.15` and pytest `>=8,<9`; and
- atomic artifact publication, with malformed, missing, stale, partial, or
  multi-writer evidence failing closed.

The repository currently has no runtime dependencies and declares pytest only
in its development group as `pytest>=8,<9`.

## Method and evidence labels

Research used primary sources only: official pytest and uv documentation,
official PyPI metadata, and upstream package source/release history. Package
facts below are current as of the date at the top of this document.

Evidence labels mean:

- **Sourced fact** — directly documented or visible in upstream source/package
  metadata.
- **Local probe** — observed in a throwaway environment; useful evidence, but
  not an upstream compatibility guarantee or a product test.
- **Inference** — a conclusion from the sourced implementation and C2 contract.
- **Unknown** — not established by the reviewed sources or bounded probe.

The reproducible local probe used CPython 3.13.15 on Darwin arm64,
pytest 8.4.2, pytest-reportlog 1.0.0,
pytest-json-report 1.5.0, pytest-xdist 3.8.0, and
pytest-rerunfailures 16.6. It exercised normal outcomes, collection failure,
deselection, xdist, and retry-shaped reports. Exact fixtures, commands, and
artifact queries appear in the reproduction appendix.

## Decision matrix

| Candidate | Richness for C2 | Scope completeness | Retry / xdist truth | Artifact semantics | Compatibility and cost | C2 verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `pytest-reportlog` 1.0.0 | High raw phase richness; aggregation is ours | Missing deselection, effective args, and collection-reduction proof | Retry reports remain observable; xdist is supported rather than rejected | Incremental JSONL, flushed per event; direct non-atomic writer; finish marker | Compatible with Python `>=3.13.15`; one direct pytest dependency; MIT; active pytest-dev project | Closest, but still fails mandatory boundaries |
| `pytest-json-report` 1.5.0 | Rich ready-made summary, collectors, phases, durations | Has deselected count in one process, but no effective-argument or reduction proof | Collapses repeated phases by node/phase; xdist loses some collection evidence | One final JSON file, written directly; consumer hook can mutate full report | Old 2022 release; pytest plus `pytest-metadata`; MIT | Convenient, but retry and trust behavior disqualify it |
| `pytest-reporter` 0.5.3 | Rich live pytest objects and phase context | Collected item map, but no authoritative deselection/effective-option proof | No documented fail-closed retry or xdist contract | Template rendering writes directly; context remains mutable pytest state | Compatible with Python `>=3.13.15`; one pytest dependency; beta; 2024 release | Presentation framework, not durable evidence |
| Built-in JUnit XML | Basic counts, failures/errors/skips, combined duration | No deselection or selector proof | Flattens pytest-specific outcome and attempt details | One final XML file, written directly; no recorded pytest exit code | No added dependency; maintained with pytest; MIT | Too lossy for agent evidence |
| `pytest-json-ctrf` 0.5.3 | Basic per-test status/timing and summary | No collection, deselection, or effective-option proof | Designed to accept xdist and summarize outcomes; no fail-closed retry evidence | One final JSON file, written directly | Python `>=3.8`; pytest direct dependency; MIT; recent release | Maintained, but aimed at portable CI display, not C2 truth |
| `pytest-beacon` 0.6.0 | Rich CTRF, collection errors, pytest-style counts, agent-oriented formats | Summary has deselection, but no effective-option or hidden reduction proof | xdist is supported; retries are summarized rather than rejected | Direct report/export surface; optional filtering and HTTP delivery | Requires pytest `>=9`, outside C2's pytest `>=8,<9`; five runtime dependencies; alpha | Incompatible with C2 and broader than needed |
| `allure-pytest` 2.16.0 | Very rich test lifecycle, attachments, parameters, retry presentation | No authoritative pytest collection/scope record | Preserves retry-oriented result history; parallel result directories are a feature | Multiple append-oriented result files; no single session-finalized artifact | pytest plus exact `allure-python-commons`; Apache-2.0; mature and active | Powerful but much broader and less authoritative than C2 needs |
| Approved standalone plugin | Exact C2 raw schema | Can observe args, semantic options, deselection, and collection mutation | Can reject xdist and repeated/non-core reports before accepting evidence | Owner-only temp dir plus atomic, versioned, finalized artifact | No runtime dependency; Python 3.13 grammar; consumer pytest only | Recommended |

## Candidate findings

### 1. pytest-reportlog 1.0.0

#### Sourced facts

The project is owned by `pytest-dev`, is marked production/stable, uses the MIT
license, and released 1.0.0 on 2025-11-11. The current package requires Python
`>=3.10` and depends only on pytest. The 1.0.0 changelog explicitly dropped
Python 3.7–3.9. See the [PyPI project and release history](https://pypi.org/project/pytest-reportlog/)
and [1.0.0 project metadata](https://github.com/pytest-dev/pytest-reportlog/blob/v1.0.0/pyproject.toml#L17-L43).
Those versions are all below C2's Python `>=3.13.15` floor, so the current
release is compatible with the approved consumer requirement.

`--report-log=FILE` writes JSON Lines as the session executes. Upstream says
each event is self-contained and each line is flushed so readers can consume it
in real time. The implementation opens the destination directly in write mode,
writes `SessionStart`, serializes each `TestReport` and `CollectReport`, flushes
after every line, and writes `SessionFinish` with an exit status. It has no
`pytest_deselected` hook. See the [official format description](https://pypi.org/project/pytest-reportlog/)
and [1.0.0 writer source](https://github.com/pytest-dev/pytest-reportlog/blob/v1.0.0/src/pytest_reportlog/plugin.py#L49-L134).

The changelog records xdist fixes, and the plugin deliberately creates only a
controller-side writer when `workerinput` is absent. Thus xdist aggregation is
a supported use case, not a condition it rejects.

The writer asks pytest's consumer-extensible
`pytest_report_to_serializable` hook for each report and imports
`_pytest.pathlib.Path`, a private pytest module. Upstream explicitly tells
consumers to ignore unknown event types and fields for forward compatibility.
Therefore the JSONL shape is version-sensitive and can be influenced by other
loaded plugins; it is useful raw material, but not intrinsically authoritative
C2 evidence. See the [1.0.0 writer source](https://github.com/pytest-dev/pytest-reportlog/blob/v1.0.0/src/pytest_reportlog/plugin.py#L1-L134).

#### Local probe

- Normal runs preserved separate setup/call/teardown records and durations.
- Retry-shaped execution emitted duplicate setup/call records, including a
  `rerun` outcome followed by the final outcome. That is sufficient for a C2
  adapter to detect and reject the observed retry instead of flattening it.
- Under xdist, the report log contained no `CollectReport` entries in the
  bounded probe.

#### Inference against C2

This is the best raw-event substrate reviewed. C2 could derive node durations,
phase failures, collection failures, and most expected-failure metadata from
the event stream.

It is still not a complete substrate:

- there is no deselection event or count;
- there is no final effective-argument or semantic-option snapshot;
- it cannot prove that another collection hook removed items without reporting
  deselection;
- JSONL is deliberately visible before completion, not atomically published;
- a missing `SessionFinish` can flag incompleteness, but a killed writer can
  leave a truncated last record;
- its consumer-extensible serialized report shape still requires strict schema
  validation and C2's pytest-8-specific XPASS validation shim; and
- it relies on a private pytest import despite fitting the Python floor.

A companion C2 plugin could add the missing scope and finalization records, but
at Python `>=3.13.15` that companion would still own effective arguments,
semantic options, deselection, hidden collection reduction, retry and xdist
rejection, writer/session cardinality, and atomic finalization. The dependency
would replace only event serialization while adding deployment and schema-trust
surfaces.

### 2. pytest-json-report 1.5.0

#### Sourced facts

The plugin exposes a session summary, collectors, per-test setup/call/teardown
stages, captured output, logs, tracebacks, warnings, and customization hooks.
Its documented summary includes collected, deselected, passed, failed, XFAIL,
XPASS, error, skipped, and total counts. See the [official PyPI format documentation](https://pypi.org/project/pytest-json-report/).

Release 1.5.0 was uploaded on 2022-03-15 and remains the latest release. Its
package metadata declares MIT, beta status, `pytest>=3.8.0`, and a direct
runtime dependency on `pytest-metadata`; classifiers stop at Python 3.10. See the
[1.5.0 package source](https://github.com/numirias/pytest-json-report/blob/v1.5.0/setup.py#L16-L45).

The implementation keys tests by node ID and assigns each incoming phase to
`json_testitem[report.when]`. A repeated setup/call/teardown therefore
overwrites the previous phase. It writes during a `tryfirst`
`pytest_sessionfinish`, permits consumer code to mutate the entire report via
`pytest_json_modifyreport`, then opens the destination directly and calls
`json.dump`. A file-write `OSError` is converted to a terminal message rather
than a pytest evidence failure. See the [aggregation and writer source](https://github.com/numirias/pytest-json-report/blob/v1.5.0/pytest_jsonreport/plugin.py#L166-L280).

#### Local probe

- The normal summary was `passed=1`, `failed=1`, `skipped=1`, `xfailed=1`,
  `xpassed=1`, `error=1`, `total=6`, `collected=7`, and `deselected=1`, with
  useful phase details.
- A collection error was visible only as a failed collector plus pytest exit
  code 2.
- Under xdist, a requested deselection was omitted from the JSON evidence.
- With pytest-rerunfailures, the report retained only the final phase/result and
  hid the preceding retry.

#### Inference against C2

This candidate gets closest to C2's desired public presentation, but its
pre-aggregation is exactly the wrong trust boundary for version 1: an earlier
failed/rerun attempt can disappear. Consumer hooks can also modify or remove
evidence before it is saved. It cannot establish effective selectors or hidden
collection reduction, and the direct final write is not atomic. Its age and
extra `pytest-metadata` dependency increase compatibility cost without solving
the hard requirements.

### 3. pytest-reporter 0.5.3

#### Sourced facts

`pytest-reporter` is a template-oriented reporting framework. PyPI lists
version 0.5.3, released 2024-02-28, as beta, MIT, Python `>=3.8`, with pytest as
its only dependency. Its template context exposes live pytest `Config`,
`Session`, collected `Item`, `CallInfo`, `TestReport`, and log-record objects.
See the [official PyPI metadata and context description](https://pypi.org/project/pytest-reporter/).

The implementation stores collected items and phase reports in mutable
in-process dictionaries, imports `_pytest.hookspec`, calls
`session.perform_collect()` when xdist skips controller collection, lets
consumer hooks mutate the context and render arbitrary content, and writes the
rendered report directly to its target path. See the
[0.5.3 plugin source](https://github.com/christiansandberg/pytest-reporter/blob/v0.5.3/pytest_reporter/plugin.py#L1-L225).

#### Inference against C2

This library helps humans build presentation templates; it does not define a
durable, versioned evidence schema. A C2 adapter would still have to transform
live/private pytest objects, prove effective arguments and deselection, detect
hidden collection reduction, reject retries/xdist, establish writer/session
cardinality, and publish atomically. It therefore adds mutable hook and private
API surfaces without removing the difficult C2 work.

### 4. Built-in JUnit XML

#### Sourced facts

pytest documents `--junit-xml=path` as its built-in CI report. By default the
`time` attribute includes setup, call, and teardown; it can instead be limited
to call duration. See [pytest's JUnit XML documentation](https://docs.pytest.org/en/8.4.x/how-to/output.html#creating-junitxml-format-files).

In pytest 8.4.2, JUnit handling consumes runtest reports but consolidates them
into `<testcase>` entries. XFAIL is encoded as a skipped testcase;
non-strict XPASS is just a pass; strict XPASS is not represented as a distinct
pytest outcome. Collection failure becomes an error testcase. The session
writer directly opens the final path in write mode and records suite counts and
duration, but not pytest's session exit code or deselection count. See the
[pytest 8.4.2 implementation](https://github.com/pytest-dev/pytest/blob/8.4.2/src/_pytest/junitxml.py#L188-L242)
and [session writer](https://github.com/pytest-dev/pytest/blob/8.4.2/src/_pytest/junitxml.py#L623-L676).

#### Local probe

The bounded fixture confirmed that JUnit did not retain distinct XFAIL/XPASS or
deselection fidelity.

#### Inference against C2

JUnit is the lowest-dependency choice, but it is an interchange format for CI
systems, not a lossless pytest event format. It cannot reconstruct C2's six
outcome categories, exact phase consolidation, retry detection, scope reasons,
or exit reconciliation. A companion plugin would again need to collect the
missing authoritative evidence.

### 5. pytest-json-ctrf 0.5.3

#### Sourced facts

The current PyPI release is 0.5.3, uploaded 2026-07-19. PyPI declares Python
`>=3.8`, a direct `pytest>6.0.0` dependency, MIT, one maintainer, and explicit
xdist support. Its published summary schema has only tests, passed, failed,
pending, skipped, other, start, and stop. See the [official PyPI page](https://pypi.org/project/pytest-json-ctrf/).

The published 0.5.3 source artifact aggregates incoming `TestReport` objects by
node ID, maps report states to passed/failed/skipped/pending/other, derives
duration from setup start to teardown stop, and writes the final JSON directly
at session finish. It has no collection or deselection hooks. The package's
linked default source branch still declares version 0.3.5 while PyPI serves
0.5.3, reducing source-to-release traceability; compare the [linked source metadata](https://github.com/infopulse/pytest-common-test-report-json/blob/master/pyproject.toml#L10-L27)
with [PyPI's 0.5.3 artifact](https://pypi.org/project/pytest-json-ctrf/#files).

#### Inference against C2

CTRF is current and simple, but its vocabulary intentionally normalizes away
pytest-specific XFAIL/XPASS and phase/collection semantics. It accepts xdist
rather than rejecting it, provides no authoritative session exit or scope
proof, and does not preserve every retry attempt. It is not stronger than
`pytest-reportlog` for C2.

### 6. pytest-beacon 0.6.0

#### Sourced facts

`pytest-beacon` is explicitly aimed at machine and agent consumption. It emits
CTRF JSON, YAML, or token-oriented text; reports collection errors, pytest-style
summary counts including deselection/XFAIL/XPASS/reruns, supports xdist, and can
filter stored results or send them over HTTP. PyPI lists 0.6.0, released
2026-05-11, as alpha with Python `>=3.11`. Its runtime requirements are
Pydantic, pydantic-settings, HTTPX, PyYAML, and pytest `>=9.0.0`. See the
[official project page](https://pypi.org/project/pytest-beacon/) and
[0.6.0 package metadata](https://pypi.org/pypi/pytest-beacon/0.6.0/json).

#### Inference against C2

The agent-oriented output is relevant as a presentation comparison, but the
hard pytest requirement directly conflicts with C2's pytest `>=8,<9` contract.
It also embraces xdist and retry summaries where C2 version 1 must reject them,
and its broad configurable output/network surface does not establish effective
arguments, hidden collection reduction, or owner-only atomic evidence. It
cannot be adopted without changing the milestone's trust and compatibility
contract.

### 7. allure-pytest 2.16.0

#### Sourced facts

Allure is the strongest maintained “rich report” alternative found. PyPI lists
2.16.0, released 2026-04-27, as production/stable and Apache-2.0. It depends on
`pytest>=4.5.0` and exactly matching `allure-python-commons==2.16.0`. See the
[official PyPI package](https://pypi.org/project/allure-pytest/).

The pytest integration writes an Allure results directory. A test attempt maps
to its own `{uuid}-result.json`, with optional attachment files. Existing
results are retained by default and new files are appended; Allure uses
multiple files to present retries and history. See the [official pytest guide](https://allurereport.org/docs/pytest/),
[test-result format](https://allurereport.org/docs/how-it-works-test-result-file/),
and [retry model](https://allurereport.org/docs/history-and-retries/).

#### Inference against C2

Allure is excellent when the goal is human test analytics. For C2 it adds a
larger runtime and artifact surface while omitting the single finalized session
record needed to prove completeness, collection scope, and subprocess-exit
agreement. Its append-oriented directory must also be isolated and audited for
stale/multiple-writer files. Adapting it would be more work and less precise
than the standalone plugin.

## Cross-cutting deployment issue

An external reporter must be importable by the **consumer's** pytest process.
Adding it to `pyrepo-check`'s own runtime dependencies does not by itself place
it in the environment created by the current consumer command:

```text
uv run [--frozen] python -m pytest ...
```

uv offers `uv run --with PACKAGE`, but official documentation says those
requirements are layered in a separate ephemeral environment and may conflict
with project requirements. See [`uv run --with`](https://docs.astral.sh/uv/reference/cli/#uv-run)
and [running commands with additional dependencies](https://docs.astral.sh/uv/concepts/projects/run/#requesting-additional-dependencies).

**Inference:** using `--with` would add resolution/cache/network availability
to every pytest run and could layer a reporter's broad pytest requirement over
the consumer's locked pytest. Pinning both reporter and pytest would make
`pyrepo-check`, rather than the project lock, choose the executed pytest. Both
paths weaken the existing “run the consumer's frozen pytest once” boundary.

Requiring every consumer project to install a pyrepo-check-specific reporter
would avoid ephemeral resolution but make C2 unavailable by default. Copying or
vendoring an external plugin's source would remove installation cost but is no
longer meaningfully different from maintaining the approved standalone plugin,
and adds license/update obligations.

## Artifact and failure semantics

| Candidate | When data appears | Completion signal | Atomic? | C2 failure consequence |
| --- | --- | --- | --- | --- |
| reportlog | At session start and after every event | Final `SessionFinish` JSONL record with exit status | No | Missing finish is detectable, but truncation and missing scope data remain |
| json-report | During `pytest_sessionfinish` | Top-level `exitcode` inside final JSON | No | Kill/write error can leave missing or partial JSON; consumer can mutate it first |
| reporter | During session; rendered at `pytest_sessionfinish` | Successful template hook/write only | No | Mutable/private objects and custom hooks prevent authoritative reconciliation |
| JUnit XML | During `pytest_sessionfinish` | Well-formed XML only; no pytest exit code | No | Cannot distinguish a complete XML report from later exit mutation without extra evidence |
| CTRF | During `pytest_sessionfinish` | Well-formed report only; no pytest exit code | No | No authoritative session/cardinality reconciliation |
| beacon | During/after session, depending on selected sink | Report summary; export errors do not fail tests | No C2 atomic contract | Filtering/export policy can omit detail and cannot prove C2 completeness |
| Allure | Per test/fixture throughout execution | No single pytest-session final marker | No; multi-file by design | Directory can be internally valid but session-incomplete |
| Standalone plugin | Temporary file, renamed only after validation-ready finalization | Version, writer/session cardinality, pytest version, and exit status | Yes by contract | Missing/malformed/non-final artifact fails closed with `evidence: null` |

No reviewed package provides C2's atomic publication semantics. This is not a
minor wrapper concern: the milestone promises that agents never receive
fabricated zero counts or a false pass when structured evidence is incomplete.

## Recommendation and decision options

### Recommended decision

Keep the approved no-new-dependency implementation boundary:

1. Ship one Python 3.13-compatible standalone plugin source file.
2. Use documented pytest hooks to record raw collection, deselection,
   phase-report, effective-option, xdist, and session-finish evidence.
3. Own the artifact schema and atomic write/finalization protocol.
4. Validate the artifact outside the consumer process before constructing
   `PytestResult`.
5. Use upstream reporters as comparison fixtures, not as the trusted evidence
   producer.

`pytest-reportlog` is the best reference for event serialization and
controller-side xdist behavior. `pytest-json-report` is the best comparison for
the desired public summary shape. JUnit XML is a useful regression oracle for
basic counts only.

### Alternative if product requirements change

Choose `pytest-reportlog` only if all of these changes are explicitly accepted:

- accept a companion plugin for scope/deselection and atomic finalization;
- define how the package enters the consumer environment without replacing its
  locked pytest;
- pin and test its serialized event shape across every supported pytest 8
  release; and
- retain C2's own exit/cardinality validation.

The current Python `>=3.13.15` floor already satisfies reportlog. That does not
make the hybrid smaller: the companion remains responsible for effective args,
semantic options, deselection, collection reduction, retry/xdist rejection,
writer/session cardinality, and atomic finalization. The alternative has no
clear advantage once that required code and the dependency's serialization
trust boundary are counted.

## Reproduction appendix

The bounded probe ran on Darwin 25.5.0 arm64 with CPython 3.13.15. It used a
fresh virtual environment and these exact package versions:

```text
pytest==8.4.2
pytest-reportlog==1.0.0
pytest-json-report==1.5.0
pytest-metadata==3.1.1
pytest-xdist==3.8.0
pytest-rerunfailures==16.6
```

Environment creation:

```sh
uvx --from uv uv venv --python 3.13.15 .venv
uvx --from uv uv pip install --python .venv/bin/python \
  pytest==8.4.2 pytest-reportlog==1.0.0 \
  pytest-json-report==1.5.0 pytest-xdist==3.8.0 \
  pytest-rerunfailures==16.6
```

Exact fixture contents:

```python
# tests/test_outcomes.py
import pytest


def test_pass() -> None:
    assert True


def test_fail() -> None:
    assert False


@pytest.mark.skip(reason="probe skip")
def test_skip() -> None:
    raise AssertionError("must not run")


@pytest.mark.xfail(reason="probe xfail")
def test_xfail() -> None:
    assert False


@pytest.mark.xfail(reason="probe xpass")
def test_xpass() -> None:
    assert True


@pytest.fixture
def broken_fixture() -> None:
    raise RuntimeError("probe setup error")


def test_setup_error(broken_fixture: None) -> None:
    assert broken_fixture is None


def test_deselected() -> None:
    assert True
```

```python
# tests/test_retry.py
import pytest

attempts = 0

@pytest.mark.flaky(reruns=1)
def test_retry_then_pass() -> None:
    global attempts
    attempts += 1
    assert attempts == 2
```

```python
# tests/test_collection_error.py
raise RuntimeError("probe collection error")
```

Commands:

```sh
.venv/bin/python -m pytest tests/test_outcomes.py \
  -k 'not deselected' \
  --report-log=artifacts/outcomes.reportlog.jsonl \
  --json-report --json-report-file=artifacts/outcomes.json -q

.venv/bin/python -m pytest tests/test_collection_error.py \
  --report-log=artifacts/collection.reportlog.jsonl \
  --json-report --json-report-file=artifacts/collection.json -q

.venv/bin/python -m pytest tests/test_retry.py \
  --report-log=artifacts/retry.reportlog.jsonl \
  --json-report --json-report-file=artifacts/retry.json -q

.venv/bin/python -m pytest tests/test_outcomes.py \
  -k 'not deselected' -n 2 \
  --report-log=artifacts/xdist.reportlog.jsonl \
  --json-report --json-report-file=artifacts/xdist.json -q
```

Retained observations:

- The single-process outcome command exited 1. JSON report summary was
  `passed=1`, `failed=1`, `skipped=1`, `xfailed=1`, `xpassed=1`, `error=1`,
  `total=6`, `collected=7`, `deselected=1`.
- The collection command exited 2 and both reporters recorded a failed
  collector/collection event.
- The retry command exited 0. Reportlog retained five test reports:
  `setup:passed`, `call:rerun`, `setup:passed`, `call:passed`, and
  `teardown:passed`. JSON report retained one test with final passed phases;
  its summary said `rerun=1`, but the discarded failed call was unavailable.
- The xdist command exited 1. Reportlog contained 16 test reports and zero
  collection reports. JSON report contained zero collectors and reported
  `collected=6` with no deselected count, unlike the same selector's
  single-process `collected=7`, `deselected=1` evidence.

Artifact queries used `jq` against the two saved formats, including:

```sh
jq -s '[.[] | select(.["$report_type"] == "TestReport") |
  {nodeid, when, outcome}]' artifacts/retry.reportlog.jsonl
jq -s '[.[] | select(.["$report_type"] == "CollectReport")] | length' \
  artifacts/xdist.reportlog.jsonl
jq '{summary: .summary, collectors: (.collectors | length)}' \
  artifacts/xdist.json
```

These observations are comparison evidence only. They are not an upstream
compatibility guarantee and do not replace C2's own contract tests.

## Remaining unknowns before implementation

These are implementation-test items, not reasons to defer the dependency
decision:

- exact raw expected-failure representations across pytest 8.0 through 8.4 and
  Python 3.13.15 and later supported interpreters;
- behavior of third-party plugins that replace the runtest protocol and hide
  all intermediate attempts (already outside the version-1 claim);
- ordering when another `pytest_sessionfinish` hook changes `session.exitstatus`;
- filesystem and signal fault injection around temporary write, flush, fsync,
  rename, and cleanup; and
- whether any non-xdist third-party runner exposes worker metadata early enough
  for proactive rejection. C2 should continue to reject observable violations
  without claiming universal detection.

None of those unknowns is solved by the reviewed dependencies.
