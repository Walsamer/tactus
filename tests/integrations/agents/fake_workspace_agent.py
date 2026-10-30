#!/usr/bin/env python3
"""Deterministic fake coding agent that edits a disposable workspace.

The purpose is to exercise the adapter contract against a *real* child process
and a *real* on-disk change, without invoking Pi, Codex, a model provider or any
credentialed service.  The agent reads the supervisor's bounded request JSON and
edits ``src/example.py`` in the request's workspace, then emits the matching
bounded ``patch.diff`` and ``result.json``.

Modes
-----
edit          edit the workspace file, write a real diff + result, exit 0
crash         exit 3 without artifacts
timeout       sleep far beyond the supervisor deadline
bad-scope     report a changed path outside the allowed scope
"""

from __future__ import annotations

import difflib
import json
import os
import sys
import time
from pathlib import Path

ORIGINAL = 'def hello():\n    return "hello"\n'
UPDATED = 'def hello():\n    return "hello, world"\n    # fake agent edit\n'


def _version() -> int:
    print("fake-workspace-agent 1.0.0")
    return 0


def _patch() -> str:
    return "".join(
        difflib.unified_diff(
            ORIGINAL.splitlines(keepends=True),
            UPDATED.splitlines(keepends=True),
            fromfile="a/src/example.py",
            tofile="b/src/example.py",
        )
    )


def _write_result(
    output_dir: Path,
    *,
    status: str,
    changed_paths: list[str],
    summary: str,
) -> None:
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "changed_paths": changed_paths,
                "summary": summary,
                "env_keys": sorted(os.environ),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    if "--version" in argv:
        return _version()
    if len(argv) < 2:
        print("usage: fake_workspace_agent.py MODE REQUEST", file=sys.stderr)
        return 2
    mode, request_path = argv[0], Path(argv[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output_dir = Path(request["output_dir"])
    workspace = Path(request["workspace"]["root"])

    if mode == "crash":
        print("fake workspace agent crashed", file=sys.stderr)
        return 3
    if mode in {"timeout", "cancel"}:
        time.sleep(120)
        return 0
    if mode == "bad-scope":
        _write_result(
            output_dir,
            status="completed",
            changed_paths=["../outside.py"],
            summary="reported an escaped path",
        )
        (output_dir / "patch.diff").write_text(_patch(), encoding="utf-8")
        return 0

    # edit: a real workspace mutation plus the matching real unified diff.
    target = workspace / "src" / "example.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(UPDATED, encoding="utf-8")
    (output_dir / "patch.diff").write_text(_patch(), encoding="utf-8")
    _write_result(
        output_dir,
        status="completed",
        changed_paths=["src/example.py"],
        summary="edited one file in the disposable workspace",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
