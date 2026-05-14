# Agent Prompt: pyrepo-check Cleanup Workflow

Use this prompt when asking an agent to clean up a repository that uses the
global `pyrepo-check` quality gate.

```text
I want you to strategically refactor this repository until `pyrepo-check --all`
passes cleanly.

Goal:
Make the repo healthy under the global `pyrepo-check` quality gate without
changing intended behavior.

Start by inspecting the repository:
- `git status --short`
- project layout
- `pyproject.toml`
- existing tests
- current `pyrepo-check --all` output

Important pyrepo-check behavior:
- `pyrepo-check --all` is the strict aggregate gate.
- No-argument `pyrepo-check` behaves the same as `pyrepo-check --all`.
- The aggregate gate runs all selected checks and reports diagnostics from every
  selected tool before returning a non-zero exit code.
- Without explicit target paths, `--all` runs Ruff, annotation reporting, and
  Bandit against the repository root (`.`).
- Focused checks may use project-configured targets.

Core commands:
- Full strict repo gate:
  `pyrepo-check --all`
- Focused checks by class:
  `pyrepo-check ruff`
  `pyrepo-check annotations`
  `pyrepo-check ty`
  `pyrepo-check bandit`
  `pyrepo-check pytest`
- File-oriented checks for one file:
  `pyrepo-check path/to/file.py`
- Full gate against one file, including pytest:
  `pyrepo-check --all path/to/file.py`
- Focused annotation report for one file:
  `pyrepo-check annotations path/to/file.py`
- Mechanical annotation fixer:
  `pyrepo-check annotations-fix path/to/file.py`

Work strategically:
- Do not blindly rewrite the repo.
- Use `pyrepo-check --all` to see the full failure set.
- Then fix failures by class, usually in this order:
  Ruff/lint correctness -> annotations -> ty -> Bandit -> pytest.
- Prefer small, coherent changes over broad churn.
- Preserve public APIs and intended behavior unless a test clearly proves the
  behavior is broken.

Annotation workflow:
- `pyrepo-check annotations` reports Ruff `ANN` issues explicitly.
- `pyrepo-check annotations-fix` applies Ruff's mechanical annotation fixes.
- After running `annotations-fix`, inspect the diff before continuing.
- Do not rely on `ty` to report missing annotations; `ty` checks type
  consistency.

Safety rules:
- Do not revert unrelated user changes.
- Do not delete files without confirming they are obsolete.
- Do not change dependency versions unless required; explain why if you do.
- Do not weaken `pyrepo-check`, Ruff, ty, Bandit, or pytest config just to pass.
- Fix lint issues by improving code, not by suppressing rules.
- Use narrow ignores only when there is a clear documented reason.
- Fix Bandit findings with safer code, not blanket skips.
- Make tests deterministic and meaningful.
- If behavior must change, add or update tests that capture the intended
  behavior.

Version sanity:
- If editor diagnostics differ from terminal diagnostics, check tool versions.
- For ty specifically, compare:
  `uv run --frozen python -m ty --version`
  against the editor/bundled ty version.
- Do not upgrade dependencies just to silence disagreement; upgrade only when the
  repo should intentionally adopt the newer tool behavior.

Verification:
- After each meaningful batch, rerun the relevant focused check.
- At the end, run:
  `pyrepo-check --all`

Deliverable:
Finish with:
- summary of what changed
- checks run and results
- any residual risks or follow-up recommendations

If the full cleanup is too large for one pass, create a prioritized checklist
and complete the highest-impact phase first, but keep working until at least one
full class of failures is eliminated.
```
