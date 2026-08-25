from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess  # nosec B404
from typing import cast

import pytest

from pyrepo_check.execution import CapturedBytes, ExecutedProcess, execute_plan
from pyrepo_check.planning import (
    CoverageExecutionPlan,
    PlannedCheck,
    PytestExecutionPlan,
    RunPlan,
)
from pyrepo_check import coverage_execution


def _process(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int | None = 0,
    spawn_error: str | None = None,
) -> ExecutedProcess:
    return ExecutedProcess(
        role="coverage_preflight",
        command=("consumer-python", "-c", "probe"),
        cwd=Path("."),
        returncode=returncode,
        duration_ms=0,
        stdout=CapturedBytes(stdout, 0),
        stderr=CapturedBytes(stderr, 0),
        spawn_error=spawn_error,
    )


def _document(
    *,
    python_version: object = [3, 13, 15],
    coverage_available: object = True,
    coverage_version: object = "7.15.2",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "python_version": python_version,
            "coverage_available": coverage_available,
            "coverage_version": coverage_version,
        },
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize(
    ("stdout", "returncode", "spawn_error", "classification", "version"),
    [
        (_document(python_version=[3, 13, 14], coverage_available=False, coverage_version=None), 0, None, "unsupported_python", None),
        (_document(coverage_available=False, coverage_version=None), 0, None, "module_unavailable", None),
        (_document(coverage_version="7.15"), 0, None, "supported", "7.15"),
        (_document(coverage_version="7.15.2"), 0, None, "supported", "7.15.2"),
        (_document(coverage_version="7.99.0"), 0, None, "supported", "7.99.0"),
        (_document(coverage_version="7.14.9"), 0, None, "unsupported_version", "7.14.9"),
        (_document(coverage_version="8.0.0"), 0, None, "unsupported_version", "8.0.0"),
        (_document(coverage_version="7.15rc1"), 0, None, "unsupported_version", None),
        (_document(coverage_version="7.15.dev0"), 0, None, "unsupported_version", None),
        (_document(coverage_version="7.15.post1"), 0, None, "unsupported_version", None),
        (_document(coverage_version="7.15+local"), 0, None, "unsupported_version", None),
        (_document() + b"\nextra", 0, None, "preflight_invalid", None),
        (b"not-json", 0, None, "preflight_invalid", None),
        (b"\xff", 0, None, "preflight_invalid", None),
        (_document(), 1, None, "preflight_invalid", None),
        (_document(), -9, None, "terminated_by_signal", None),
        (b"", None, "FileNotFoundError: consumer-python", "spawn_failed", None),
    ],
    ids=(
        "unsupported-python",
        "missing-coverage",
        "minimum-stable",
        "stable-patch",
        "stable-high-minor",
        "too-old",
        "too-new",
        "prerelease",
        "dev",
        "post",
        "local",
        "extra-output",
        "malformed-json",
        "invalid-utf8",
        "nonzero",
        "signal",
        "spawn-failure",
    ),
)
def test_coverage_preflight_classifies_the_supported_consumer_contract(
    stdout: bytes,
    returncode: int | None,
    spawn_error: str | None,
    classification: str,
    version: str | None,
) -> None:
    observation = coverage_execution.classify_coverage_preflight(
        _process(stdout=stdout, returncode=returncode, spawn_error=spawn_error)
    )

    assert observation.classification == classification
    assert observation.record is not None or classification in {
        "preflight_invalid",
        "spawn_failed",
        "terminated_by_signal",
    }
    if observation.record is None:
        assert version is None
    else:
        assert observation.record.coverage_version == version


def test_coverage_preflight_rejects_truncated_stderr() -> None:
    process = _process(stdout=_document())
    object.__setattr__(process, "stderr", CapturedBytes(b"tail", 1))

    observation = coverage_execution.classify_coverage_preflight(process)

    assert observation.classification == "preflight_invalid"


def test_coverage_preflight_fails_closed_on_an_oversized_stable_version() -> None:
    observation = coverage_execution.classify_coverage_preflight(
        _process(stdout=_document(coverage_version=f"7.{'9' * 5000}"))
    )

    assert observation.classification == "unsupported_version"


