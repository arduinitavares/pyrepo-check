# Agent Prompt: pyrepo-check Repository Workflow

Use this prompt when asking an agent to improve a uv-managed Python repository with
the globally installed `pyrepo-check` controller.

```text
Improve this repository until its pyrepo-check quality gate passes without weakening
intended behavior or quality policy.

Start with read-only evidence:
- `git status --short`
- project layout and `pyproject.toml`
- `uv.lock` presence/currentness
- `pyrepo-check --help`
- `pyrepo-check --format json --all`

Environment contract:
- The globally installed pyrepo-check and its Python 3.13.15+ runtime are only the
  Tool Environment/controller.
- The target must be a uv project with a present, current `uv.lock`.
- One normal invocation uses one uv-selected Repository Python. Use `--python` only
  when the user or CI selects one version, for example:
  `pyrepo-check --python 3.12 --all`
- A Python matrix is multiple invocations, not one pyrepo-check run.
- Every executable check runs in the Repository Environment. pyrepo-check itself is
  not injected there.
- Ruff and Ty keep the Analysis Python semantics configured by the repository. Do
  not replace them with the controller or Repository Python version.
- uv may reconstruct or synchronize a safe ignored, untracked `.venv` from the
  current lock. pyrepo-check must not change `pyproject.toml`, `uv.lock`, or tracked
  source during a normal run.

Dependency contract:
- uv's default dependency selection must contain compatible repository-owned Ruff,
  Ty, Bandit, pytest, and requested Coverage.py.
- If a dependency is missing, incompatible, shadowed, or unusable, fix the target
  repository configuration and lock only with user authority. Never install or
  inject a package merely to make the report green.
- `--no-frozen` is intentionally rejected as `unsafe_unlocked_execution`. Update the
  lock explicitly with user authority, then rerun without the flag.
- A check-local dependency error does not suppress independent checks. Inspect every
  check status and its evidence.

Editing workflow:
1. Use the smallest focused command for the current change.
2. Fix one coherent failure class at a time.
3. Preserve public APIs and behavior unless tests establish the intended change.
4. Never weaken Ruff, annotation, Ty, Bandit, pytest, or Coverage configuration just
   to pass.
5. Run the strict target-free gate before completion.

Canonical commands:
- Strict repository-native gate:
  `pyrepo-check --all`
- One explicit Repository Python:
  `pyrepo-check --python 3.12 --all`
- Focused typing:
  `pyrepo-check --python 3.12 annotations ty src/`
- Complete agent evidence:
  `pyrepo-check --python 3.12 --format json --all`
- One test:
  `pyrepo-check pytest tests/test_file.py::test_name`
- Mechanical annotation fixes, only when source mutation is authorized:
  `pyrepo-check annotations-fix src/`

JSON interpretation:
- Require `schema_version == 2` for this release.
- Inspect `tool_environment` and `repository_environment`, including exact Python,
  lock status, mutation protection, dependencies, processes, and environment error.
- For each check inspect `execution_environment`, `analysis_python_authority`,
  `start_evidence`, processes, status, and error.
- Inspect nested pytest and Coverage results when selected.
- Coverage with `scope="partial"`, `status="guidance"`, and
  `gate_eligible=false` is useful guidance, not a complete threshold gate.

Deliverable:
- changes made and why
- exact focused commands and results
- exact final `pyrepo-check --all` result
- Tool and Repository Python evidence
- lock/dependency/mutation evidence
- pytest/Coverage completeness and eligibility
- residual risks or unrun gates
```

The installed CLI's `pyrepo-check --help` is the syntax source of truth for that
installed version. For schema version 2, use
[`docs/reference/agent-report-schema-v2.md`](docs/reference/agent-report-schema-v2.md).
