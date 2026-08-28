"""Standalone launcher for a staged, controller-verified Coverage package."""

from __future__ import annotations

import os
import runpy
import sys


def main(arguments: list[str]) -> int:
    if len(arguments) < 3 or arguments[0] != "--module-root" or arguments[2] != "--":
        return 120
    module_root = os.path.abspath(os.path.normpath(arguments[1]))
    coverage_arguments = arguments[3:]
    sys.path[0] = module_root
    sys.argv = ["coverage", *coverage_arguments]
    sys.orig_argv = [sys.executable, "-m", "coverage", *coverage_arguments]
    runpy.run_module("coverage", run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
