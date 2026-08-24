from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import argparse
import subprocess  # nosec B404
import sys
from typing import cast

from pyrepo_check.config import (
    InvalidTestShortcutError,
    collect_existing_positionals,
    load_project_config,
)
from pyrepo_check.execution import ExecutionResult, ProcessRunner, execute_plan
from pyrepo_check.planning import (
    OutputFormat,
    PlanningErrorCode,
    PlanningFacts,
    PlanningFailure,
    RunRequest,
    plan_run,
)
from pyrepo_check.reporting import (
    build_planning_error_report,
    build_run_report,
    render_terminal,
    select_exit_code,
    serialize_json,
    validate_report_v1,
)


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
        "--format",
        choices=("terminal", "json"),
        default="terminal",
        help="Output terminal diagnostics or one JSON document.",
    )
    parser.add_argument(
        "--shortcut",
        metavar="NAME",
        help="Run a configured Test Shortcut in a pytest-only focused run.",
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
    output_format = cast(OutputFormat, args.format)
    request = RunRequest(
        root=Path(args.root),
        positionals=tuple(args.checks),
        all_selected=args.all,
        no_frozen=args.no_frozen,
        output_format=output_format,
        test_shortcut=args.shortcut,
    )

    try:
        config = load_project_config(request.root, no_frozen=request.no_frozen)
    except InvalidTestShortcutError as error:
        return _write_planning_error(
            "invalid_test_shortcut",
            str(error),
            hint="Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.",
            output_format=output_format,
        )
    except ValueError as error:
        return _write_planning_error(
            "invalid_project_config",
            str(error),
            hint=None,
            output_format=output_format,
        )
    except Exception as error:
        return _write_planning_error(
            "internal_planning_error",
            str(error),
            hint=None,
            output_format=output_format,
        )

    try:
        facts = PlanningFacts(
            existing_positionals=collect_existing_positionals(
                config.root,
                request.positionals,
            )
        )
        plan = plan_run(request, config, facts)
    except PlanningFailure as error:
        return _write_planning_error(
            error.code,
            str(error),
            hint=error.hint,
            output_format=output_format,
        )
    except Exception as error:
        return _write_planning_error(
            "internal_planning_error",
            str(error),
            hint=None,
            output_format=output_format,
        )

    execution = execute_plan(plan, runner=runner)
    try:
        report = build_run_report(config.root, plan, execution)
        validate_report_v1(report)
        rendered = (
            serialize_json(report)
            if output_format == "json"
            else render_terminal(report)
        )
        exit_code = select_exit_code(report)
    except Exception as error:
        _write_reporting_fallback(error)
        return _fallback_exit_code(execution)

    if output_format == "json":
        sys.stdout.buffer.write(cast(bytes, rendered))
    else:
        sys.stdout.write(cast(str, rendered))
    return exit_code


def _write_planning_error(
    code: PlanningErrorCode,
    message: str,
    *,
    hint: str | None,
    output_format: OutputFormat,
) -> int:
    try:
        report = build_planning_error_report(code, message, hint=hint)
        validate_report_v1(report)
        rendered = (
            serialize_json(report)
            if output_format == "json"
            else render_terminal(report)
        )
        exit_code = select_exit_code(report)
    except Exception as error:
        _write_reporting_fallback(error)
        return 2

    if output_format == "json":
        sys.stdout.buffer.write(cast(bytes, rendered))
    else:
        sys.stderr.write(cast(str, rendered))
    return exit_code


def _write_reporting_fallback(error: Exception) -> None:
    print(f"pyrepo-check: internal reporting error: {error}", file=sys.stderr)


def _fallback_exit_code(execution: ExecutionResult) -> int:
    first_positive = next(
        (
            check.returncode
            for check in execution.checks
            if check.returncode is not None and check.returncode > 0
        ),
        None,
    )
    return first_positive if first_positive is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
