"""Prepare one locked uv Repository Environment without routing public execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from types import MappingProxyType
from typing import cast

from pyrepo_check.artifact_safety import load_bounded_json, read_regular_file
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    EnvironmentFailureObservation,
    ExecutedProcess,
    LockStatus,
    PreparedRepositoryEnvironment,
    ProcessRunner,
    PythonObservation,
    RepositoryEnvironmentObservation,
    RepositoryLockPresence,
    RepositoryPreparation,
    execute_process,
)
from pyrepo_check.planning import ExplicitRepositoryPython, RunPlan


ENVIRONMENT_PROBE_SOURCE = """import json
import os
import sys

record = {
    "schema_version": 1,
    "implementation": sys.implementation.name,
    "version": list(sys.version_info[:3]),
    "executable": os.path.abspath(os.path.normpath(sys.executable)),
    "environment_root": os.path.abspath(os.path.normpath(sys.prefix)),
}
print(json.dumps(record, separators=(",", ":")))
"""

_REMOVED_ENVIRONMENT_VARIABLES = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "__PYVENV_LAUNCHER__",
    }
)
_ALLOWED_UV_VARIABLES = frozenset(
    {
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
    }
)
_INDEX_CREDENTIAL_PATTERN = re.compile(
    r"^UV_INDEX_[A-Z0-9_]+_(?:USERNAME|PASSWORD)$"
)
_UV_VERSION_PATTERN = re.compile(
    r"^uv ([0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?)(?: \([^\r\n]*\))?\r?\n?$"
)
_STORAGE_OVERRIDES = (
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_CACHE_DIR",
    "UV_PYTHON_BIN_DIR",
)
_UV_CONFIG_LIMIT_BYTES = 1024 * 1024
_BOOLISH_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_BOOLISH_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})
_WINDOWS = os.name == "nt"
_PROBE_KEYS = frozenset(
    {
        "schema_version",
        "implementation",
        "version",
        "executable",
        "environment_root",
    }
)


@dataclass(frozen=True)
class _UvCacheConfiguration:
    no_cache: bool | None = None
    cache_dir: str | None = None


@dataclass(frozen=True)
class _ParsedEnvironmentProbe:
    python: PythonObservation
    environment_root: Path


def sanitized_repository_environment(
    source: Mapping[str, str],
) -> Mapping[str, str]:
    """Remove controller selection overrides and retain exact operational controls."""
    cleaned = {
        name: value
        for name, value in source.items()
        if name not in _REMOVED_ENVIRONMENT_VARIABLES and not name.startswith("UV_")
    }
    cleaned.update(
        {
            name: value
            for name, value in source.items()
            if name in _ALLOWED_UV_VARIABLES
            or _INDEX_CREDENTIAL_PATTERN.fullmatch(name) is not None
        }
    )
    return MappingProxyType(cleaned)


def validate_uv_storage_boundaries(
    root: Path,
    child_environment: Mapping[str, str],
) -> EnvironmentFailureObservation | None:
    """Reject every effective uv writable-storage destination inside the project."""
    resolved_root = root.resolve(strict=False)
    destinations: list[tuple[str, str]] = []
    for variable in _STORAGE_OVERRIDES:
        if variable in child_environment:
            destinations.append((variable, child_environment[variable]))

    cache_destination = _effective_cache_destination(root, child_environment)
    if isinstance(cache_destination, EnvironmentFailureObservation):
        return cache_destination
    destinations.append(cache_destination)
    if "UV_PYTHON_INSTALL_DIR" not in child_environment:
        derived = _python_install_destination(root, child_environment)
        if isinstance(derived, EnvironmentFailureObservation):
            return derived
        destinations.append(derived)
    if "UV_PYTHON_BIN_DIR" not in child_environment:
        derived = _python_bin_destination(child_environment)
        if isinstance(derived, EnvironmentFailureObservation):
            return derived
        if derived is not None:
            destinations.append(derived)

    for authority, raw_destination in destinations:
        destination = Path(raw_destination)
        if not destination.is_absolute():
            return _unsafe_storage_failure(authority, "is not absolute")
        normalized = _normalized_absolute(destination)
        unsafe_reason = _unsafe_destination_reason(resolved_root, normalized)
        if unsafe_reason is not None:
            return _unsafe_storage_failure(authority, unsafe_reason)
    return None


def inspect_repository_lock(root: Path) -> RepositoryLockPresence:
    lock_path = _normalized_absolute(root / "uv.lock")
    try:
        lock_status = lock_path.lstat()
    except FileNotFoundError:
        return RepositoryLockPresence(lock_path, "missing", None)
    except OSError as error:
        return RepositoryLockPresence(
            lock_path,
            "unsafe",
            f"{type(error).__name__}: uv.lock could not be inspected",
        )
    if not stat.S_ISREG(lock_status.st_mode):
        return RepositoryLockPresence(
            lock_path,
            "unsafe",
            "uv.lock is not a regular non-symlink file",
        )
    return RepositoryLockPresence(lock_path, "present", None)


def prepare_repository_environment(
    plan: RunPlan,
    *,
    lock_presence: RepositoryLockPresence | None = None,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
) -> RepositoryPreparation:
    """Prepare and validate one locked Repository Environment."""
    expected_lock = _normalized_absolute(plan.root / "uv.lock")
    presence = lock_presence or inspect_repository_lock(plan.root)
    invalid_presence = _validate_lock_presence(expected_lock, presence)
    if invalid_presence is not None:
        return _failed_preparation(
            plan,
            lock_status="unverified",
            error=invalid_presence,
        )
    if presence.state == "missing":
        return _failed_preparation(
            plan,
            lock_status="missing",
            error=EnvironmentFailureObservation(
                code="repository_lock_missing",
                message="uv.lock is required.",
                hint="Create and commit uv.lock outside pyrepo-check, then retry.",
            ),
        )
    if presence.state == "unsafe":
        return _failed_preparation(
            plan,
            lock_status="unverified",
            error=EnvironmentFailureObservation(
                code="unsafe_repository_environment",
                message="uv.lock could not be safely inspected.",
                hint="Replace uv.lock with a regular non-symlink file, then retry.",
            ),
        )

    child_environment = sanitized_repository_environment(os.environ)
    storage_error = validate_uv_storage_boundaries(plan.root, child_environment)
    if storage_error is not None:
        return _failed_preparation(
            plan,
            lock_status="unverified",
            error=storage_error,
        )

    uv_process = execute_process(
        role="uv_version",
        command=("uv", "--version"),
        cwd=plan.root,
        capture_output=True,
        runner=runner,
        clock_ns=clock_ns,
        environment=child_environment,
    )
    manager_version, uv_error = _parse_uv_version(uv_process)
    if uv_error is not None:
        return _failed_preparation(
            plan,
            lock_status="unverified",
            error=uv_error,
            processes=(uv_process,),
        )

    selector = (
        ()
        if not isinstance(plan.repository_python, ExplicitRepositoryPython)
        else ("--python", plan.repository_python.request)
    )
    probe_process = execute_process(
        role="environment_probe",
        command=(
            "uv",
            "run",
            "--locked",
            *selector,
            "python",
            "-c",
            ENVIRONMENT_PROBE_SOURCE,
        ),
        cwd=plan.root,
        capture_output=True,
        runner=runner,
        clock_ns=clock_ns,
        environment=child_environment,
    )
    processes = (uv_process, probe_process)
    if probe_process.spawn_error is not None or probe_process.returncode is None:
        return _failed_preparation(
            plan,
            lock_status="unverified",
            manager_version=manager_version,
            error=_repository_environment_failed(
                "The locked Repository Environment probe could not start."
            ),
            processes=processes,
        )
    if probe_process.returncode != 0:
        return _failed_preparation(
            plan,
            lock_status="unverified",
            manager_version=manager_version,
            error=_repository_environment_failed(
                "The locked Repository Environment probe did not complete successfully."
            ),
            processes=processes,
        )

    probe, probe_error = _parse_environment_probe(probe_process)
    if probe_error is not None:
        return _failed_preparation(
            plan,
            lock_status="unverified",
            manager_version=manager_version,
            error=probe_error,
            processes=processes,
        )
    if probe is None:
        raise RuntimeError("environment probe parser returned no result")
    semantic_error = _validate_environment_probe(plan, probe)
    if semantic_error is not None:
        return _failed_preparation(
            plan,
            lock_status="current",
            manager_version=manager_version,
            path=probe.environment_root,
            python=probe.python,
            error=semantic_error,
            processes=processes,
        )

    environment_path = probe.environment_root
    prepared = PreparedRepositoryEnvironment(
        root=plan.root,
        path=environment_path,
        python=probe.python,
        python_selection=plan.repository_python,
        manager_version=manager_version,
        child_environment=child_environment,
    )
    return RepositoryPreparation(
        prepared=prepared,
        observation=RepositoryEnvironmentObservation(
            manager_version=manager_version,
            path=environment_path,
            python_selection=plan.repository_python,
            python=probe.python,
            lock_path=expected_lock,
            lock_status="current",
            mutation_protection="unobserved",
            dependencies=(),
            processes=processes,
            error=None,
        ),
    )


def locked_repository_prefix(
    prepared: PreparedRepositoryEnvironment,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--locked",
        "--python",
        str(prepared.python.executable),
        "python",
    )


def _effective_cache_destination(
    root: Path,
    environment: Mapping[str, str],
) -> tuple[str, str] | EnvironmentFailureObservation:
    configuration = _discover_uv_cache_configuration(root, environment)
    if isinstance(configuration, EnvironmentFailureObservation):
        return configuration

    no_cache = configuration.no_cache is True
    if "UV_NO_CACHE" in environment:
        parsed_no_cache = _parse_boolish(environment["UV_NO_CACHE"])
        if parsed_no_cache is None:
            return _unsafe_storage_failure(
                "UV_NO_CACHE", "does not contain a supported Boolean value"
            )
        no_cache = no_cache or parsed_no_cache
    if no_cache:
        return _temporary_cache_destination(environment)

    if "UV_CACHE_DIR" in environment:
        return ("UV_CACHE_DIR", environment["UV_CACHE_DIR"])
    if configuration.cache_dir is not None:
        configured = Path(configuration.cache_dir)
        if not configured.is_absolute():
            configured = root / configured
        return ("uv configuration cache", str(configured))
    return _cache_destination(root, environment)


def _temporary_cache_destination(
    environment: Mapping[str, str],
) -> tuple[str, str] | EnvironmentFailureObservation:
    if os.name == "nt":
        for name in ("TMP", "TEMP", "USERPROFILE"):
            if name in environment:
                value = environment[name]
                invalid = _validate_absolute_base(name, value)
                return invalid or (f"uv temporary cache from {name}", value)
        return _unsafe_storage_failure(
            "uv temporary cache", "has no documented absolute base"
        )
    if "TMPDIR" in environment:
        value = environment["TMPDIR"]
        invalid = _validate_absolute_base("TMPDIR", value)
        return invalid or ("uv temporary cache from TMPDIR", value)
    return ("uv temporary cache fallback", str(Path(os.sep, "tmp")))


def _discover_uv_cache_configuration(
    root: Path,
    environment: Mapping[str, str],
) -> _UvCacheConfiguration | EnvironmentFailureObservation:
    project = _discover_project_uv_configuration(root)
    if isinstance(project, EnvironmentFailureObservation):
        return project
    user_path = _user_uv_configuration_path(environment)
    if isinstance(user_path, EnvironmentFailureObservation):
        return user_path
    user = _read_optional_uv_configuration(
        user_path,
        table_path=(),
        authority="user uv configuration",
    )
    if isinstance(user, EnvironmentFailureObservation):
        return user
    system = _discover_system_uv_configuration(environment)
    if isinstance(system, EnvironmentFailureObservation):
        return system
    return _combine_uv_cache_configurations(project, user, system)


def _discover_project_uv_configuration(
    root: Path,
) -> _UvCacheConfiguration | EnvironmentFailureObservation:
    for directory in (root, *root.parents):
        uv_toml = directory / "uv.toml"
        if _lexically_exists(uv_toml):
            parsed = _read_optional_uv_configuration(
                uv_toml,
                table_path=(),
                authority="project uv configuration",
            )
            if parsed is None:
                raise RuntimeError("existing uv.toml was not parsed")
            return parsed

        pyproject = directory / "pyproject.toml"
        if not _lexically_exists(pyproject):
            continue
        parsed = _read_optional_uv_configuration(
            pyproject,
            table_path=("tool", "uv"),
            authority="project uv configuration",
        )
        if isinstance(parsed, EnvironmentFailureObservation):
            return parsed
        if parsed is not None:
            return parsed
    return _UvCacheConfiguration()


def _user_uv_configuration_path(
    environment: Mapping[str, str],
) -> Path | EnvironmentFailureObservation | None:
    if os.name == "nt":
        base_name = "APPDATA"
        base = environment.get("APPDATA")
    else:
        base_name = "XDG_CONFIG_HOME"
        base = environment.get("XDG_CONFIG_HOME")
        if base is None and "HOME" in environment:
            base_name = "HOME"
            base = str(Path(environment["HOME"]) / ".config")
    if base is None:
        return None
    invalid = _validate_absolute_base(base_name, base)
    if invalid is not None:
        return invalid
    return Path(base) / "uv/uv.toml"


def _discover_system_uv_configuration(
    environment: Mapping[str, str],
) -> _UvCacheConfiguration | EnvironmentFailureObservation | None:
    if _WINDOWS:
        system_drive = environment.get("SYSTEMDRIVE")
        if system_drive is None:
            candidates = ()
        else:
            system_root = Path(f"{system_drive}{os.sep}")
            invalid = _validate_absolute_base("SYSTEMDRIVE", str(system_root))
            if invalid is not None:
                return invalid
            candidates = (system_root / "ProgramData/uv/uv.toml",)
    else:
        raw_directories = environment.get("XDG_CONFIG_DIRS", "/etc/xdg")
        directories = tuple(
            directory for directory in raw_directories.split(os.pathsep) if directory
        )
        for directory in directories:
            invalid = _validate_absolute_base("XDG_CONFIG_DIRS", directory)
            if invalid is not None:
                return invalid
        candidates = tuple(
            Path(directory) / "uv/uv.toml"
            for directory in directories
        ) + (Path("/etc/uv/uv.toml"),)
    for candidate in candidates:
        parsed = _read_optional_uv_configuration(
            candidate,
            table_path=(),
            authority="system uv configuration",
        )
        if parsed is not None:
            return parsed
    return None


def _read_optional_uv_configuration(
    path: Path | None,
    *,
    table_path: tuple[str, ...],
    authority: str,
) -> _UvCacheConfiguration | EnvironmentFailureObservation | None:
    if path is None or not _lexically_exists(path):
        return None
    if not path.is_absolute():
        return _unsafe_storage_failure(authority, "path is not absolute")
    try:
        content = read_regular_file(path, max_bytes=_UV_CONFIG_LIMIT_BYTES)
        document = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return _unsafe_storage_failure(
            authority,
            f"cannot be safely interpreted ({type(error).__name__})",
        )

    table: object = document
    for name in table_path:
        if not isinstance(table, dict) or name not in table:
            return None
        table = table[name]
    if not isinstance(table, dict):
        return _unsafe_storage_failure(authority, "cache settings table is invalid")
    values = cast(dict[str, object], table)
    no_cache = values.get("no-cache")
    cache_dir = values.get("cache-dir")
    if no_cache is not None and type(no_cache) is not bool:
        return _unsafe_storage_failure(authority, "no-cache setting is not Boolean")
    if cache_dir is not None and not isinstance(cache_dir, str):
        return _unsafe_storage_failure(authority, "cache-dir setting is not a string")
    return _UvCacheConfiguration(
        no_cache=no_cache,
        cache_dir=cache_dir,
    )


def _combine_uv_cache_configurations(
    *configurations: _UvCacheConfiguration | None,
) -> _UvCacheConfiguration:
    no_cache: bool | None = None
    cache_dir: str | None = None
    for configuration in configurations:
        if configuration is None:
            continue
        if no_cache is None:
            no_cache = configuration.no_cache
        if cache_dir is None:
            cache_dir = configuration.cache_dir
    return _UvCacheConfiguration(no_cache=no_cache, cache_dir=cache_dir)


def _parse_boolish(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in _BOOLISH_TRUE:
        return True
    if normalized in _BOOLISH_FALSE:
        return False
    return None


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _cache_destination(
    root: Path,
    environment: Mapping[str, str],
) -> tuple[str, str] | EnvironmentFailureObservation:
    legacy = _existing_macos_legacy_uv_cache(environment)
    if isinstance(legacy, EnvironmentFailureObservation):
        return legacy
    if legacy is not None:
        return ("uv legacy cache default from HOME", str(legacy))
    if os.name == "nt":
        base = environment.get("LOCALAPPDATA")
        if base is None:
            return ("uv cache fallback", str(root / ".uv_cache"))
        invalid = _validate_absolute_base("LOCALAPPDATA", base)
        return invalid or ("uv cache default from LOCALAPPDATA", str(Path(base) / "uv/cache"))
    if "XDG_CACHE_HOME" in environment:
        base = environment["XDG_CACHE_HOME"]
        invalid = _validate_absolute_base("XDG_CACHE_HOME", base)
        return invalid or ("uv cache default from XDG_CACHE_HOME", str(Path(base) / "uv"))
    if "HOME" in environment:
        base = environment["HOME"]
        invalid = _validate_absolute_base("HOME", base)
        return invalid or ("uv cache default from HOME", str(Path(base) / ".cache/uv"))
    return ("uv cache fallback", str(root / ".uv_cache"))


def _python_install_destination(
    root: Path,
    environment: Mapping[str, str],
) -> tuple[str, str] | EnvironmentFailureObservation:
    legacy = _existing_macos_legacy_uv_data_root(environment)
    if isinstance(legacy, EnvironmentFailureObservation):
        return legacy
    if legacy is not None:
        return ("uv legacy Python install default from HOME", str(legacy / "python"))
    if os.name == "nt":
        base = environment.get("APPDATA")
        if base is None:
            return ("uv data fallback", str(root / ".uv/python"))
        invalid = _validate_absolute_base("APPDATA", base)
        return invalid or (
            "uv Python install default from APPDATA",
            str(Path(base) / "uv/data/python"),
        )
    if "XDG_DATA_HOME" in environment:
        base = environment["XDG_DATA_HOME"]
        invalid = _validate_absolute_base("XDG_DATA_HOME", base)
        return invalid or (
            "uv Python install default from XDG_DATA_HOME",
            str(Path(base) / "uv/python"),
        )
    if "HOME" in environment:
        base = environment["HOME"]
        invalid = _validate_absolute_base("HOME", base)
        return invalid or (
            "uv Python install default from HOME",
            str(Path(base) / ".local/share/uv/python"),
        )
    return ("uv data fallback", str(root / ".uv/python"))


def _python_bin_destination(
    environment: Mapping[str, str],
) -> tuple[str, str] | EnvironmentFailureObservation | None:
    if "XDG_BIN_HOME" in environment:
        base = environment["XDG_BIN_HOME"]
        invalid = _validate_absolute_base("XDG_BIN_HOME", base)
        return invalid or ("uv Python bin default from XDG_BIN_HOME", base)
    if "XDG_DATA_HOME" in environment:
        base = environment["XDG_DATA_HOME"]
        invalid = _validate_absolute_base("XDG_DATA_HOME", base)
        return invalid or (
            "uv Python bin default from XDG_DATA_HOME",
            str(Path(base) / "../bin"),
        )
    home_name = "USERPROFILE" if os.name == "nt" else "HOME"
    if home_name not in environment:
        return None
    base = environment[home_name]
    invalid = _validate_absolute_base(home_name, base)
    return invalid or (
        f"uv Python bin default from {home_name}",
        str(Path(base) / ".local/bin"),
    )


def _validate_absolute_base(
    name: str,
    value: str,
) -> EnvironmentFailureObservation | None:
    if not Path(value).is_absolute():
        return _unsafe_storage_failure(name, "is not absolute")
    return None


def _existing_macos_legacy_uv_cache(
    environment: Mapping[str, str],
) -> Path | EnvironmentFailureObservation | None:
    if sys.platform != "darwin" or "HOME" not in environment:
        return None
    home = environment["HOME"]
    invalid = _validate_absolute_base("HOME", home)
    if invalid is not None:
        return invalid
    legacy_cache = Path(home) / "Library/Caches/uv"
    return legacy_cache if legacy_cache.exists() else None


def _existing_macos_legacy_uv_data_root(
    environment: Mapping[str, str],
) -> Path | EnvironmentFailureObservation | None:
    if sys.platform != "darwin" or "HOME" not in environment:
        return None
    home = environment["HOME"]
    invalid = _validate_absolute_base("HOME", home)
    if invalid is not None:
        return invalid
    legacy_root = Path(home) / "Library/Application Support/uv"
    return legacy_root if legacy_root.exists() else None


def _unsafe_destination_reason(root: Path, destination: Path) -> str | None:
    if _contained_by(destination, root):
        return "selects the repository or one of its descendants"
    try:
        resolved_destination = destination.resolve(strict=False)
    except (OSError, RuntimeError):
        return "cannot be resolved without a filesystem alias error"
    if _contained_by(resolved_destination, root):
        return "resolves to the repository or one of its descendants"

    try:
        root_status = root.stat()
    except OSError:
        return "cannot be compared with the repository identity"
    current = Path(destination.anchor)
    for component in destination.parts[1:]:
        current /= component
        try:
            current_status = current.lstat()
        except FileNotFoundError:
            break
        except OSError:
            return "has an ancestor that cannot be safely inspected"
        if (current_status.st_dev, current_status.st_ino) == (
            root_status.st_dev,
            root_status.st_ino,
        ):
            return "has a filesystem ancestor identical to the repository"
        try:
            if os.path.samefile(current, root):
                return "has a filesystem ancestor aliased to the repository"
        except OSError:
            pass
        try:
            resolved_current = current.resolve(strict=False)
        except (OSError, RuntimeError):
            return "has an ancestor with an unsafe filesystem alias"
        if _contained_by(resolved_current, root):
            return "has an ancestor resolving inside the repository"
    return None


def _unsafe_storage_failure(
    authority: str,
    reason: str,
) -> EnvironmentFailureObservation:
    return EnvironmentFailureObservation(
        code="unsafe_repository_environment",
        message=f"{authority} {reason}.",
        hint="Choose an absolute uv storage directory outside the repository.",
    )


def _validate_lock_presence(
    expected_lock: Path,
    presence: RepositoryLockPresence,
) -> EnvironmentFailureObservation | None:
    if presence.state not in {"missing", "present", "unsafe"}:
        return EnvironmentFailureObservation(
            code="unsafe_repository_environment",
            message="Repository lock observation state is invalid.",
            hint="Inspect uv.lock for the selected repository root, then retry.",
        )
    try:
        observed_path = _normalized_absolute(presence.path).resolve(strict=False)
        expected_path = expected_lock.resolve(strict=False)
    except (OSError, RuntimeError):
        observed_path = Path()
        expected_path = expected_lock
    if observed_path != expected_path:
        return EnvironmentFailureObservation(
            code="unsafe_repository_environment",
            message="Repository lock observation does not belong to the selected project.",
            hint="Inspect uv.lock for the selected repository root, then retry.",
        )
    return None


def _parse_uv_version(
    process: ExecutedProcess,
) -> tuple[str, EnvironmentFailureObservation | None]:
    if process.spawn_error is not None or process.returncode is None:
        return "", EnvironmentFailureObservation(
            code="uv_unavailable",
            message="uv is unavailable to the Tool Environment.",
            hint="Install uv on the Tool Environment PATH, then retry.",
        )
    if process.returncode != 0:
        return "", EnvironmentFailureObservation(
            code="uv_unavailable",
            message="uv --version did not complete successfully.",
            hint="Repair the uv executable available to the Tool Environment, then retry.",
        )
    content = _complete_stdout(process)
    if content is None:
        return "", _invalid_evidence("uv --version output is missing or truncated.")
    try:
        output = content.decode("utf-8")
    except UnicodeDecodeError:
        return "", _invalid_evidence("uv --version output is not valid UTF-8.")
    match = _UV_VERSION_PATTERN.fullmatch(output)
    if match is None:
        return "", _invalid_evidence("uv --version output is malformed.")
    return match.group(1), None


def _parse_environment_probe(
    process: ExecutedProcess,
) -> tuple[_ParsedEnvironmentProbe | None, EnvironmentFailureObservation | None]:
    content = _complete_stdout(process)
    if content is None:
        return None, _invalid_evidence(
            "Repository Environment probe output is missing or truncated."
        )
    try:
        document = load_bounded_json(content, max_nesting=8)
    except (UnicodeDecodeError, ValueError) as error:
        return None, _invalid_evidence(
            f"Repository Environment probe output is invalid: {type(error).__name__}."
        )
    if not isinstance(document, dict) or set(document) != _PROBE_KEYS:
        return None, _invalid_evidence(
            "Repository Environment probe fields do not match schema version 1."
        )
    document = cast(dict[str, object], document)
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        return None, _invalid_evidence(
            "Repository Environment probe schema version is invalid."
        )
    implementation = document["implementation"]
    version_value = document["version"]
    executable_value = document["executable"]
    environment_root_value = document["environment_root"]
    if not isinstance(implementation, str):
        return None, _invalid_evidence(
            "Repository Environment implementation evidence is invalid."
        )
    if (
        not isinstance(version_value, list)
        or len(version_value) != 3
        or any(type(part) is not int for part in version_value)
    ):
        return None, _invalid_evidence(
            "Repository Environment Python version evidence is invalid."
        )
    if not isinstance(executable_value, str) or not isinstance(
        environment_root_value, str
    ):
        return None, _invalid_evidence(
            "Repository Environment path evidence is invalid."
        )
    if "\x00" in executable_value or "\x00" in environment_root_value:
        return None, _invalid_evidence(
            "Repository Environment path evidence contains an invalid character."
        )

    executable = _normalized_reported_path(executable_value)
    environment_root = _normalized_reported_path(environment_root_value)
    if executable is None or environment_root is None:
        return None, _invalid_evidence(
            "Repository Environment paths are not normalized absolute paths."
        )
    version_parts = cast(list[int], version_value)
    return (
        _ParsedEnvironmentProbe(
            python=PythonObservation(
                implementation=implementation,
                version=(version_parts[0], version_parts[1], version_parts[2]),
                executable=executable,
            ),
            environment_root=environment_root,
        ),
        None,
    )


def _validate_environment_probe(
    plan: RunPlan,
    probe: _ParsedEnvironmentProbe,
) -> EnvironmentFailureObservation | None:
    python = probe.python
    version = python.version
    if python.implementation != "cpython" or not (
        version[0] == 3 and 10 <= version[1] <= 13 and version[2] >= 0
    ):
        return EnvironmentFailureObservation(
            code="repository_python_unsupported",
            message="Repository Python must be CPython 3.10 through 3.13.",
            hint="Select a supported Repository Python and update uv.lock explicitly.",
        )
    contradiction = _explicit_python_contradiction(plan, version)
    if contradiction is not None:
        return contradiction

    expected_root = _normalized_absolute(plan.root / ".venv")
    environment_root = probe.environment_root
    if environment_root != expected_root:
        if expected_root in environment_root.parents:
            return _invalid_evidence(
                "Repository Environment root is nested beneath the required .venv."
            )
        return EnvironmentFailureObservation(
            code="unsafe_repository_environment",
            message="Repository Environment root is outside the project .venv.",
            hint="Use the real non-symlink .venv at the selected repository root.",
        )
    try:
        environment_status = expected_root.lstat()
    except OSError:
        return EnvironmentFailureObservation(
            code="unsafe_repository_environment",
            message="Repository Environment .venv cannot be safely inspected.",
            hint="Create a real non-symlink .venv through locked uv preparation.",
        )
    if not stat.S_ISDIR(environment_status.st_mode):
        return EnvironmentFailureObservation(
            code="unsafe_repository_environment",
            message="Repository Environment .venv is not a real non-symlink directory.",
            hint="Replace .venv with a real directory, then retry.",
        )
    executable = python.executable
    if executable == expected_root or expected_root not in executable.parents:
        return _invalid_evidence(
            "Repository Python executable is not lexically contained in .venv."
        )
    return None


def _explicit_python_contradiction(
    plan: RunPlan,
    version: tuple[int, ...],
) -> EnvironmentFailureObservation | None:
    if not isinstance(plan.repository_python, ExplicitRepositoryPython):
        return None
    requested = tuple(int(part) for part in plan.repository_python.request.split("."))
    if version[: len(requested)] == requested:
        return None
    return _invalid_evidence(
        "Repository Python evidence contradicts the explicit Python request."
    )


def _complete_stdout(process: ExecutedProcess) -> bytes | None:
    if process.stdout is None or process.stdout.omitted_bytes != 0:
        return None
    if len(process.stdout.tail) > CAPTURE_LIMIT_BYTES:
        return None
    return process.stdout.tail


def _invalid_evidence(message: str) -> EnvironmentFailureObservation:
    return EnvironmentFailureObservation(
        code="environment_evidence_invalid",
        message=message,
        hint="Retry with a supported uv executable and an unchanged locked repository.",
    )


def _repository_environment_failed(message: str) -> EnvironmentFailureObservation:
    return EnvironmentFailureObservation(
        code="repository_environment_failed",
        message=message,
        hint="Run uv lock --check and repair the lock outside pyrepo-check, then retry.",
    )


def _failed_preparation(
    plan: RunPlan,
    *,
    lock_status: LockStatus,
    error: EnvironmentFailureObservation,
    manager_version: str | None = None,
    path: Path | None = None,
    python: PythonObservation | None = None,
    processes: tuple[ExecutedProcess, ...] = (),
) -> RepositoryPreparation:
    return RepositoryPreparation(
        prepared=None,
        observation=RepositoryEnvironmentObservation(
            manager_version=manager_version,
            path=path,
            python_selection=plan.repository_python,
            python=python,
            lock_path=_normalized_absolute(plan.root / "uv.lock"),
            lock_status=lock_status,
            mutation_protection="unobserved",
            dependencies=(),
            processes=processes,
            error=error,
        ),
    )


def _normalized_reported_path(raw_path: str) -> Path | None:
    path = Path(raw_path)
    if not path.is_absolute():
        return None
    normalized = _normalized_absolute(path)
    return normalized if str(normalized) == raw_path else None


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(path)))


def _contained_by(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents
