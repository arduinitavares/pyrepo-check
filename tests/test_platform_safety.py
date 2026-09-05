"""Unsupported filesystem guarantees are environment errors, not invalid TOML."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from pyrepo_check import config
from pyrepo_check.cli import main
from pyrepo_check.filesystem import PlatformSafetyError
from pyrepo_check.reporting import serialize_json
from pyrepo_check.reporting_schema import CheckErrorV2
from tests.test_reporting_schema_v2 import pytest_workspace_failure_report, valid_run_report


def test_unavailable_file_safety_is_reported_as_platform_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(path: Path, *, max_bytes: int) -> bytes:
        raise PlatformSafetyError("filesystem cannot provide stable file identities")

    monkeypatch.setattr(config, "read_regular_file", unavailable)
    assert main(["--root", str(tmp_path), "--format", "json", "ruff"]) != 0
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "platform_safety_unavailable"
    assert "stable file identities" in report["error"]["message"]


@pytest.mark.parametrize("check", ("ruff", "pytest"))
def test_platform_setup_failure_serializes_without_claiming_execution(check: str) -> None:
    report = pytest_workspace_failure_report() if check == "pytest" else valid_run_report()
    failed = replace(
        report.checks[0], status="error", execution_environment=None,
        analysis_python_authority=None, start_evidence=None, processes=(),
        error=CheckErrorV2("platform_safety_unavailable", "private ACL unsupported", None),
    )
    report = replace(report, overall_status="error", complete=False, checks=(failed,))

    payload = json.loads(serialize_json(report))

    assert payload["checks"][0]["error"]["code"] == "platform_safety_unavailable"
    assert payload["checks"][0]["processes"] == []
