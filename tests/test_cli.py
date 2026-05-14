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
