#!/usr/bin/env python3
"""Deterministic agent-instruction and pinned-revision consistency gate.

Fails when the repository's agent instructions or pinned-revision references
drift from the executable source of truth. It is intentionally offline,
dependency-free and order-stable so it can run before publication:

1. **Pinned revision.** ``integrations/ictus/conformance.py``
   (``PINNED_ICTUS_COMMIT``) is authoritative. Every other *pin declaration*
   must carry exactly that commit: the observation module, the CI workflow
   (``ICTUS_COMMIT`` and the pinned ``actions/checkout`` ref), the integration
   README and the architecture docs. Narrative mentions of historical commits
   are ignored; only explicit pin declarations are checked.
2. **Agent instructions.** ``AGENTS.md`` must state the pin, must keep the five
   ownership planes (Tactus, Ictus, Dagster, runtime adapters, Fleet v3) and
   must not contain any other 40-hex commit id.
3. **Read-first references.** Every repo-relative file referenced from the
   ``Read first`` section must exist.

Usage::

    python3 scripts/check-agent-consistency.py            # check this repo
    python3 scripts/check-agent-consistency.py --root DIR  # check a copy

Exit status is 0 when consistent and 1 when any check fails.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SHA = r"[0-9a-f]{40}"
SHA_RE = re.compile(rf"\b({SHA})\b")

CANONICAL_PIN_FILE = Path("integrations/ictus/conformance.py")
CANONICAL_PIN_RE = re.compile(rf'^PINNED_ICTUS_COMMIT = "({SHA})"', re.MULTILINE)

#: (relative path, description, regex) — every match must equal the canonical pin.
PIN_DECLARATIONS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "src/tactus/integrations/ictus/observation.py",
        "observation module constant",
        re.compile(rf'^ICTUS_COMMIT = "({SHA})"', re.MULTILINE),
    ),
    (
        "src/tactus/integrations/ictus/observation.py",
        "observation module docstring",
        re.compile(rf"Ictus commit: ``({SHA})``"),
    ),
    (
        "src/tactus/integrations/ictus/state_snapshot.py",
        "state-snapshot module docstring",
        re.compile(rf"Ictus commit: ``({SHA})``"),
    ),
    (
        ".github/workflows/ci.yml",
        "CI workflow environment",
        re.compile(rf"^\s*ICTUS_COMMIT:\s*({SHA})\s*$", re.MULTILINE),
    ),
    (
        ".github/workflows/ci.yml",
        "CI pinned checkout ref",
        re.compile(rf"^\s*ref:\s*({SHA})\s*$", re.MULTILINE),
    ),
    (
        "integrations/ictus/README.md",
        "integration README pin table",
        re.compile(rf"\|\s*Pinned Ictus commit\s*\|\s*`({SHA})`\s*\|"),
    ),
    (
        "AGENTS.md",
        "agent entry point pin statement",
        re.compile(rf"pins Ictus at `({SHA})`"),
    ),
)

DOC_DECLARATION_RES = (
    re.compile(rf"\|\s*Pinned commit\s*\|\s*`({SHA})`\s*\|"),
    re.compile(rf"Ictus commit: ``({SHA})``"),
    re.compile(rf"conformance is now `({SHA})`"),
    re.compile(rf"pins Ictus at `({SHA})`"),
)

REQUIRED_OWNERSHIP_PLANES = ("**Tactus**", "**Ictus**", "**Dagster**", "**Runtime adapters**", "**Fleet v3**")

READ_FIRST_HEADING = "## Read first"
READ_FIRST_TOKEN_RE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|py|toml|sh|json))`")


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        raise FileNotFoundError(rel)
    return path.read_text(encoding="utf-8")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def canonical_pin(root: Path, failures: list[str]) -> str | None:
    try:
        text = _read(root, str(CANONICAL_PIN_FILE))
    except FileNotFoundError:
        failures.append(f"{CANONICAL_PIN_FILE}: missing (authoritative pin file)")
        return None
    match = CANONICAL_PIN_RE.search(text)
    if not match:
        failures.append(f"{CANONICAL_PIN_FILE}: no PINNED_ICTUS_COMMIT declaration found")
        return None
    return match.group(1)


def check_pin_declarations(root: Path, pin: str, failures: list[str]) -> list[str]:
    """Return human-readable summaries of every declaration that was verified."""
    verified: list[str] = []
    for rel, description, regex in PIN_DECLARATIONS:
        try:
            text = _read(root, rel)
        except FileNotFoundError:
            failures.append(f"{rel}: missing ({description})")
            continue
        matches = list(regex.finditer(text))
        if not matches:
            failures.append(f"{rel}: no {description} found; expected `{pin}`")
            continue
        for match in matches:
            found = match.group(1)
            if found != pin:
                failures.append(
                    f"{rel}:{_line_of(text, match.start())}: {description} pins {found}, "
                    f"expected {pin}"
                )
        verified.append(f"{rel} ({description})")

    docs = sorted((root / "docs" / "architecture").glob("*.md"))
    for path in docs:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(root))
        for regex in DOC_DECLARATION_RES:
            for match in regex.finditer(text):
                found = match.group(1)
                if found != pin:
                    failures.append(
                        f"{rel}:{_line_of(text, match.start())}: pin declaration {found}, "
                        f"expected {pin}"
                    )
                else:
                    verified.append(f"{rel} (architecture doc)")
    return verified


def check_agent_instructions(root: Path, pin: str, failures: list[str]) -> list[str]:
    verified: list[str] = []
    try:
        text = _read(root, "AGENTS.md")
    except FileNotFoundError:
        failures.append("AGENTS.md: missing (agent entry point)")
        return verified

    for plane in REQUIRED_OWNERSHIP_PLANES:
        if plane not in text:
            failures.append(f"AGENTS.md: ownership plane {plane} is not declared")

    stray = sorted({m.group(1) for m in SHA_RE.finditer(text)} - {pin})
    for sha in stray:
        line = _line_of(text, text.find(sha))
        failures.append(f"AGENTS.md:{line}: stale commit id {sha}; only `{pin}` may appear")

    if "scripts/check-agent-consistency.py" not in text:
        failures.append(
            "AGENTS.md: does not reference scripts/check-agent-consistency.py "
            "(stale agent instructions are not documented as gated)"
        )

    read_first_at = text.find(READ_FIRST_HEADING)
    if read_first_at < 0:
        failures.append(f"AGENTS.md: missing `{READ_FIRST_HEADING}` section")
    else:
        tail = text[read_first_at + len(READ_FIRST_HEADING) :]
        next_heading = re.search(r"^## ", tail, re.MULTILINE)
        section = tail[: next_heading.start()] if next_heading else tail
        for token in sorted(set(READ_FIRST_TOKEN_RE.findall(section))):
            if not (root / token).exists():
                failures.append(f"AGENTS.md: Read-first reference `{token}` does not exist")
            else:
                verified.append(f"{token} (read-first reference)")
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="repository root to check (default: this script's repository)",
    )
    parser.add_argument("--quiet", action="store_true", help="print only failures and the verdict")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    failures: list[str] = []

    pin = canonical_pin(root, failures)
    verified: list[str] = []
    if pin is not None:
        verified += check_pin_declarations(root, pin, failures)
        verified += check_agent_instructions(root, pin, failures)

    if not args.quiet:
        for item in verified:
            print(f"AGENT_CONSISTENCY_OK {item}")

    if failures:
        for failure in failures:
            print(f"AGENT_CONSISTENCY_FAIL {failure}", file=sys.stderr)
        print(
            f"AGENT_CONSISTENCY_FAIL {len(failures)} problem(s); "
            "run `python3 scripts/check-agent-consistency.py` and fix before publishing",
            file=sys.stderr,
        )
        return 1

    print(f"AGENT_CONSISTENCY_PASS pin={pin} declarations={len(verified)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
