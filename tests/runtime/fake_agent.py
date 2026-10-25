#!/usr/bin/env python3
"""Deterministic fake coding agent used by the runtime-supervision tests.

It is a real process (that is the point: supervision, timeouts, cancellation and
process-tree termination are only meaningful against real children). It reads the
request JSON the supervisor writes and behaves according to ``MODE``.

Modes
-----
success        write a valid result + patch, exit 0
crash          exit 3 without artifacts
timeout        sleep far beyond the supervisor deadline
ignore-term    spawn a grandchild that ignores SIGTERM, then sleep
unterminated   like ignore-term (used with an injected non-confirming terminator)
bad-scope      report a changed path outside the allowed scope
wide-patch     produce a patch larger than the configured limit
huge-result    produce a result.json larger than the configured limit
undeclared     patch touches a file the result did not declare
no-result      exit 0 without writing anything
failed         report ``status: failed`` with a reason, exit 0
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PATCH = """--- a/src/example.py
+++ b/src/example.py
@@ -1,2 +1,3 @@
 def hello():
-    return "hello"
+    return "hello, world"
+    # fake agent edit
"""


def _version() -> int:
    print("fake-agent 1.0.0")
    return 0


def _write(output_dir: Path, *, status: str, changed, summary: str) -> None:
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "changed_paths": list(changed),
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
        print("usage: fake_agent.py MODE REQUEST", file=sys.stderr)
        return 2
    mode, request_path = argv[0], Path(argv[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output_dir = Path(request["output_dir"])

    if mode == "crash":
        print("fake agent crashed", file=sys.stderr)
        return 3
    if mode == "timeout":
        time.sleep(120)
        return 0
    if mode in {"ignore-term", "unterminated"}:
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('child ready', flush=True)\ntime.sleep(120)"],
            start_new_session=False,
        )
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(f"grandchild pid={child.pid}", flush=True)
        time.sleep(120)
        return 0
    if mode == "no-result":
        return 0
    if mode == "failed":
        _write(output_dir, status="failed", changed=[], summary="agent gave up")
        return 0
    if mode == "bad-scope":
        _write(output_dir, status="completed", changed=["../outside.py"], summary="escaped")
        (output_dir / "patch.diff").write_text(PATCH, encoding="utf-8")
        return 0
    if mode == "undeclared":
        _write(output_dir, status="completed", changed=["src/example.py"], summary="poke")
        (output_dir / "patch.diff").write_text(
            PATCH + "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-a\n+b\n",
            encoding="utf-8",
        )
        return 0
    if mode == "huge-result":
        (output_dir / "result.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "completed",
                    "changed_paths": ["src/example.py"],
                    "summary": "x" * 5000,
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "patch.diff").write_text(PATCH, encoding="utf-8")
        return 0
    if mode == "wide-patch":
        _write(output_dir, status="completed", changed=["src/example.py"], summary="wide")
        body = "".join(f"+line {index}\n" for index in range(20000))
        (output_dir / "patch.diff").write_text(
            f"--- a/src/example.py\n+++ b/src/example.py\n@@ -1,1 +1,20000 @@\n{body}",
            encoding="utf-8",
        )
        return 0

    # success
    _write(output_dir, status="completed", changed=["src/example.py"], summary="edited")
    (output_dir / "patch.diff").write_text(PATCH, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
