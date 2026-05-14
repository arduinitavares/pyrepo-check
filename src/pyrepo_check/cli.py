from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import argparse
import subprocess
import sys

from pyrepo_check.config import load_project_config
from pyrepo_check.runner import build_checks, run_checks, select_checks


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
        help="Optional subset of checks: ruff, ty, bandit, pytest.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, runner=subprocess.run) -> int:
    args = parse_args(argv)
    try:
        config = load_project_config(Path(args.root), no_frozen=args.no_frozen)
        available_checks = build_checks(config)
        selected = select_checks(
            available_checks,
            requested=tuple(args.checks),
            all_selected=args.all,
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    return run_checks(selected, cwd=config.root, runner=runner)


if __name__ == "__main__":
    raise SystemExit(main())
