from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import sys
from types import MappingProxyType
from typing import Any, cast

import pytest

import pyrepo_check.repository_environment as repository_environment
from pyrepo_check.repository_environment import (
    ENVIRONMENT_PROBE_SOURCE,
    inspect_repository_lock,
    locked_repository_prefix,
    prepare_repository_environment,
    sanitized_repository_environment,
    validate_uv_storage_boundaries,
)
from pyrepo_check.execution import RepositoryLockPresence
from pyrepo_check.planning import RunPlan
from tests.support import (
    RecordingRunner,
    environment_probe_bytes,
    focused_plan,
    monotonic_clock,
    write_minimal_uv_project,
)


_ALLOWED_UV_CONTROLS = (
    "UV_INDEX",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_INDEX_STRATEGY",
    "UV_KEYRING_PROVIDER",
    "UV_NATIVE_TLS",
    "UV_SYSTEM_CERTS",
    "UV_OFFLINE",
    "UV_INSECURE_HOST",
    "UV_CACHE_DIR",
    "UV_NO_CACHE",
    "UV_LINK_MODE",
    "UV_COMPILE_BYTECODE",
    "UV_NO_PROGRESS",
    "UV_NO_BUILD",
    "UV_NO_BUILD_PACKAGE",
    "UV_NO_BUILD_ISOLATION",
    "UV_NO_BINARY",
    "UV_NO_BINARY_PACKAGE",
    "UV_PYTHON_DOWNLOADS",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_CACHE_DIR",
    "UV_PYTHON_BIN_DIR",
)


def test_sanitized_environment_keeps_only_approved_uv_controls() -> None:
    private_password = "".join(("sec", "ret"))
    app_token = "".join(("repository", "-secret"))
    allowed = {name: f"allowed-{index}" for index, name in enumerate(_ALLOWED_UV_CONTROLS)}
    allowed.update(
        {
            "UV_INDEX_PRIVATE_USERNAME": "agent",
            "UV_INDEX_PRIVATE_PASSWORD": private_password,
            "UV_INDEX_TEAM_2_USERNAME": "robot",
        }
    )
    source = {
        "PATH": "/bin",
        "APP_TOKEN": app_token,
        "PYTHONHOME": "/controller",
        "PYTHONPATH": "/controller/src",
        "PYTHONEXECUTABLE": "/controller/python",
        "VIRTUAL_ENV": "/controller/.venv",
        "CONDA_PREFIX": "/controller/conda",
        "__PYVENV_LAUNCHER__": "/controller/launcher",
        "UV_PROJECT": "/wrong",
        "UV_WORKING_DIR": "/wrong",
        "UV_PYTHON": "3.14",
        "UV_GROUP": "wrong",
        "UV_CONFIG_FILE": "/wrong/uv.toml",
        "UV_LOCKED": "0",
        "UV_FROZEN": "1",
        "UV_ACTIVE": "1",
        "UV_ISOLATED": "1",
        "UV_NO_SYNC": "1",
        "UV_MANAGED_PYTHON": "1",
        "UV_NO_MANAGED_PYTHON": "1",
        "UV_PYTHON_PREFERENCE": "system",
        "UV_PYTHON_SEARCH_PATH": "/wrong",
        "UV_INDEX_private_USERNAME": "lowercase-name-is-not-approved",
        "UV_UNDOCUMENTED": "removed",
        **allowed,
    }

    cleaned = sanitized_repository_environment(source)

    assert isinstance(cleaned, MappingProxyType)
    assert cleaned["PATH"] == "/bin"
    assert cleaned["APP_TOKEN"] == app_token
    assert {name: cleaned[name] for name in allowed} == allowed
    assert set(cleaned) == {"PATH", "APP_TOKEN", *allowed}
    with pytest.raises(TypeError):
        cast(dict[str, str], cleaned)["PATH"] = "/changed"


