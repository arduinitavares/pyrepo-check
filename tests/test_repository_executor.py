from __future__ import annotations

from pathlib import Path
import subprocess  # nosec B404

from pyrepo_check.repository_executor import prepare_safe_repository
from tests.support import (
    RecordingRunner,
    environment_probe_bytes,
    focused_plan,
    monotonic_clock,
)


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
    )


def _write_locked_project(root: Path) -> Path:
    resolved = root.resolve()
    (resolved / "src").mkdir(parents=True)
    (resolved / ".venv/bin").mkdir(parents=True)
    (resolved / ".venv/bin/python").write_bytes(b"")
    (resolved / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (resolved / "src/example.py").write_text("value = 1\n", encoding="utf-8")
    (resolved / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\nrequires-python='>=3.10'\n",
        encoding="utf-8",
    )
    (resolved / "uv.lock").write_text(
        "version = 1\nrevision = 3\n",
        encoding="utf-8",
    )
    _run_git(resolved, "init", "-q")
    _run_git(resolved, "add", ".")
    _run_git(
        resolved,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return resolved


def _successful_runner(root: Path) -> RecordingRunner:
    stage = _run_git(root, "ls-files", "--stage", "-z", "--", ".").stdout
    environment_root = root / ".venv"
    return RecordingRunner(
        stdout=(
            str(root).encode() + b"\n",
            b"",
            b"",
            stage,
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 13, 15),
                executable=environment_root / "bin/python",
                environment_root=environment_root,
            ),
        )
    )


def test_missing_lock_returns_canonical_failure_before_git_or_uv(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    runner = RecordingRunner()

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "repository_lock_missing"
    assert observation.lock_status == "missing"
    assert observation.processes == ()
    assert runner.calls == []


def test_unsafe_lock_returns_canonical_failure_before_git_or_uv(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    target = root / "lock-target"
    target.write_text("version = 1\n", encoding="utf-8")
    (root / "uv.lock").symlink_to(target)
    runner = RecordingRunner()

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "unsafe_repository_environment"
    assert observation.processes == ()
    assert runner.calls == []


def test_unsafe_venv_stops_before_git_or_uv(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\nrevision = 3\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (root / ".venv").symlink_to(outside, target_is_directory=True)
    runner = RecordingRunner()

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "unsafe_repository_environment"
    assert observation.processes == ()
    assert runner.calls == []
    assert list(outside.iterdir()) == []


def test_failed_preparation_retains_baseline_process_order(tmp_path: Path) -> None:
    root = _write_locked_project(tmp_path)
    stage = _run_git(root, "ls-files", "--stage", "-z", "--", ".").stdout
    runner = RecordingRunner(
        returncodes=(0, 0, 0, 0, 1),
        stdout=(str(root).encode() + b"\n", b"", b"", stage, b""),
    )

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is not None
    assert result.preparation.prepared is None
    observation = result.preparation.observation
    assert observation.error is not None
    assert observation.error.code == "uv_unavailable"
    assert tuple(process.role for process in observation.processes) == (
        "repository_git_root",
        "repository_venv_tracked",
        "repository_venv_ignored",
        "repository_tracked_snapshot",
        "uv_version",
    )
    assert all("ruff" not in call.command for call in runner.calls)


def test_safe_preparation_runs_safety_before_uv_and_starts_no_check(tmp_path: Path) -> None:
    root = _write_locked_project(tmp_path)
    runner = _successful_runner(root)

    result = prepare_safe_repository(
        focused_plan(root, "ruff"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.baseline is not None
    assert result.preparation.prepared is not None
    observation = result.preparation.observation
    assert observation.error is None
    assert tuple(process.role for process in observation.processes) == (
        "repository_git_root",
        "repository_venv_tracked",
        "repository_venv_ignored",
        "repository_tracked_snapshot",
        "uv_version",
        "environment_probe",
    )
    assert observation.processes[3].command[-5:] == (
        "ls-files",
        "--stage",
        "-z",
        "--",
        ".",
    )
    assert observation.processes[4].command == ("uv", "--version")
    assert observation.mutation_protection == "unobserved"
    assert all("ruff" not in call.command for call in runner.calls)
    assert len(runner.calls) == 6
