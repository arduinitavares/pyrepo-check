from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import argparse
import subprocess  # nosec B404
import sys

from pyrepo_check.config import load_project_config
from pyrepo_check.runner import SELECTABLE_CHECK_ORDER, build_checks, run_checks, select_checks


TARGET_DEFAULT_CHECKS = ("ruff", "annotations", "ty", "bandit")
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


def main(argv: Sequence[str] | None = None, *, runner=subprocess.run) -> int:
    args = parse_args(argv)
    try:
        config = load_project_config(Path(args.root), no_frozen=args.no_frozen)
        requested_checks, targets = _split_checks_and_targets(
            args.checks,
            root=config.root,
            all_selected=args.all,
        )
        if targets and not requested_checks and not args.all:
            requested_checks = TARGET_DEFAULT_CHECKS
        strict_all = not targets and (args.all or not args.checks)
        available_checks = build_checks(config, targets=targets, strict_all=strict_all)
        selected = select_checks(
            available_checks,
            requested=requested_checks,
            all_selected=args.all,
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    return run_checks(selected, cwd=config.root, runner=runner)


def _split_checks_and_targets(
    arguments: Sequence[str],
    *,
    root: Path,
    all_selected: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    check_names = set(SELECTABLE_CHECK_ORDER)
    requested_checks = tuple(argument for argument in arguments if argument in check_names)
    targets = tuple(argument for argument in arguments if argument not in check_names)

    if targets and not requested_checks and not all_selected:
        missing_targets = tuple(target for target in targets if not _target_exists(root, target))
        if missing_targets:
            names = ", ".join(sorted(missing_targets))
            raise ValueError(f"Unknown check(s): {names}")

    return requested_checks, targets


def _target_exists(root: Path, target: str) -> bool:
    path = Path(target)
    return path.exists() if path.is_absolute() else (root / path).exists()


if __name__ == "__main__":
    raise SystemExit(main())
