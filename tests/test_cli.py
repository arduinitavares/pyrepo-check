from pathlib import Path
from subprocess import CompletedProcess

import pytest

from pyrepo_check.cli import main


def test_cli_runs_selected_check_from_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    calls: list[tuple[tuple[str, ...], Path]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append((command, cwd))
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "ruff"], runner=fake_runner)

    assert result == 0
    assert calls == [(("uv", "run", "python", "-m", "ruff", "check", "src"), tmp_path)]


def test_cli_runs_annotations_check_from_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "annotations"], runner=fake_runner)

    assert result == 0
    assert calls == [
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            "src",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        )
    ]


def test_cli_runs_annotations_fix_from_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "annotations-fix"], runner=fake_runner)

    assert result == 0
    assert calls == [
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            "src",
            "--select",
            "ANN",
            "--fix",
            "--unsafe-fixes",
        )
    ]


def test_cli_passes_file_target_to_selected_check(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], Path]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append((command, cwd))
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "ruff", "api.py"], runner=fake_runner)

    assert result == 0
    assert calls == [
        (("uv", "run", "python", "-m", "ruff", "check", "api.py"), tmp_path)
    ]


def test_cli_passes_file_target_to_annotations_check(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "annotations", "api.py"], runner=fake_runner)

    assert result == 0
    assert calls == [
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            "api.py",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        )
    ]


def test_cli_passes_file_target_to_annotations_fix(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "annotations-fix", "api.py"], runner=fake_runner)

    assert result == 0
    assert calls == [
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            "api.py",
            "--select",
            "ANN",
            "--fix",
            "--unsafe-fixes",
        )
    ]


def test_cli_runs_file_checks_against_file_target_when_no_check_is_named(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "api.py"], runner=fake_runner)

    assert result == 0
    assert calls == [
        ("uv", "run", "python", "-m", "ruff", "check", "api.py"),
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            "api.py",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        ),
        ("uv", "run", "python", "-m", "ty", "check", "api.py"),
        ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "api.py"),
    ]


def test_cli_runs_all_checks_against_file_target_with_all_flag(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "--all", "api.py"], runner=fake_runner)

    assert result == 0
    assert calls == [
        ("uv", "run", "python", "-m", "ruff", "check", "api.py"),
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            "api.py",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        ),
        ("uv", "run", "python", "-m", "ty", "check", "api.py"),
        ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "api.py"),
        ("uv", "run", "python", "-m", "pytest", "api.py"),
    ]


def test_cli_all_includes_annotations_but_not_annotations_fix(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "--all"], runner=fake_runner)

    assert result == 0
    assert calls == [
        ("uv", "run", "python", "-m", "ruff", "check", "."),
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            ".",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        ),
        ("uv", "run", "python", "-m", "ty", "check"),
        ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "-r", "."),
        ("uv", "run", "python", "-m", "pytest"),
    ]


def test_cli_no_args_uses_strict_repository_targets(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyrepo-check]
ruff_targets = ["tests", "scripts"]
bandit_targets = ["tests"]
""".strip(),
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path)], runner=fake_runner)

    assert result == 0
    assert calls == [
        ("uv", "run", "python", "-m", "ruff", "check", "."),
        (
            "uv",
            "run",
            "python",
            "-m",
            "ruff",
            "check",
            ".",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        ),
        ("uv", "run", "python", "-m", "ty", "check"),
        ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "-r", "."),
        ("uv", "run", "python", "-m", "pytest"),
    ]


def test_cli_returns_two_for_unknown_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["--root", str(tmp_path), "mypy"])

    captured = capsys.readouterr()

    assert result == 2
    assert "Unknown check(s): mypy" in captured.err


def test_cli_no_frozen_flag_overrides_lock(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=0)

    result = main(["--root", str(tmp_path), "--no-frozen", "ruff"], runner=fake_runner)

    assert result == 0
    assert calls == [("uv", "run", "python", "-m", "ruff", "check", "src")]
