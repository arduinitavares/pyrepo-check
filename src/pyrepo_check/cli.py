from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import argparse
import sys
from typing import cast

from pyrepo_check.config import (
    InvalidCoverageConfigError,
    InvalidTestShortcutError,
    collect_existing_positionals,
    load_project_config,
)
from pyrepo_check.execution import (
    ProcessRunner,
    ToolEnvironmentObservation,
    execute_plan,
    observe_tool_environment,
)
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
    validate_report_v2,
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
        help="Recognized for compatibility; repository-safe execution rejects it.",
    )
    parser.add_argument(
        "--python",
        metavar="REQUEST",
        help="Request a Repository Python from 3.10 through 3.13.",
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
        "--coverage",
        action="store_true",
        help="Plan Coverage.py collection for the selected pytest run.",
    )
    parser.add_argument(
        "checks",
        nargs="*",
        help=(
            "Optional check names and existing project-relative target paths. "
            f"Checks: {CHECK_HELP}. annotations-fix must be selected alone."
        ),
    )
    return parser.parse_intermixed_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: ProcessRunner | None = None,
) -> int:
    args = parse_args(argv)
    tool_environment = observe_tool_environment()
    output_format = cast(OutputFormat, args.format)
    request = RunRequest(
        root=Path(args.root),
        positionals=tuple(args.checks),
        all_selected=args.all,
        no_frozen=args.no_frozen,
        output_format=output_format,
        test_shortcut=args.shortcut,
        coverage_requested=args.coverage,
        repository_python=args.python,
    )

    try:
        config = load_project_config(request.root)
    except InvalidCoverageConfigError as error:
        return _write_planning_error(
            "invalid_project_config",
            str(error),
            hint="Fix native [tool.coverage] settings in pyproject.toml.",
            output_format=output_format,
            tool_environment=tool_environment,
        )
    except InvalidTestShortcutError as error:
        return _write_planning_error(
            "invalid_test_shortcut",
            str(error),
            hint="Fix [tool.pyrepo-check.test-shortcuts] in pyproject.toml.",
            output_format=output_format,
            tool_environment=tool_environment,
        )
    except ValueError as error:
        return _write_planning_error(
            "invalid_project_config",
            str(error),
            hint=None,
            output_format=output_format,
            tool_environment=tool_environment,
        )
    except Exception as error:
        return _write_planning_error(
            "internal_planning_error",
            str(error),
            hint=None,
            output_format=output_format,
            tool_environment=tool_environment,
        )

    try:
        facts = PlanningFacts(
            existing_positionals=collect_existing_positionals(
                config.root,
                request.positionals,
            ),
            pyproject_exists=config.pyproject_sha256 is not None,
        )
        plan = plan_run(request, config, facts)
    except PlanningFailure as error:
        return _write_planning_error(
            error.code,
            str(error),
            hint=error.hint,
            output_format=output_format,
            tool_environment=tool_environment,
        )
    except Exception as error:
        return _write_planning_error(
            "internal_planning_error",
            str(error),
            hint=None,
            output_format=output_format,
            tool_environment=tool_environment,
        )

    terminal_progress_emitted = False
    terminal_progress_error: Exception | None = None

    def write_terminal_progress(text: str) -> None:
        nonlocal terminal_progress_emitted, terminal_progress_error
        if terminal_progress_error is not None:
            return
        try:
            sys.stdout.write(text)
            terminal_progress_emitted = True
            sys.stdout.flush()
        except Exception as error:
            terminal_progress_error = error

    execution = execute_plan(
        plan,
        tool_environment=tool_environment,
        runner=runner,
        terminal_writer=write_terminal_progress if output_format == "terminal" else None,
    )
    if terminal_progress_error is not None:
        _write_reporting_fallback(terminal_progress_error)
        return 2
    try:
        report = build_run_report(config.root, plan, execution)
        validate_report_v2(report)
        rendered = (
            serialize_json(report)
            if output_format == "json"
            else render_terminal(
                report,
                include_environment=not terminal_progress_emitted,
            )
        )
        exit_code = select_exit_code(report)
    except Exception as error:
        _write_reporting_fallback(error)
        return 2

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
    tool_environment: ToolEnvironmentObservation,
) -> int:
    try:
        report = build_planning_error_report(
            code,
            message,
            hint=hint,
            tool_environment=tool_environment,
        )
        validate_report_v2(report)
        rendered = serialize_json(report) if output_format == "json" else render_terminal(report)
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


if __name__ == "__main__":
    raise SystemExit(main())
