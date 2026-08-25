from pathlib import Path
import json
import os
import subprocess  # nosec B404
import sys
import tomllib
from types import SimpleNamespace
import uuid

import pytest

from pyrepo_check.cli import main, parse_args
from tests.support import RecordedCall, RecordingRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_external_consumer(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "support").mkdir()
    (root / ".gitignore").write_text(".venv/\n.pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """[project]
name = "c2-external-consumer"
version = "0.0.0"
requires-python = ">=3.13.15"
dependencies = ["pytest>=8,<9"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        encoding="utf-8",
    )
    (root / "support" / "support_marker.py").write_text(
        "VALUE = 'inherited-pythonpath'\n", encoding="utf-8"
    )
    (root / "consumer_marker.py").write_text("VALUE = 'consumer'\n", encoding="utf-8")
    (root / "tests" / "test_consumer.py").write_text(
        """from pathlib import Path
import consumer_marker
import importlib.util
import os
import support_marker
import sys


def test_consumer_execution_is_owned_by_the_consumer() -> None:
    assert Path.cwd() == Path(__file__).parents[1]
    assert consumer_marker.VALUE == "consumer"
    assert support_marker.VALUE == "inherited-pythonpath"
    assert str(Path.cwd()) in sys.path
    assert importlib.util.find_spec("pyrepo_check") is None
    assert "COVERAGE_PROCESS_START" not in os.environ
    assert "COVERAGE_PROCESS_CONFIG" not in os.environ
""",
        encoding="utf-8",
    )


def _write_forged_legacy_plugin(root: Path) -> None:
    (root / "pyrepo_check_pytest_evidence_plugin.py").write_text(
        '''import json
import os
from pathlib import Path

Path(os.environ["SHADOW_IMPORTED"]).write_text("imported", encoding="utf-8")
WRITER_ID = "forged-consumer"


def pytest_sessionstart(session):
    del session
    writer_dir = Path(os.environ["PYREPO_CHECK_PYTEST_WRITER_DIR"])
    marker = writer_dir / f"pytest-writer-{WRITER_ID}.json"
    with marker.open("x", encoding="utf-8") as marker_file:
        json.dump({"schema_version": 1, "writer_id": WRITER_ID, "pid": os.getpid()}, marker_file)


def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    session.exitstatus = 0
    artifact = {
        "schema_version": 1,
        "state": "finalized",
        "writer_id": WRITER_ID,
        "pytest_version": "8.4.2",
        "session": {
            "starts": 1,
            "finishes": 1,
            "exit_code": 0,
            "collection_completed": True,
            "stopped_early": False,
        },
        "effective_args": [],
        "semantic_options": {
            "collection_paths": [],
            "keyword": "",
            "markexpr": "",
            "deselect": [],
            "ignore": [],
            "ignore_glob": [],
            "lf": False,
            "pyargs": False,
            "collectonly": False,
            "setuponly": False,
            "setupplan": False,
        },
        "collection": {
            "initial_nodeids": [],
            "final_nodeids": [],
            "deselected_nodeids": [],
            "uncovered_removed_nodeids": [],
            "errors": [],
            "skips": [],
        },
        "reports": [],
        "flags": {
            "unsupported_parallelism": False,
            "unsupported_retries": False,
            "worker_metadata": False,
        },
    }
    Path(os.environ["PYREPO_CHECK_PYTEST_JSON"]).write_text(json.dumps(artifact), encoding="utf-8")
''',
        encoding="utf-8",
    )


def _write_pytest_nine_consumer(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '''[project]
name = "c2-pytest-nine-consumer"
version = "0.0.0"
requires-python = ">=3.13.15"
dependencies = ["pytest==9.0.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
''',
        encoding="utf-8",
    )
    (root / "tests" / "test_must_not_run.py").write_text(
        '''from pathlib import Path
import os

Path(os.environ["TEST_MODULE_IMPORTED"]).write_text("imported", encoding="utf-8")


def test_must_not_run():
    assert False
''',
        encoding="utf-8",
    )


def test_external_consumer_emits_structured_pytest_json_and_stays_clean(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_external_consumer(consumer)
    subprocess.run(("uv", "lock"), cwd=consumer, check=True, capture_output=True)  # nosec B603
    subprocess.run(("git", "init", "-q"), cwd=consumer, check=True)  # nosec B603
    before_status = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    environment = dict(os.environ)
    environment["COVERAGE_PROCESS_START"] = str(consumer / "coverage.toml")
    environment["COVERAGE_PROCESS_CONFIG"] = str(consumer / "coverage.toml")
    environment["COVERAGE_FILE"] = str(consumer / ".coverage")
    environment["PYTHONPATH"] = str(consumer / "support")
    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--format",
            "json",
            "pytest",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    after_status = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert payload["pytest"]["evidence"] is not None
    assert [process["role"] for process in payload["checks"][0]["processes"]] == [
        "pytest_preflight",
        "primary",
    ]
    assert after_status == before_status
    assert not list(consumer.rglob(".coverage*"))
    assert not list(consumer.rglob("pyrepo_check_pytest_*.py"))


def test_external_consumer_cannot_shadow_isolated_plugin_or_forge_a_pass(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_external_consumer(consumer)
    (consumer / "tests" / "test_consumer.py").write_text(
        "def test_native_failure():\n    assert False\n",
        encoding="utf-8",
    )
    _write_forged_legacy_plugin(consumer)
    subprocess.run(("uv", "lock"), cwd=consumer, check=True, capture_output=True)  # nosec B603
    shadow_imported = tmp_path / "shadow-imported"
    environment = dict(os.environ)
    environment["SHADOW_IMPORTED"] = str(shadow_imported)

    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--root",
            str(consumer),
            "--format",
            "json",
            "pytest",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert completed.stderr == b""
    assert payload["overall_status"] == "failed"
    assert payload["pytest"]["status"] == "failed"
    assert payload["pytest"]["exit_code"] == 1
    assert payload["pytest"]["evidence"]["counts"]["failed"] == 1
    assert not shadow_imported.exists()


def test_external_pytest_nine_consumer_stops_after_unsupported_preflight(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_pytest_nine_consumer(consumer)
    subprocess.run(("uv", "lock"), cwd=consumer, check=True, capture_output=True)  # nosec B603
    test_module_imported = tmp_path / "test-module-imported"
    environment = dict(os.environ)
    environment["TEST_MODULE_IMPORTED"] = str(test_module_imported)

    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--root",
            str(consumer),
            "--format",
            "json",
            "pytest",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    processes = payload["checks"][0]["processes"]
    assert completed.returncode == 2
    assert completed.stderr == b""
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["pytest"]["status"] == "error"
    assert payload["pytest"]["evidence"] is None
    assert payload["pytest"]["error"] is not None
    assert payload["pytest"]["error"]["code"] == "unsupported_version"
    assert [process["role"] for process in processes] == ["pytest_preflight"]
    assert processes[0]["argv"][:3] == ["uv", "run", "--frozen"]
    preflight_record = json.loads(processes[0]["stdout"]["text"])
    assert preflight_record["pytest_version"] == [9, 0, 0]
    assert not test_module_imported.exists()
    assert not (consumer / ".pytest_cache").exists()
    assert not list(consumer.rglob("artifact.json"))
    assert not list(consumer.rglob("pytest-writer-*.json"))


def test_direct_pytest_node_id_is_forwarded_verbatim(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_example.py"
    test_file.parent.mkdir()
    test_file.write_text("", encoding="utf-8")
    runner = RecordingRunner(publish_pytest_artifact=True)

    result = main(
        [
            "--root",
            str(tmp_path),
            "pytest",
            "tests/test_example.py::test_exact_behavior",
        ],
        runner=runner,
    )

    assert result == 0
    plugin_name = runner.calls[1].command[runner.calls[1].command.index("-p") + 1]
    assert [call.command for call in runner.calls] == [
        ("uv", "run", "python", "-c", runner.calls[0].command[-1]),
        (
            "uv",
            "run",
                "python",
                "-m",
                "pytest",
                "-p",
                plugin_name,
                "tests/test_example.py::test_exact_behavior",
        )
    ]


def test_recording_runner_opt_in_publishes_raw_pytest_protocol(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    runner = RecordingRunner(publish_pytest_artifact=True)

    runner(
        ("consumer-python", "-m", "pytest", "-p", "owned_plugin", "tests"),
        cwd=tmp_path,
        check=False,
        env={
            "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
            "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
        },
    )

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    markers = list(writer_directory.glob("pytest-writer-*.json"))
    assert artifact["state"] == "finalized"
    assert artifact["effective_args"] == ["tests"]
    assert artifact["writer_id"] == json.loads(markers[0].read_text())["writer_id"]


def test_recording_runner_uses_exclusive_writer_marker_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    collision = "collision"
    writer_id = f"recording-runner-{os.getpid()}-{collision}"
    marker_path = writer_directory / f"pytest-writer-{writer_id}.json"
    marker_path.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(
        uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=collision),
    )
    runner = RecordingRunner(publish_pytest_artifact=True)

    with pytest.raises(FileExistsError):
        runner(
            ("consumer-python", "-m", "pytest", "-p", "owned_plugin"),
            cwd=tmp_path,
            check=False,
            env={
                "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
                "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
            },
        )

    assert marker_path.read_text(encoding="utf-8") == "occupied"
    assert not artifact_path.exists()


def test_recording_runner_registers_writer_before_atomic_artifact_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def observe_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        markers = list(writer_directory.glob("pytest-writer-*.json"))
        assert len(markers) == 1
        assert source_path.parent == artifact_path.parent
        assert source_path.name.startswith(f".{artifact_path.name}.")
        assert source_path.name.endswith(".tmp")
        assert source_path.is_file()
        assert destination_path == artifact_path
        replacements.append((source_path, destination_path))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observe_replace)
    runner = RecordingRunner(publish_pytest_artifact=True)

    runner(
        ("consumer-python", "-m", "pytest", "-p", "owned_plugin"),
        cwd=tmp_path,
        check=False,
        env={
            "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
            "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
        },
    )

    assert len(replacements) == 1
    assert artifact_path.is_file()
    assert not list(tmp_path.glob(f".{artifact_path.name}.*.tmp"))


def test_recording_runner_repeated_primaries_leave_distinct_writer_markers(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    environment = {
        "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
        "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
    }
    runner = RecordingRunner(publish_pytest_artifact=True)
    command = ("consumer-python", "-m", "pytest", "-p", "owned_plugin")

    runner(command, cwd=tmp_path, check=False, env=environment)
    first_writer_id = json.loads(artifact_path.read_text(encoding="utf-8"))["writer_id"]
    runner(command, cwd=tmp_path, check=False, env=environment)
    second_writer_id = json.loads(artifact_path.read_text(encoding="utf-8"))["writer_id"]

    marker_ids = {
        json.loads(marker.read_text(encoding="utf-8"))["writer_id"]
        for marker in writer_directory.glob("pytest-writer-*.json")
    }
    assert first_writer_id != second_writer_id
    assert marker_ids == {first_writer_id, second_writer_id}


@pytest.mark.parametrize(
    ("returncodes", "raise_on_call", "expected"),
    [
        ((-15, 7, 0, 0, 0), None, 7),
        ((-15, 0, 0, 0, 0), None, 2),
        ((0, 0, 0, 0, 0), 1, 2),
        ((0, 7, 0, 0, 0), 1, 7),
    ],
)
def test_legacy_exit_code_classifies_spawn_and_negative_outcomes(
    tmp_path: Path,
    returncodes: tuple[int, ...],
    raise_on_call: int | None,
    expected: int,
) -> None:
    (tmp_path / "src").mkdir()
    runner = RecordingRunner(returncodes=returncodes, raise_on_call=raise_on_call)

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == expected
    assert len(runner.calls) == 6


def test_spawn_exception_is_recorded_and_later_checks_continue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    error = FileNotFoundError("uv")
    stdout_at_spawn: list[str] = []

    def record_spawn(call: RecordedCall) -> None:
        stdout_at_spawn.append(capsys.readouterr().out)

    runner = RecordingRunner(
        raise_on_call=2,
        exception=error,
        on_call=record_spawn,
        publish_pytest_artifact=True,
    )

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == 2
    assert len(runner.calls) == 6
    assert stdout_at_spawn == [
        "\n==> ruff: uv run python -m ruff check .\n",
        (
            "\n==> annotations: uv run python -m ruff check . "
            "--select ANN --output-format concise\n"
        ),
        "\n==> ty: uv run python -m ty check\n",
        "\n==> bandit: uv run python -m bandit -c pyproject.toml -r .\n",
        "\n==> pytest: uv run python -m pytest\n",
        "",
    ]
    assert capsys.readouterr().out == (
        "\n==> pyrepo-check summary: error (incomplete)\n"
        "    error: annotations: Could not start process: FileNotFoundError: uv\n"
        "    passed: ruff, ty, bandit, pytest\n"
    )


def test_runner_value_error_is_not_a_planning_error(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    error = ValueError("runner failed")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as captured:
        main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert captured.value is error


def test_banner_is_printed_before_each_spawn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    stdout_at_spawn: list[str] = []
    runner = RecordingRunner(
        on_call=lambda _call: stdout_at_spawn.append(capsys.readouterr().out)
    )

    result = main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert result == 0
    assert stdout_at_spawn == ["\n==> ruff: uv run python -m ruff check src\n"]


def test_help_surface_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["pyrepo-check"])

    with pytest.raises(SystemExit) as captured:
        parse_args(["--help"])

    output = capsys.readouterr()
    assert captured.value.code == 0
    assert output.out == """usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen]
                    [--format {terminal,json}] [--shortcut NAME] [--coverage]
                    [checks ...]

Run Python repository quality checks.

positional arguments:
  checks                Optional check names and target paths. Checks: ruff,
                        annotations, annotations-fix, ty, bandit, pytest.

options:
  -h, --help            show this help message and exit
  --all                 Run all checks.
  --root ROOT           Project root to check. Defaults to the current working
                        directory.
  --no-frozen           Run uv without --frozen even when uv.lock exists.
  --format {terminal,json}
                        Output terminal diagnostics or one JSON document.
  --shortcut NAME       Run a configured Test Shortcut in a pytest-only
                        focused run.
  --coverage            Plan Coverage.py collection for the selected pytest
                        run.
"""
    assert output.err == ""


def test_format_defaults_to_terminal() -> None:
    assert parse_args([]).format == "terminal"


def test_json_format_is_public_syntax_before_checks() -> None:
    args = parse_args(["--format", "json", "ty"])

    assert args.format == "json"
    assert args.checks == ["ty"]


@pytest.mark.parametrize(
    "argv",
    (
        ("--shortcut", "unit", "pytest"),
        ("pytest", "--shortcut", "unit"),
    ),
)
def test_shortcut_is_public_syntax_in_both_supported_placements(
    argv: tuple[str, ...],
) -> None:
    args = parse_args(argv)

    assert args.checks == ["pytest"]
    assert args.shortcut == "unit"


def test_missing_shortcut_operand_remains_argparse_owned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["--shortcut"])

    output = capsys.readouterr()
    assert captured.value.code == 2
    assert output.out == ""
    assert "argument --shortcut: expected one argument" in output.err


def test_invalid_format_remains_argparse_owned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["--format", "xml"])

    output = capsys.readouterr()
    assert captured.value.code == 2
    assert output.out == ""
    assert "invalid choice: 'xml'" in output.err
    assert "choose from 'terminal', 'json'" in output.err


def test_python_requirement_is_consistent_across_active_contracts() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    reporting_design = (
        PROJECT_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-24-agent-guidance-reporting-design.md"
    ).read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert pyproject["project"]["requires-python"] == ">=3.13.15"
    assert pyproject["tool"]["ruff"]["target-version"] == "py313"
    assert lockfile["requires-python"] == ">=3.13.15"
    assert (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8") == "3.13.15\n"
    assert "consumer Python `>=3.13.15`" in reporting_design
    assert "consumer Python `>=3.9`" not in reporting_design
    assert "consumer Python `>=3.10`" not in reporting_design
    assert "consumer Python below 3.9" not in reporting_design
    assert "consumer Python below 3.10" not in reporting_design
    assert "Python 3.13.15 or newer is required" in readme


def test_pytest_fixture_dependencies_are_development_only() -> None:
    """Reject moving plugin integration fixtures into runtime dependencies."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == []
    assert "pytest-xdist>=3.8,<4" in pyproject["dependency-groups"]["dev"]
    assert "pytest-rerunfailures>=16.6,<17" in pyproject["dependency-groups"]["dev"]
