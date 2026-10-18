"""Regression tests for the agent-instruction / pin consistency gate.

The gate exists so stale agent instructions or a drifted Ictus pin are caught
before publication instead of being discovered later in a merged branch. These
tests pin the *behaviour* of the checker without duplicating the pin itself:
the expected commit is always read from the authoritative
``PINNED_ICTUS_COMMIT`` declaration.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check-agent-consistency.py"
COPIED_PATHS = ("AGENTS.md", ".github", "integrations", "src", "docs")
_PIN_RE = re.compile(r'PINNED_ICTUS_COMMIT = "([0-9a-f]{40})"')
FAKE_COMMIT = "f" * 40


def canonical_pin() -> str:
    text = (REPO_ROOT / "integrations" / "ictus" / "conformance.py").read_text(encoding="utf-8")
    match = _PIN_RE.search(text)
    assert match is not None, "authoritative pin declaration is missing"
    return match.group(1)


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root), "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )


def repo_copy(tmp_path: Path) -> Path:
    for rel in COPIED_PATHS:
        source = REPO_ROOT / rel
        target = tmp_path / rel
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return tmp_path


def test_repository_agent_instructions_are_consistent() -> None:
    result = run(REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "AGENT_CONSISTENCY_PASS" in result.stdout


def test_stale_pin_in_agent_instructions_is_detected(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    agents = root / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    agents.write_text(text.replace(canonical_pin(), FAKE_COMMIT), encoding="utf-8")

    result = run(root)

    assert result.returncode == 1
    assert "AGENTS.md" in result.stderr
    assert FAKE_COMMIT in result.stderr


def test_diverging_observation_module_pin_is_detected(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    module = root / "src" / "tactus" / "integrations" / "ictus" / "observation.py"
    text = module.read_text(encoding="utf-8")
    module.write_text(text.replace(canonical_pin(), FAKE_COMMIT), encoding="utf-8")

    result = run(root)

    assert result.returncode == 1
    assert "observation.py" in result.stderr
    assert FAKE_COMMIT in result.stderr


def test_ci_workflow_pin_drift_is_detected(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    workflow = root / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    workflow.write_text(text.replace(canonical_pin(), FAKE_COMMIT), encoding="utf-8")

    result = run(root)

    assert result.returncode == 1
    assert "ci.yml" in result.stderr


def test_missing_ownership_plane_is_detected(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    agents = root / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    assert "**Dagster**" in text
    agents.write_text(text.replace("**Dagster**", "Dagster"), encoding="utf-8")

    result = run(root)

    assert result.returncode == 1
    assert "**Dagster**" in result.stderr


def test_broken_read_first_reference_is_detected(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    agents = root / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    agents.write_text(
        text.replace(
            "docs/architecture/CROSS_SYSTEM_CONTRACTS.md",
            "docs/architecture/NOT_A_REAL_DOC.md",
        ),
        encoding="utf-8",
    )

    result = run(root)

    assert result.returncode == 1
    assert "NOT_A_REAL_DOC.md" in result.stderr
