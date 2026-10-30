"""Bounded, explicit failure control for the Tactus local capability location.

This module is the *test-only* control surface for
``integrations/dagster/definitions.py``. It answers exactly one question:

    "should this attempt of this capability fail?"

and it answers it from a caller-supplied, bounded file — never from the
Ictus-authorized ``ExecutionIntent``.

Why it exists
-------------
``TCT-WO-005`` needs deterministic transient and terminal execution failures
without weakening the authorization boundary. The pinned Ictus revision issues
``ExecutionIntent`` values whose ``arguments`` come from Ictus's own policy
provider (``crates/policy/src/intent.rs`` copies ``proposal.arguments``; the
rules provider only ever sets ``recovery_outcome``). Tactus must therefore not
inject ``fail_until_attempt`` / ``fail_hard`` into an authorized intent.

In production a capability simply *fails*; nobody injects the failure. This
module reproduces that shape faithfully: the authorized intent is untouched and
the **capability implementation** fails, driven by configuration that lives
outside the intent and outside the policy boundary.

Invariants enforced here
------------------------
* The control file is optional. Absent ⇒ no injected failure, ever.
* Unknown intent ids get no programme (no accidental cross-scenario leakage).
* Unknown keys, unknown versions, out-of-range attempts and the intent-argument
  spellings (``fail_until_attempt``, ``fail_hard`` inside an intent) are
  rejected loudly rather than silently ignored.
* Nothing in this module reads or writes an ``ExecutionIntent``.

The module is deliberately dependency-free (no Dagster import) so it can be
unit-tested offline and by the normal ``pytest`` gate.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "CONTROL_VERSION",
    "CONTROL_PATH_ENV",
    "ATTEMPT_LOG_ENV",
    "MAX_INJECTED_ATTEMPTS",
    "FailureControlError",
    "FailureProgramme",
    "control_path",
    "load_programmes",
    "programme_for_intent",
    "record_attempt",
]


#: Environment variable naming the JSON control file. Unset/empty ⇒ inert.
CONTROL_PATH_ENV = "TACTUS_DAGSTER_FAILURE_CONTROL"
#: Environment variable naming an append-only JSONL attempt ledger.
ATTEMPT_LOG_ENV = "TACTUS_DAGSTER_ATTEMPT_LOG"

CONTROL_VERSION = 1

#: The location's execute step declares ``RetryPolicy(max_retries=3)``, so one
#: run can attempt the step four times. Failing "attempts 1..N" only stays
#: meaningful (i.e. can be made to *stop* failing) while N < 4.
MAX_INJECTED_ATTEMPTS = 3

_ALLOWED_KEYS = frozenset({"fail_attempts", "fail_hard", "exit_code"})

#: Highest exit status the capability subprocess may be told to return. The
#: program is a bounded local `python -c`, so anything above 125 is a signal,
#: not an application exit status.
MAX_INJECTED_EXIT_CODE = 125

#: Keys that belong to an intent's ``arguments`` in a *different* design. They
#: are rejected here on purpose: seeing one means somebody is trying to smuggle
#: execution knobs through the authorization boundary again.
_REJECTED_INTENT_ARGUMENT_KEYS = frozenset(
    {"fail_until_attempt", "fail_verify", "hold_seconds"}
)


class FailureControlError(RuntimeError):
    """The failure-control configuration is unusable (fail closed)."""


@dataclass(frozen=True)
class FailureProgramme:
    """How one intent's capability implementation should fail.

    Three mutually exclusive shapes:

    * ``fail_attempts=N`` — the execute step fails attempts 1..N, then succeeds
      (Dagster retries; the run succeeds).
    * ``fail_hard=True`` — the execute step fails every attempt, before the
      capability work starts (the run is recorded as failed).
    * ``exit_code=N`` — the capability *subprocess* runs and returns a non-zero
      status (the run is recorded as failed after real process work).

    An empty programme injects nothing: the capability subprocess runs normally
    and exits 0.
    """

    fail_attempts: int = 0
    fail_hard: bool = False
    exit_code: int = 0

    @property
    def injects_failure(self) -> bool:
        return self.fail_hard or self.fail_attempts > 0 or self.exit_code > 0

    def fails_on(self, attempt: int) -> bool:
        """Return whether the 1-based ``attempt`` must fail before the subprocess."""
        if self.fail_hard:
            return True
        return 1 <= attempt <= self.fail_attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "fail_attempts": self.fail_attempts,
            "fail_hard": self.fail_hard,
            "exit_code": self.exit_code,
        }


NO_FAILURE = FailureProgramme()


def control_path() -> Path | None:
    """Return the configured control file, or ``None`` when injection is off."""
    raw = (os.environ.get(CONTROL_PATH_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def _parse_programme(intent_id: str, raw: Any) -> FailureProgramme:
    if not isinstance(raw, Mapping):
        raise FailureControlError(
            f"failure programme for intent {intent_id!r} must be a JSON object"
        )
    unknown = set(raw) - _ALLOWED_KEYS
    rejected = unknown & _REJECTED_INTENT_ARGUMENT_KEYS
    if rejected:
        raise FailureControlError(
            f"failure programme for intent {intent_id!r} carries intent-argument key(s) "
            f"{sorted(rejected)}; execution knobs must never travel inside an "
            "authorized ExecutionIntent"
        )
    if unknown:
        raise FailureControlError(
            f"failure programme for intent {intent_id!r} carries unknown key(s) "
            f"{sorted(unknown)}; allowed keys are {sorted(_ALLOWED_KEYS)}"
        )
    if not raw:
        raise FailureControlError(
            f"failure programme for intent {intent_id!r} is empty; omit the entry instead"
        )

    fail_hard = raw.get("fail_hard", False)
    if not isinstance(fail_hard, bool):
        raise FailureControlError(
            f"failure programme for intent {intent_id!r}: fail_hard must be a boolean"
        )

    fail_attempts = raw.get("fail_attempts", 0)
    if isinstance(fail_attempts, bool) or not isinstance(fail_attempts, int):
        raise FailureControlError(
            f"failure programme for intent {intent_id!r}: fail_attempts must be an integer"
        )
    if not 0 <= fail_attempts <= MAX_INJECTED_ATTEMPTS:
        raise FailureControlError(
            f"failure programme for intent {intent_id!r}: fail_attempts must be within "
            f"0..{MAX_INJECTED_ATTEMPTS} (the execute step retries at most three times)"
        )

    exit_code = raw.get("exit_code", 0)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise FailureControlError(
            f"failure programme for intent {intent_id!r}: exit_code must be an integer"
        )
    if not 0 <= exit_code <= MAX_INJECTED_EXIT_CODE:
        raise FailureControlError(
            f"failure programme for intent {intent_id!r}: exit_code must be within "
            f"0..{MAX_INJECTED_EXIT_CODE}"
        )

    shapes = [fail_hard, fail_attempts > 0, exit_code > 0]
    if sum(shapes) > 1:
        raise FailureControlError(
            f"failure programme for intent {intent_id!r} mixes failure shapes "
            "(fail_hard / fail_attempts / exit_code); choose exactly one"
        )
    if not any(shapes):
        raise FailureControlError(
            f"failure programme for intent {intent_id!r} injects nothing; omit the entry "
            "to run the capability normally"
        )
    return FailureProgramme(
        fail_attempts=fail_attempts, fail_hard=fail_hard, exit_code=exit_code
    )


def load_programmes(path: Path | os.PathLike[str]) -> dict[str, FailureProgramme]:
    """Load and strictly validate a control file into intent → programme."""
    file = Path(path)
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FailureControlError(f"failure control file is missing: {file}") from exc
    except json.JSONDecodeError as exc:
        raise FailureControlError(f"failure control file is not valid JSON: {exc}") from exc

    if not isinstance(document, Mapping):
        raise FailureControlError("failure control file must contain a JSON object")
    if document.get("version") != CONTROL_VERSION:
        raise FailureControlError(
            f"failure control file must declare version {CONTROL_VERSION} "
            f"(got {document.get('version')!r})"
        )
    unknown = set(document) - {"version", "intents"}
    if unknown:
        raise FailureControlError(
            f"failure control file carries unknown top-level key(s) {sorted(unknown)}"
        )
    intents = document.get("intents")
    if not isinstance(intents, Mapping):
        raise FailureControlError("failure control file must contain an 'intents' object")

    programmes: dict[str, FailureProgramme] = {}
    for raw_intent_id, raw_programme in intents.items():
        intent_id = str(raw_intent_id).strip()
        if not intent_id:
            raise FailureControlError("failure control file contains an empty intent id")
        if intent_id in programmes:
            raise FailureControlError(f"duplicate intent id {intent_id!r} in control file")
        programmes[intent_id] = _parse_programme(intent_id, raw_programme)
    return programmes


def programme_for_intent(intent_id: str) -> FailureProgramme:
    """Resolve the programme for one run's intent id (never raises for absence).

    A missing/unreadable control file means "no injection". A *malformed*
    control file always raises: silently running the capability successfully
    would turn a configuration mistake into a false green scenario.
    """
    path = control_path()
    if path is None:
        return NO_FAILURE
    programmes = load_programmes(path)
    return programmes.get(str(intent_id), NO_FAILURE)


def record_attempt(
    *,
    intent_id: str,
    attempt: int,
    programme: FailureProgramme,
    outcome: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append one capability attempt to the evidence ledger (best effort).

    The ledger is written by the capability implementation, in the Dagster
    daemon's process, keyed by the intent id. It is evidence only and never
    feeds a decision.
    """
    raw = (os.environ.get(ATTEMPT_LOG_ENV) or "").strip()
    if not raw:
        return
    entry: dict[str, Any] = {
        "intent_id": intent_id,
        "attempt": attempt,
        "outcome": outcome,
        "programme": programme.as_dict(),
    }
    if details is not None:
        entry["details"] = dict(details)
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