@pytest.mark.parametrize(
    "variable",
    (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_CACHE_DIR",
        "UV_PYTHON_BIN_DIR",
    ),
)
@pytest.mark.parametrize(
    "destination_kind",
    ("relative", "root", "descendant", "symlink_descendant"),
)
def test_storage_override_rejects_every_repository_alias_without_mutation(
    tmp_path: Path,
    variable: str,
    destination_kind: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"unchanged")
    external = tmp_path / "external"
    external.mkdir()
    alias = external / "repository-alias"
    alias.symlink_to(root, target_is_directory=True)
    destinations = {
        "relative": Path("relative-storage"),
        "root": root,
        "descendant": root / "storage",
        "symlink_descendant": alias / "storage",
    }
    environment = _external_storage_environment(tmp_path)
    environment[variable] = str(destinations[destination_kind])

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"
    assert sentinel.read_bytes() == b"unchanged"
    assert not (root / "storage").exists()


@pytest.mark.parametrize(
    "variable",
    (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_CACHE_DIR",
        "UV_PYTHON_BIN_DIR",
    ),
)
def test_absolute_external_storage_override_is_accepted(
    tmp_path: Path,
    variable: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    environment = _external_storage_environment(tmp_path)
    environment[variable] = str(tmp_path / f"external-{variable.lower()}")

    assert validate_uv_storage_boundaries(root.resolve(), environment) is None


def test_no_cache_validates_effective_temporary_storage_and_ignores_cache_dir(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"unchanged")
    environment = _external_storage_environment(tmp_path)
    environment.update(
        {
            "UV_NO_CACHE": "1",
            "UV_CACHE_DIR": str(root / "ineffective-cache"),
            "TMPDIR": str(root / "temporary-cache-parent"),
        }
    )

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"
    assert "temporary" in failure.message.lower()
    assert sentinel.read_bytes() == b"unchanged"
    assert not (root / "ineffective-cache").exists()
    assert not (root / "temporary-cache-parent").exists()


def test_no_cache_accepts_external_temporary_storage_despite_unsafe_cache_override(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    environment = _external_storage_environment(tmp_path)
    environment.update(
        {
            "UV_NO_CACHE": "true",
            "UV_CACHE_DIR": str(root / "ineffective-cache"),
            "TMPDIR": str(tmp_path / "external-temporary-cache-parent"),
        }
    )

    assert validate_uv_storage_boundaries(root.resolve(), environment) is None


def test_project_config_cache_inside_repository_stops_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_uv_project(tmp_path)
    (tmp_path / "uv.toml").write_text('cache-dir = ".uv-write-cache"\n', encoding="utf-8")
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    _apply_external_storage_environment(monkeypatch, tmp_path.parent)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("UV_NO_CACHE", raising=False)
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"
    assert "configuration cache" in preparation.observation.error.message.lower()
    assert sentinel.read_bytes() == b"unchanged"
    assert not (tmp_path / ".uv-write-cache").exists()


def test_project_config_no_cache_validates_temporary_storage_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_uv_project(tmp_path)
    (tmp_path / "uv.toml").write_text(
        f'no-cache = true\ncache-dir = "{tmp_path.parent / "ineffective-cache"}"\n',
        encoding="utf-8",
    )
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    _apply_external_storage_environment(monkeypatch, tmp_path.parent)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("UV_NO_CACHE", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temporary-cache-parent"))
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"
    assert "temporary" in preparation.observation.error.message.lower()
    assert sentinel.read_bytes() == b"unchanged"
    assert not (tmp_path / "temporary-cache-parent").exists()


def test_user_config_cache_inside_repository_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    config_home = tmp_path / "config"
    (config_home / "uv").mkdir(parents=True)
    (config_home / "uv/uv.toml").write_text(
        f'cache-dir = "{root / "user-cache"}"\n',
        encoding="utf-8",
    )
    environment = _external_storage_environment(tmp_path)
    environment.pop("UV_CACHE_DIR")
    environment["XDG_CONFIG_HOME"] = str(config_home)

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"
    assert "configuration cache" in failure.message.lower()
    assert not (root / "user-cache").exists()


def test_explicit_cache_dir_overrides_unsafe_configuration_cache(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    config_home = tmp_path / "config"
    (config_home / "uv").mkdir(parents=True)
    (config_home / "uv/uv.toml").write_text(
        f'cache-dir = "{root / "ineffective-user-cache"}"\n',
        encoding="utf-8",
    )
    environment = _external_storage_environment(tmp_path)
    environment["XDG_CONFIG_HOME"] = str(config_home)

    assert validate_uv_storage_boundaries(root.resolve(), environment) is None


def test_configuration_no_cache_cannot_be_disabled_by_false_environment_value(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "uv.toml").write_text("no-cache = true\n", encoding="utf-8")
    environment = _external_storage_environment(tmp_path)
    environment.update({"UV_NO_CACHE": "0", "TMPDIR": str(root / "temporary-cache")})

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"
    assert "temporary" in failure.message.lower()
    assert not (root / "temporary-cache").exists()


def test_windows_system_config_authority_is_absolute_from_drive_designator() -> None:
    candidate = repository_environment._windows_system_config_path("C:")

    assert isinstance(candidate, PureWindowsPath)
    assert candidate.is_absolute()
    assert candidate == PureWindowsPath("C:/ProgramData/uv/uv.toml")


def test_windows_systemdrive_config_cache_stops_before_process_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_uv_project(tmp_path)
    system_drive = tmp_path.parent / "windows-system-drive"
    config_directory = system_drive / "ProgramData/uv"
    config_directory.mkdir(parents=True)
    (config_directory / "uv.toml").write_text(
        f'cache-dir = "{tmp_path / "system-cache"}"\n',
        encoding="utf-8",
    )
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    _apply_external_storage_environment(monkeypatch, tmp_path.parent)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("UV_NO_CACHE", raising=False)
    monkeypatch.setenv("SYSTEMDRIVE", str(system_drive))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path.parent / "ineffective-program-data"))
    monkeypatch.setattr(repository_environment, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        repository_environment,
        "_windows_system_config_path",
        lambda _system_drive: config_directory / "uv.toml",
        raising=False,
    )
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"
    assert "configuration cache" in preparation.observation.error.message.lower()
    assert sentinel.read_bytes() == b"unchanged"
    assert not (tmp_path / "system-cache").exists()


@pytest.mark.parametrize(
    ("base_variable", "unsafe_value"),
    (
        ("HOME", "."),
        ("XDG_CACHE_HOME", "."),
        ("XDG_DATA_HOME", "."),
        ("XDG_BIN_HOME", "."),
    ),
)
def test_relative_default_storage_base_is_rejected(
    tmp_path: Path,
    base_variable: str,
    unsafe_value: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    environment = _external_storage_environment(tmp_path)
    environment[base_variable] = unsafe_value
    _remove_overrides_using_base(environment, base_variable)

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"


@pytest.mark.parametrize(
    "base_variable",
    ("HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_BIN_HOME"),
)
def test_default_storage_destination_inside_repository_is_rejected(
    tmp_path: Path,
    base_variable: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    environment = _external_storage_environment(tmp_path)
    environment[base_variable] = str(root)
    _remove_overrides_using_base(environment, base_variable)

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"


def test_case_insensitive_storage_alias_is_rejected_when_filesystem_supports_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repositorylowercase"
    root.mkdir()
    alias = root.with_name(root.name.upper())
    try:
        same_directory = os.path.samefile(alias, root)
    except OSError:
        same_directory = False
    if not same_directory:
        pytest.skip()
    environment = _external_storage_environment(tmp_path)
    environment["UV_CACHE_DIR"] = str(alias / "cache")

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"


def test_missing_lock_stops_before_every_process(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n",
        encoding="utf-8",
    )
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.lock_status == "missing"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "repository_lock_missing"


def test_lock_inspection_distinguishes_missing_regular_and_unsafe_entries(
    tmp_path: Path,
) -> None:
    missing = inspect_repository_lock(tmp_path)
    assert missing == RepositoryLockPresence(tmp_path / "uv.lock", "missing", None)

    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    present = inspect_repository_lock(tmp_path)
    assert present == RepositoryLockPresence(tmp_path / "uv.lock", "present", None)

    (tmp_path / "uv.lock").unlink()
    (tmp_path / "uv.lock").mkdir()
    unsafe = inspect_repository_lock(tmp_path)
    assert unsafe.path == tmp_path / "uv.lock"
    assert unsafe.state == "unsafe"
    assert unsafe.diagnostic is not None


@pytest.mark.parametrize("unsafe_kind", ("directory", "symlink", "dangling_symlink"))
def test_unsafe_lock_entry_stops_before_every_process(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    lock_path = tmp_path / "uv.lock"
    if unsafe_kind == "directory":
        lock_path.mkdir()
    elif unsafe_kind == "symlink":
        target = tmp_path / "real-lock"
        target.write_text("version = 1\n", encoding="utf-8")
        lock_path.symlink_to(target)
    else:
        lock_path.symlink_to(tmp_path / "missing-lock")
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"


def test_supplied_missing_lock_observation_stops_without_reinspection_or_process(
    tmp_path: Path,
) -> None:
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner()
    supplied = RepositoryLockPresence(plan.root / "uv.lock", "missing", None)

    preparation = prepare_repository_environment(
        plan,
        lock_presence=supplied,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.observation.lock_status == "missing"


def test_foreign_lock_observation_is_rejected_without_a_process(tmp_path: Path) -> None:
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner()
    supplied = RepositoryLockPresence(tmp_path / "other.lock", "present", None)

    preparation = prepare_repository_environment(
        plan,
        lock_presence=supplied,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"


def test_forged_lock_state_is_rejected_without_a_process(tmp_path: Path) -> None:
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner()
    supplied = RepositoryLockPresence(
        plan.root / "uv.lock",
        cast(Any, "forged"),
        None,
    )

    preparation = prepare_repository_environment(
        plan,
        lock_presence=supplied,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"


def test_unsafe_storage_stops_before_uv_and_does_not_expose_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_uv_project(tmp_path)
    secret = "".join(("repository-token", "-do-not-report"))
    monkeypatch.setenv("APP_TOKEN", secret)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "unsafe-cache"))
    runner = RecordingRunner()

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 0
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"
    assert secret not in repr(preparation.observation)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="uv's legacy native storage path is platform-specific",
)
@pytest.mark.parametrize("storage_kind", ("cache", "python_install"))
def test_existing_macos_legacy_uv_storage_alias_is_rejected(
    tmp_path: Path,
    storage_kind: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "home"
    legacy_parent = (
        home / "Library/Caches"
        if storage_kind == "cache"
        else home / "Library/Application Support"
    )
    legacy_parent.mkdir(parents=True)
    (legacy_parent / "uv").symlink_to(root, target_is_directory=True)
    environment = _external_storage_environment(tmp_path)
    environment["HOME"] = str(home)
    if storage_kind == "cache":
        environment.pop("UV_CACHE_DIR")
    else:
        environment.pop("UV_PYTHON_INSTALL_DIR")

    failure = validate_uv_storage_boundaries(root.resolve(), environment)

    assert failure is not None
    assert failure.code == "unsafe_repository_environment"


def test_locked_probe_selects_one_repository_python(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty", repository_python="3.12")
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 12, 11),
                executable=plan.root / ".venv/bin/python",
                environment_root=plan.root / ".venv",
            ),
        )
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert tuple(call.command for call in runner.calls) == (
        ("uv", "--version"),
        (
            "uv",
            "run",
            "--locked",
            "--python",
            "3.12",
            "python",
            "-c",
            ENVIRONMENT_PROBE_SOURCE,
        ),
    )
    assert tuple(call.cwd for call in runner.calls) == (plan.root, plan.root)
    assert tuple(call.capture_output for call in runner.calls) == (True, True)
    assert preparation.observation.lock_status == "current"
    assert preparation.observation.manager_version == "0.10.12"
    assert preparation.prepared is not None
    assert preparation.prepared.python.version == (3, 12, 11)
    assert isinstance(preparation.prepared.child_environment, MappingProxyType)
    assert locked_repository_prefix(preparation.prepared) == (
        "uv",
        "run",
        "--locked",
        "--python",
        str(plan.root / ".venv/bin/python"),
        "python",
    )


def test_default_repository_python_omits_probe_selector(tmp_path: Path) -> None:
    plan, runner = _valid_probe(tmp_path)

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is not None
    assert runner.calls[1].command[:4] == ("uv", "run", "--locked", "python")


@pytest.mark.parametrize(
    ("returncodes", "stdout", "expected_code"),
    (
        ((1,), (b"uv 0.10.12\n",), "uv_unavailable"),
        ((-9,), (b"uv 0.10.12\n",), "uv_unavailable"),
        ((0,), (None,), "environment_evidence_invalid"),
        ((0,), (b"not-uv\n",), "environment_evidence_invalid"),
        ((0,), (b"uv 0.10.12\ntrailing\n",), "environment_evidence_invalid"),
        ((0,), (b"x" * 65_537,), "environment_evidence_invalid"),
    ),
)
def test_uv_version_failure_stops_before_locked_probe(
    tmp_path: Path,
    returncodes: tuple[int, ...],
    stdout: tuple[bytes | str | None, ...],
    expected_code: str,
) -> None:
    write_minimal_uv_project(tmp_path)
    runner = RecordingRunner(returncodes=returncodes, stdout=stdout)

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 1
    assert preparation.prepared is None
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == expected_code
    assert preparation.observation.lock_status == "unverified"


def test_uv_spawn_failure_is_uv_unavailable(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    runner = RecordingRunner(raise_on_call=1)

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 1
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "uv_unavailable"


def test_uv_version_is_capability_evidence_not_a_runtime_gate(tmp_path: Path) -> None:
    plan, runner = _valid_probe(tmp_path, uv_version=b"uv 99.42.7 (future build)\n")

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is not None
    assert preparation.prepared.manager_version == "99.42.7"


@pytest.mark.parametrize(
    ("returncodes", "raise_on_call"),
    (((0, 1), None), ((0, -9), None), ((0,), 2)),
)
def test_locked_probe_process_failure_keeps_lock_unverified(
    tmp_path: Path,
    returncodes: tuple[int, ...],
    raise_on_call: int | None,
) -> None:
    write_minimal_uv_project(tmp_path)
    runner = RecordingRunner(
        returncodes=returncodes,
        stdout=(b"uv 0.10.12\n",),
        raise_on_call=raise_on_call,
    )

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert len(runner.calls) == 2
    assert preparation.prepared is None
    assert preparation.observation.path is None
    assert preparation.observation.python is None
    assert preparation.observation.lock_status == "unverified"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "repository_environment_failed"


@pytest.mark.parametrize(
    "probe_output",
    (
        b"not-json",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1,"implementation":"cpython","version":[3,12,1],'
        b'"executable":"/tmp/python","environment_root":"/tmp/.venv","extra":1}',
        b"{} trailing",
        b"[1,2,3]",
        b"x" * 65_537,
    ),
)
def test_malformed_probe_evidence_is_rejected(
    tmp_path: Path,
    probe_output: bytes,
) -> None:
    write_minimal_uv_project(tmp_path)
    runner = RecordingRunner(stdout=(b"uv 0.10.12\n", probe_output))

    preparation = prepare_repository_environment(
        focused_plan(tmp_path, "ty"),
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path is None
    assert preparation.observation.python is None
    assert preparation.observation.lock_status == "unverified"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "environment_evidence_invalid"


def test_probe_evidence_at_exact_capture_limit_is_accepted(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    document = environment_probe_bytes(
        version=(3, 12, 1),
        executable=plan.root / ".venv/bin/python",
        environment_root=plan.root / ".venv",
    )
    probe_output = document + b" " * (65_536 - len(document))
    assert len(probe_output) == 65_536
    runner = RecordingRunner(stdout=(b"uv 0.10.12\n", probe_output))

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is not None
    assert preparation.observation.lock_status == "current"


def test_probe_nesting_above_eight_is_rejected_as_malformed(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    document = json.loads(
        environment_probe_bytes(
            version=(3, 12, 1),
            executable=plan.root / ".venv/bin/python",
            environment_root=plan.root / ".venv",
        )
    )
    nested_value: object = 0
    for _ in range(8):
        nested_value = [nested_value]
    document["nested"] = nested_value
    probe_output = json.dumps(document, separators=(",", ":")).encode()
    runner = RecordingRunner(stdout=(b"uv 0.10.12\n", probe_output))

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path is None
    assert preparation.observation.python is None
    assert preparation.observation.lock_status == "unverified"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "environment_evidence_invalid"
    assert preparation.observation.error.message.endswith("invalid: ValueError.")


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("schema_version", True),
        ("implementation", 1),
        ("version", [3, 12, True]),
        ("version", [3, 12, 1.0]),
        ("executable", True),
        ("environment_root", 1),
    ),
)
def test_probe_exact_wrong_field_types_are_rejected_as_malformed(
    tmp_path: Path,
    field: str,
    wrong_value: object,
) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    document = json.loads(
        environment_probe_bytes(
            version=(3, 12, 1),
            executable=plan.root / ".venv/bin/python",
            environment_root=plan.root / ".venv",
        )
    )
    document[field] = wrong_value
    runner = RecordingRunner(
        stdout=(b"uv 0.10.12\n", json.dumps(document, separators=(",", ":")).encode())
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path is None
    assert preparation.observation.python is None
    assert preparation.observation.lock_status == "unverified"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "environment_evidence_invalid"


@pytest.mark.parametrize(
    ("field", "reported_path"),
    (
        ("executable", "relative/bin/python"),
        ("environment_root", "relative/.venv"),
        ("executable", str(Path(os.sep, "tmp", "..", "tmp", "bin/python"))),
        ("environment_root", str(Path(os.sep, "tmp", "..", "tmp", ".venv"))),
    ),
)
def test_relative_or_non_normalized_probe_paths_are_malformed(
    tmp_path: Path,
    field: str,
    reported_path: str,
) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    document = json.loads(
        environment_probe_bytes(
            version=(3, 12, 1),
            executable=plan.root / ".venv/bin/python",
            environment_root=plan.root / ".venv",
        )
    )
    document[field] = reported_path
    runner = RecordingRunner(
        stdout=(b"uv 0.10.12\n", json.dumps(document, separators=(",", ":")).encode())
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path is None
    assert preparation.observation.python is None
    assert preparation.observation.lock_status == "unverified"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "environment_evidence_invalid"


@pytest.mark.parametrize("minor", (10, 11))
def test_cpython_310_and_311_probe_evidence_is_accepted(
    tmp_path: Path,
    minor: int,
) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, minor, 0),
                executable=plan.root / ".venv/bin/python",
                environment_root=plan.root / ".venv",
            ),
        )
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is not None
    assert preparation.prepared.python.version == (3, minor, 0)
    assert preparation.observation.lock_status == "current"


@pytest.mark.parametrize(
    ("implementation", "version"),
    (
        ("pypy", (3, 12, 1)),
        ("cpython", (3, 9, 20)),
        ("cpython", (3, 14, 0)),
    ),
)
def test_unsupported_repository_python_is_rejected(
    tmp_path: Path,
    implementation: str,
    version: tuple[int, int, int],
) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    document = json.loads(
        environment_probe_bytes(
            version=version,
            executable=plan.root / ".venv/bin/python",
            environment_root=plan.root / ".venv",
        )
    )
    document["implementation"] = implementation
    runner = RecordingRunner(
        stdout=(b"uv 0.10.12\n", json.dumps(document, separators=(",", ":")).encode())
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path == plan.root / ".venv"
    assert preparation.observation.python is not None
    assert preparation.observation.python.implementation == implementation
    assert preparation.observation.python.version == version
    assert preparation.observation.lock_status == "current"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "repository_python_unsupported"


@pytest.mark.parametrize(
    ("environment_root_kind", "executable_kind", "expected_code"),
    (
        ("external", "inside", "unsafe_repository_environment"),
        ("nested", "inside_nested", "environment_evidence_invalid"),
        ("exact", "external", "environment_evidence_invalid"),
        ("exact", "root", "environment_evidence_invalid"),
    ),
)
def test_repository_environment_path_contradictions_are_rejected(
    tmp_path: Path,
    environment_root_kind: str,
    executable_kind: str,
    expected_code: str,
) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    roots = {
        "external": tmp_path.parent / "external-venv",
        "nested": plan.root / ".venv/nested",
        "exact": plan.root / ".venv",
    }
    executables = {
        "inside": roots[environment_root_kind] / "bin/python",
        "inside_nested": roots[environment_root_kind] / "bin/python",
        "external": tmp_path.parent / "python",
        "root": plan.root / ".venv",
    }
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 12, 1),
                executable=executables[executable_kind],
                environment_root=roots[environment_root_kind],
            ),
        )
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path == roots[environment_root_kind]
    assert preparation.observation.python is not None
    assert preparation.observation.python.executable == executables[executable_kind]
    assert preparation.observation.lock_status == "current"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == expected_code


def test_symlinked_venv_is_unsafe(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    shutil.rmtree(tmp_path / ".venv")
    external = tmp_path / "external-venv"
    (external / "bin").mkdir(parents=True)
    (external / "bin/python").write_bytes(b"")
    (tmp_path / ".venv").symlink_to(external, target_is_directory=True)
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 12, 1),
                executable=plan.root / ".venv/bin/python",
                environment_root=plan.root / ".venv",
            ),
        )
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path == plan.root / ".venv"
    assert preparation.observation.python is not None
    assert preparation.observation.lock_status == "current"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "unsafe_repository_environment"


def test_lexical_venv_python_symlink_to_managed_interpreter_is_accepted(
    tmp_path: Path,
) -> None:
    write_minimal_uv_project(tmp_path)
    managed = tmp_path / "managed-python"
    managed.write_bytes(b"")
    python = tmp_path / ".venv/bin/python"
    python.unlink()
    python.symlink_to(managed)
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 13, 2),
                executable=plan.root / ".venv/bin/python",
                environment_root=plan.root / ".venv",
            ),
        )
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is not None
    assert preparation.prepared.python.executable == plan.root / ".venv/bin/python"
    assert preparation.prepared.python.executable.resolve() == managed.resolve()


