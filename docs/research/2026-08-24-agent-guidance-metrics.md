# Agent-guidance metrics research

## Decision

Keep `pyrepo-check` a local/CI quality-report gate. It should tell an agent
where tests are thin, where the test suite is becoming costly or masked, and
whether a dependency has a known vulnerability. It must not acquire staging,
approval, deployment, or rollback responsibilities.

Deliver in two broad stages: first introduce a behavior-preserving
report/config architecture around the existing checks and preserve
`pytest <file-or-node>`; then add the selected metrics. The implementation
design divides those stages into smaller verified phases. Historical
percentages are baselines only, never proof of the current change.

## Current boundary

The current wrapper has no report model or persisted baseline. It dispatches
Ruff, annotation reporting/fixing, `ty`, Bandit, and pytest; `ty` invokes only
`ty check`, and explicit pytest paths/nodes are passed through unchanged. That
is the compatibility contract for this work.

## Candidate screen

| Candidate | Agent value | Determinism / gaming / cost | Decision |
| --- | --- | --- | --- |
| Line + branch coverage; missing lines/branches | Directly points to unexercised behavior and decisions. | Deterministic for a fixed environment; tests can game a percentage; one full test run. | **Select.** Report gaps, not a score alone. |
| Changed-code coverage | Excellent review-local signal. | Needs an unambiguous merge base and coverage-to-diff mapping; can miss multi-line statements. | Defer; add only after the coverage report contract is stable. |
| Slow tests | Lets agents avoid needlessly broad feedback and prioritize isolation. | Run-to-run noise; low incremental cost once pytest runs. | Diagnostic for test health. |
| Skipped / xfail / xpass / flaky tests | Exposes masked confidence and unstable evidence. | Counts are deterministic; raw count is gameable without required reasons/ownership. | Diagnostic for test health; flaky needs an explicit plugin or repeated-run policy. |
| Suppressions / waivers | Makes exceptions to Ruff, Bandit, and test policy visible. | Simple count is highly gameable and not a quality score. | Report an attributable ledger, never gate on count alone. |
| Dependency vulnerability findings | Catches a security class not covered by source scanning. | Advisory and network/resolution state can change; moderate scheduled cost. | **Select**, but keep the result as findings, not a vanity score. |
| Cyclomatic complexity | Sometimes identifies code worth reading. | Thresholds are language/domain dependent; encourages splitting instead of simplifying behavior. | Reject for v1; no agent action without corroborating tests or incidents. |
| Mutation score | Strong test-quality probe. | Expensive and sensitive to equivalent mutants/test stability. | Scheduled deep analysis only, not a gate or routine agent loop. |

## Primary metric 1 — executed-behavior gap

