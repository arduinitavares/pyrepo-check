"""Standalone launcher for a staged, controller-verified Coverage package."""

from __future__ import annotations

import sys


_PLATFORM_MODULE_NAME = "nt" if "nt" in sys.builtin_module_names else "posix"
_PLATFORM = __import__(_PLATFORM_MODULE_NAME)
_SCRIPT_DIRECTORY = sys.path[0] if sys.path else ""
_REPOSITORY = _PLATFORM.getcwd()


def _lexical_path_key(value: str) -> str:
    key = value.rstrip("/\\")
    return key.casefold() if _PLATFORM_MODULE_NAME == "nt" else key


_UNSAFE_INITIAL_PATHS = {
    _lexical_path_key(_SCRIPT_DIRECTORY),
    _lexical_path_key(_REPOSITORY),
}
sys.path[:] = [
    entry
    for index, entry in enumerate(sys.path)
    if index != 0 and entry and _lexical_path_key(entry) not in _UNSAFE_INITIAL_PATHS
]

import os  # noqa: E402


_WORKSPACE = os.path.abspath(os.path.normpath(os.path.dirname(__file__)))
_REPOSITORY = os.path.abspath(os.path.normpath(_REPOSITORY))
sys.path[:] = [
    entry
    for entry in sys.path
    if os.path.abspath(os.path.normpath(entry)) not in {_WORKSPACE, _REPOSITORY}
]

import runpy  # noqa: E402


def main(arguments: list[str]) -> int:
    if (
        len(arguments) < 5
        or arguments[0] != "--module-root"
        or arguments[2] != "--dependency-root"
        or arguments[4] != "--"
    ):
        return 120
    module_root = os.path.abspath(os.path.normpath(arguments[1]))
    dependency_root = os.path.abspath(os.path.normpath(arguments[3]))
    coverage_arguments = arguments[5:]
    sys.path[:] = [
        module_root,
        *(
            entry
            for entry in sys.path
            if os.path.abspath(os.path.normpath(entry))
            not in {module_root, dependency_root}
        ),
        dependency_root,
    ]
    sys.argv = ["coverage", *coverage_arguments]
    sys.orig_argv = [sys.executable, "-m", "coverage", *coverage_arguments]
    runpy.run_module("coverage", run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
