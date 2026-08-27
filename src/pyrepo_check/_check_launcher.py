"""Standalone Repository Check launcher; this module imports no pyrepo-check code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import traceback


_MAX_EVIDENCE_BYTES = 4_096


def argument_digest(arguments: list[str]) -> str:
    digest = hashlib.sha256()
    for argument in arguments:
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def publish_start(path: Path, check: str, module: str, arguments: list[str]) -> None:
    payload = {
        "schema_version": 1,
        "check": check,
        "module": module,
        "arguments_sha256": argument_digest(arguments),
        "python": {
            "implementation": sys.implementation.name,
            "version": list(sys.version_info[:3]),
            "executable": os.path.abspath(os.path.normpath(sys.executable)),
        },
    }
    content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(content) > _MAX_EVIDENCE_BYTES:
        raise RuntimeError("start evidence exceeds 4096 bytes")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("start evidence write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def dispatch(evidence: Path, check: str, module: str, arguments: list[str]) -> int:
    sys.path[0] = os.getcwd()
    sys.argv = [module, *arguments]
    sys.orig_argv = [sys.executable, "-m", module, *arguments]
    publish_start(evidence, check, module, arguments)
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 120
    return 0


def parse_arguments(arguments: list[str]) -> tuple[Path, str, str, list[str]] | None:
    if len(arguments) < 7:
        return None
    if (
        arguments[0] != "--evidence"
        or arguments[2] != "--check"
        or arguments[4] != "--module"
        or arguments[6] != "--"
        or not arguments[1]
        or not arguments[3]
        or not arguments[5]
    ):
        return None
    return Path(arguments[1]), arguments[3], arguments[5], arguments[7:]


def main(arguments: list[str]) -> int:
    parsed = parse_arguments(arguments)
    if parsed is None:
        return 120
    evidence, check, module, module_arguments = parsed
    try:
        return dispatch(evidence, check, module, module_arguments)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 120


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
