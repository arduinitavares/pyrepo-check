from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import argparse
import subprocess  # nosec B404
import sys

from pyrepo_check.config import collect_existing_positionals, load_project_config
from pyrepo_check.execution import ProcessRunner, execute_plan
from pyrepo_check.planning import PlanningFacts, RunRequest, plan_run


CHECK_HELP = "ruff, annotations, annotations-fix, ty, bandit, pytest"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Python repository quality checks.")
    parser.add_argument("--all", action="store_true", help="Run all checks.")
    parser.add_argument(
        "--root",
        default=".",
        help="Project root to check. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--no-frozen",
        action="store_true",
        help="Run uv without --frozen even when uv.lock exists.",
    )
    parser.add_argument(
        "checks",
        nargs="*",
        help=f"Optional check names and target paths. Checks: {CHECK_HELP}.",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: ProcessRunner = subprocess.run,
) -> int:
    args = parse_args(argv)
    request = RunRequest(
        root=Path(args.root),
        positionals=tuple(args.checks),
        all_selected=args.all,
        no_frozen=args.no_frozen,
    )

    try:
        config = load_project_config(request.root, no_frozen=request.no_frozen)
        facts = PlanningFacts(
            existing_positionals=collect_existing_positionals(
                config.root,
                request.positionals,
            )
        )
        plan = plan_run(request, config, facts)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    result = execute_plan(plan, runner=runner)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