**Definition.** Run the selected pytest scope under coverage.py with branch
measurement. Per measured production file, report `missing_lines` (executable
statements with zero hits) and `missing_branches` / partial branches; rank by
uncovered executable opportunities, not by a single percentage. Coverage.py
supports branch measurement, reports missing statements and partial branches,
and emits JSON; its report commands can enforce `fail-under` when configured
([branch measurement](https://coverage.readthedocs.io/en/latest/branch.html),
[reporting](https://coverage.readthedocs.io/en/latest/commands/cmd_report.html),
[JSON reporting](https://coverage.readthedocs.io/en/latest/commands/cmd_json.html)).

**Agent action.** If changed or risk-relevant production code has missing
lines/branches, add or strengthen a behavior test before refactoring nearby.
If the gaps are in untouched code, record them as context rather than expanding
the task. A high aggregate number never overrules a missing branch in the
changed decision path.

**Drivers.** (1) exact missing line ranges, (2) exact partial/missing branch
arcs. These are the actionable payload; aggregate line and branch percentages
are only headers.

**Cadence and cost.** Fast focused: run only when a direct pytest file/node or
Test Shortcut is selected, and label scope as partial. Strict aggregate: one full
suite run and JSON report, generally similar to pytest plus tracing overhead.
Scheduled: none required.

**Inform vs gate.** Always **inform**. A project may opt into an aggregate
`fail_under` threshold as a strict gate; no global default threshold. Do not
gate focused runs, changed files alone, generated code, or files intentionally
excluded by an explicit coverage configuration.

**Guardrails.** Require a project-owned source/include/exclude policy; keep
test and generated paths out of the production denominator; preserve the raw
Coverage.py JSON as the authoritative input to the Agent Report until parsing
and validation finish; do not compare to an old percentage without rerunning
the same scope/configuration. Adding `coverage` is justified: it supplies both
the measurement and structured driver data; do not add `pytest-cov` merely as
a wrapper.

## Primary metric 2 — test-evidence health

**Definition.** For each test invocation, report: (a) the slowest N completed
test node IDs and durations, and (b) counts plus node IDs/reasons for skip,
xfail, and xpass. Treat a flaky signal as `tests that fail on at least one of
N identical scheduled repetitions`, retaining seed/environment/attempt data;
do not call a single retry a pass. pytest supports node-ID selection and
selection by registered custom markers ([invocation and selection](https://docs.pytest.org/en/stable/how-to/usage.html),
[custom markers](https://docs.pytest.org/en/stable/how-to/mark.html)); it also
records skip/xfail outcomes and reasons ([skip/xfail](https://docs.pytest.org/en/stable/how-to/skipping.html)).

**Agent action.** Preserve a direct file/node target for the smallest loop.
Use a Test Shortcut only when it is a project-declared behavioral slice (for
example `-m unit` or `-m integration`), not a replacement for the node ID.
Investigate new/changed skip, xfail, xpass, or flaky results before treating a
green run as sufficient; use slow-test rankings to choose the narrowest useful
feedback scope.

**Drivers.** (1) per-node elapsed duration, (2) exception outcome with its
declared reason, plus repetition evidence for flakiness. Pytest's `--durations`
option reports the N slowest tests ([reference](https://docs.pytest.org/en/stable/reference/reference.html)).

**Cadence and cost.** Fast focused: direct node/file or a Test Shortcut, with
durations and outcome summary. Strict aggregate: full suite, report top N and
all exception outcomes. Scheduled deep: a small fixed number of repeated full
suite runs in a controlled environment for flaky classification; that cost is
intentionally outside ordinary CI.

**Inform vs gate.** Inform by default. Gate an unexpected xpass and a project
policy violation such as a new unreasoned skip/xfail; do not gate merely because
a test is slow or because an existing, reviewed exception exists.

**Guardrails.** Require Test Shortcuts to be explicitly registered/configured;
reject unknown Test Shortcut names; emit the exact expanded pytest arguments
and collected count. Require non-empty reason plus owner/expiry metadata for new
exception markers/waivers if the project adopts such policy. Never hide a test
failure by automatically rerunning it; a rerun plugin is optional and its
"passed after retry" state must remain visible. The first Agent Report schema
deliberately rejects repeated outcomes because it cannot yet represent attempts
without losing that history; attempt-aware flaky evidence remains a scheduled
metric rather than a false green result.

## Primary metric 3 — known dependency-vulnerability findings

**Definition.** For the locked dependency graph, report one finding per
`(package, installed version, advisory ID)`, with aliases and available fixed
versions where supplied. Do not compress this into an invented security score.
`pip-audit` can audit a local project/lock files, emit JSON, and returns a
non-zero exit when known vulnerabilities are found
([official repository and CLI](https://github.com/pypa/pip-audit)).

**Agent action.** A finding in a directly used or reachable dependency is a
caution flag: identify the dependency path, fix version, compatibility and test
scope before upgrading. A finding with no fix is an escalation/acceptance item,
not permission to suppress silently.

**Drivers.** (1) advisory ID/package/version/fixed versions, (2) dependency
path and an explicit, version-controlled waiver when accepted.

**Cadence and cost.** Fast focused: do not run. Strict aggregate: optional only
when the lock and advisory source are available; it can require dependency
resolution and network access. Scheduled: preferred daily/weekly or release
candidate audit, retaining the JSON artifact. The tool warns that its runtime
can approach dependency installation when it must resolve dependencies
([security model and performance note](https://github.com/pypa/pip-audit)).

**Inform vs gate.** Inform in v1. A project may later gate newly introduced,
unwaived findings only after defining advisory-source availability and waiver
ownership/expiry. Never gate on a missing network response, an incomplete
dependency collection, or a historical audit artifact.

**Guardrails.** Keep the audit separate from Bandit: it audits dependency trees,
not application code. Record the lockfile hash, audit source/time, failures to
collect dependencies, and the full JSON. A waiver must name the advisory,
package/version, rationale, owner, expiry, and review link.

## Deferred diagnostic work

**Changed-code coverage.** `diff-cover` calculates coverage for new/modified
lines from a coverage report and Git diff and can emit JSON/Markdown
([first-party README](https://github.com/Bachmann1234/diff_cover)). It is a
good phase-2 review aid, but is not v1: require an explicit comparison base,
clean rules for untracked/staged work, and a policy for multi-line statements
before considering a new dependency or gate.

**Suppression/waiver ledger.** Phase 1 should normalize existing exceptions
into records: tool, rule/advisory/marker, path/node, reason, owner, expiry, and
source location. Show additions/removals and expired entries. Do not reward a
falling count: deleting a suppression without resolving the underlying issue is
not improvement. This is a cross-cutting diagnostic, not a fourth score.

**Cyclomatic complexity.** Do not add a complexity tool in v1. It overlaps
Ruff's maintainability-oriented feedback poorly, adds a threshold fight, and
does not say what behavior to test. Reconsider only if post-v1 coverage gaps
and incidents repeatedly cluster in the same functions.

**Mutation score.** Schedule it only for stable, high-value modules after
coverage exists. Mutmut supports incremental runs and optional filtering to
covered lines, but its own documentation frames the work as a separate mutation
run and result-browsing workflow ([official README](https://github.com/boxed/mutmut)).
Never use it as a PR/focused gate; equivalent mutants, execution cost, and test
instability make the scalar score misleading.

## Recommended metric set

1. **Executed-behavior gap:** line + branch coverage with exact missing lines
   and partial branches; guidance always, opt-in aggregate threshold only.
2. **Test-evidence health:** slow-node durations and skip/xfail/xpass evidence;
   preserve file/node targets and add project-configured Test Shortcuts.
3. **Known dependency-vulnerability findings:** scheduled JSON findings with
   explicit waiver metadata; inform first, policy gate later.

The approved first implementation contains items 1 and 2 plus the Agent Report.
Item 3 remains a separate scheduled follow-up.

## Follow-up research questions

1. What configuration schema and expiry enforcement will make waivers durable
   without forcing a central service?
2. Which CI contexts have a trustworthy merge base and network access, and when
   should a dependency audit be allowed to gate newly introduced findings?
3. What bounded repeated-run budget is acceptable for flaky classification, and
   which environment facts must be captured to make it reproducible?