def test_explicit_python_request_contradiction_is_invalid_evidence(tmp_path: Path) -> None:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty", repository_python="3.12.11")
    runner = RecordingRunner(
        stdout=(
            b"uv 0.10.12\n",
            environment_probe_bytes(
                version=(3, 12, 12),
                executable=plan.root / ".venv/bin/python",
                environment_root=plan.root / ".venv",
            ),
        )
    )

    preparation = prepare_repository_environment(
        plan,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert preparation.prepared is None
    assert preparation.observation.path == plan.root / ".venv"
    assert preparation.observation.python is not None
    assert preparation.observation.python.version == (3, 12, 12)
    assert preparation.observation.lock_status == "current"
    assert preparation.observation.error is not None
    assert preparation.observation.error.code == "environment_evidence_invalid"


def _valid_probe(
    tmp_path: Path,
    *,
    uv_version: bytes = b"uv 0.10.12\n",
) -> tuple[RunPlan, RecordingRunner]:
    write_minimal_uv_project(tmp_path)
    plan = focused_plan(tmp_path, "ty")
    runner = RecordingRunner(
        stdout=(
            uv_version,
            environment_probe_bytes(
                version=(3, 12, 11),
                executable=plan.root / ".venv/bin/python",
                environment_root=plan.root / ".venv",
            ),
        )
    )
    return plan, runner


def _external_storage_environment(tmp_path: Path) -> dict[str, str]:
    external = tmp_path / "storage"
    return {
        "HOME": str(external / "home"),
        "UV_CACHE_DIR": str(external / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(external / "python"),
        "UV_PYTHON_CACHE_DIR": str(external / "python-cache"),
        "UV_PYTHON_BIN_DIR": str(external / "bin"),
    }


def _apply_external_storage_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name, value in _external_storage_environment(tmp_path).items():
        monkeypatch.setenv(name, value)


def _remove_overrides_using_base(
    environment: dict[str, str],
    base_variable: str,
) -> None:
    if base_variable in {"HOME", "XDG_CACHE_HOME"}:
        environment.pop("UV_CACHE_DIR", None)
    if base_variable in {"HOME", "XDG_DATA_HOME"}:
        environment.pop("UV_PYTHON_INSTALL_DIR", None)
    if base_variable in {"HOME", "XDG_DATA_HOME", "XDG_BIN_HOME"}:
        environment.pop("UV_PYTHON_BIN_DIR", None)