def test_coverage_probe_is_python_37_compatible_and_reads_python_first() -> None:
    probe = coverage_execution.COVERAGE_PREFLIGHT_PROBE

    assert "f\"" not in probe
    assert probe.index("sys.version_info") < probe.index("import coverage")
    assert 'separators=(",", ":")' in probe


def _pytest_document() -> bytes:
    return b'{"schema_version":1,"python_version":[3,13,15],"pytest_available":true,"pytest_version":[8,4,2]}'


def _coverage_check(tmp_path: Path) -> PlannedCheck:
    config_path = tmp_path / "pyproject.toml"
    pytest_plan = PytestExecutionPlan(
        consumer_python=("consumer-python",),
        pytest_args=("tests",),
        coverage=CoverageExecutionPlan(
            consumer_python=("consumer-python",), config_path=config_path, fail_under=None
        ),
    )
    return PlannedCheck(
        name="pytest",
        command=("consumer-python", "-m", "pytest", "tests"),
        cwd=tmp_path,
        pytest=pytest_plan,
    )


def test_coverage_execution_orders_preflights_then_runs_one_instrumented_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "consumer-path")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "consumer-startup")
    monkeypatch.setenv("COVERAGE_FILE", "consumer-data")
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        calls.append((command, env))
        stdout = (
            _coverage_document()
            if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE
            else _pytest_document()
            if "-c" in command
            else b""
        )
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(command, 0, stdout=stdout, stderr=b""),
        )

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
    ]
    assert [command for command, _environment in calls][2].count("-m") == 2
    assert calls[2][0][1:5] == ("-m", "coverage", "run", f"--rcfile={tmp_path / 'pyproject.toml'}")
    coverage_environment = calls[2][1]
    assert coverage_environment is not None
    assert calls[2][0][5] == f"--data-file={coverage_environment['COVERAGE_FILE']}"
    assert calls[2][0][6:10] == ("-m", "pytest", "-p", calls[2][0][9])
    assert sum(command.count("pytest") for command, _environment in calls) == 1
    assert calls[0][1] is not None and "COVERAGE_PROCESS_START" not in calls[0][1]
    for _command, environment in calls[1:]:
        assert environment is not None
        assert environment["COVERAGE_FILE"].endswith("/.coverage")
        assert environment["COVERAGE_RCFILE"] == str(tmp_path / "pyproject.toml")
        assert "COVERAGE_PROCESS_START" not in environment
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "supported"
    assert observation.coverage.artifact.state == "not_attempted"


def _coverage_document(*, version: str = "7.15.2") -> bytes:
    return _document(coverage_version=version)


def test_coverage_preflight_runs_after_a_non_spawn_pytest_preflight_failure(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        calls.append(command)
        stdout = _coverage_document() if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE else b"bad"
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(command, 0, stdout=stdout, stderr=b""),
        )

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "coverage_preflight",
    ]
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "supported"


def test_coverage_preflight_is_not_attempted_after_pytest_preflight_spawn_failure(
    tmp_path: Path,
) -> None:
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del command, cwd, check, capture_output, env
        raise FileNotFoundError("consumer-python")

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == ["pytest_preflight"]
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "preflight_invalid"


def test_unsupported_coverage_preflight_prevents_a_primary_fallback(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        calls.append(command)
        stdout = _coverage_document(version="7.14.9") if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE else _pytest_document()
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(command, 0, stdout=stdout, stderr=b""),
        )

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "coverage_preflight",
    ]
    assert len(calls) == 2
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "unsupported_version"


def test_workspace_capability_failure_attempts_no_preflight_and_retains_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyrepo_check import pytest_execution

    monkeypatch.setattr(pytest_execution, "_platform_capability_error", lambda: "unsupported")
    calls: list[tuple[str, ...]] = []

    def runner(*args: object, **kwargs: object) -> CompletedProcess[tuple[str, ...]]:
        del args, kwargs
        calls.append(("unexpected",))
        raise AssertionError("runner must not be called")

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert calls == []
    assert observation.processes == ()
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "preflight_invalid"
    assert observation.coverage.artifact.state == "not_attempted"
