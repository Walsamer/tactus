"""Conformance with Ictus's published identity-digest vectors.

Ictus owns the identity-digest protocol and publishes it as
``contracts/digest-vectors.json`` (``schema_version`` 1): a consumer implements
it independently and asserts *all* of the artefact — the digested cases, the
documents the ingress must accept and the documents it must refuse. Passing the
digested cases alone is not conformance: a consumer that hashes what the
protocol refuses makes an unverified proposal look validated.

The mirror below is read from Walsamer/ictus branch
``feat/identity-digest-protocol`` at ``21f3840`` (PR #26). That PR is now merged,
and the pinned revision ``c2aa092`` carries the artefact; the mirror is that same
document, so the vectors are still mirrored inline — the schema metadata, all 19
digested cases, the acceptance and all 10 rejections, i.e. everything the
artefact carries — and both halves of the contract are asserted in CI today.
Asserting the checked-in file in place of this mirror is a follow-up.

A ``payload`` is an object and a ``document`` is text, and only the text is
judged by the literal rule: how this file spells a number inside a ``payload``
(``9007199254740993.0``, whose double ``9007199254740992.0`` does not hold those
digits) is not a spelling any implementation has to accept, while the same
literal in a ``document`` is refused.
"""

import json

import pytest

from tactus.integrations.ictus.digests import (
    DIGEST_INTEGER_MAX,
    DIGEST_INTEGER_MIN,
    DigestNumberDomainError,
    NonFiniteNumberError,
    NumberOutOfDomainError,
    canonical_digest,
    parse_digestable_document,
)

#: Mirrored from ``contracts/digest-vectors.json`` ``schema_version`` 1 on
#: Walsamer/ictus ``feat/identity-digest-protocol`` (PR #26), read at ``21f3840``.
VECTORS = json.loads(
    r"""
{
  "schema_version": 1,
  "algorithm": "sorted-keys-compact-ascii-escaped-cpython-number",
  "hash": "sha256",
  "encoding": "lowercase-hex",
  "cases": [
    {
      "name": "snapshot-minimal",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-1",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t1" },
        "facts": [],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "fd48dd2bb3c4beb848873913fa785dc9446e19c247ffb2db416162f4b9df3b0d"
    },
    {
      "name": "snapshot-key-order",
      "digests": "snapshot",
      "payload": {
        "timestamp": "2026-10-01T00:00:00Z",
        "subject": { "id": "t1", "type": "task" },
        "snapshot_id": "snap-1",
        "schema_version": 1,
        "facts": [],
        "domain": "software",
        "constraints": [],
        "capabilities": []
      },
      "sha256": "fd48dd2bb3c4beb848873913fa785dc9446e19c247ffb2db416162f4b9df3b0d"
    },
    {
      "name": "snapshot-nested",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-nested",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-nested" },
        "facts": [
          {
            "key": "route.candidates",
            "value": [
              {
                "backend": "dagster",
                "rank": 2,
                "facts": { "health": { "state": "READY", "revision": 3 } }
              },
              {
                "backend": "local",
                "rank": 1,
                "facts": { "health": { "revision": 1, "state": "DEGRADED" } }
              }
            ]
          },
          {
            "key": "observation",
            "value": {
              "category": "WORKER_TIMEOUT",
              "attempt": { "semantic": 0, "step_retries": 2 }
            }
          }
        ],
        "capabilities": ["demo.verify"],
        "constraints": ["approval_required"]
      },
      "sha256": "18b469a456f3620938393b587809e84eeb3ecf876145375aadced96037cc3668"
    },
    {
      "name": "snapshot-non-ascii",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-unicode",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-unicode" },
        "facts": [
          { "key": "cl\u00e9", "value": "h\u00e9llo \ud83d\ude00 \u65e5\u672c\u8a9e" },
          { "key": "subject.label", "value": "\u03a9\u2248\u00e7\u221a\u222b\u02dc\u00b5\u2264\u2265\u00f7" }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "00dca3992953ec75cc34a3a90d0ea0bc92f7c55395bc7fb12abe5df3e58e5327"
    },
    {
      "name": "snapshot-escapes",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-escapes",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-escapes" },
        "facts": [
          {
            "key": "note",
            "value": "quote:\" back:\\ slash:/ tab:\t newline:\n cr:\r bs:\b ff:\f nul:\u0000 soh:\u0001 unit:\u001f del:\u007f"
          }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "7a065588951086b1abeed7e1fa317bae05dc85361685930ca25f70bb4b85d346"
    },
    {
      "name": "snapshot-integers-and-floats",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-numbers",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-numbers" },
        "facts": [
          { "key": "int.zero", "value": 0 },
          { "key": "int.one", "value": 1 },
          { "key": "float.one", "value": 1.0 },
          { "key": "int.minus_one", "value": -1 },
          { "key": "int.i64_min", "value": -9223372036854775808 },
          { "key": "int.u64_max", "value": 18446744073709551615 },
          { "key": "int.safe_max", "value": 9007199254740991 },
          { "key": "float.safe_plus_one", "value": 9007199254740993.0 }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "48214a03be6d6dce22745b52643717a4b88f0b78891437316d9e5f3997ac5571"
    },
    {
      "name": "snapshot-float-battery",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-float-battery",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-float-battery" },
        "facts": [
          { "key": "f00", "value": 1.0 },
          { "key": "f01", "value": 1.5 },
          { "key": "f02", "value": 0.1 },
          { "key": "f03", "value": 100.0 },
          { "key": "f04", "value": -0.0 },
          { "key": "f05", "value": 1e15 },
          { "key": "f06", "value": 1e16 },
          { "key": "f07", "value": 1e17 },
          { "key": "f08", "value": 1e21 },
          { "key": "f09", "value": 1e300 },
          { "key": "f10", "value": 1e-4 },
          { "key": "f11", "value": 1e-5 },
          { "key": "f12", "value": 2.5e-10 },
          { "key": "f13", "value": 5e-324 },
          { "key": "f14", "value": 1.7976931348623157e308 },
          { "key": "f15", "value": 0.30000000000000004 },
          { "key": "f16", "value": 123456789012345678.0 },
          { "key": "f17", "value": 1e100 },
          { "key": "f18", "value": 1.2345678901234567e-7 },
          { "key": "f19", "value": 9007199254740993.0 }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "a2fbd2960615d801b33c4d75b7b42274a280e9413ac53a7733904eac8b48ed67"
    },
    {
      "name": "snapshot-float-tie-breaking",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-float-ties",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-float-ties" },
        "facts": [
          { "key": "tie.negative", "value": -847472097840887.2 },
          { "key": "tie.positive", "value": 847472097840887.2 },
          { "key": "third", "value": 0.3333333333333333 },
          { "key": "subnormal", "value": 5e-324 }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "79d45afab37224ee8fadbaa0350b7ad042c02c3d8beb3749bfade98373056710"
    },
    {
      "name": "snapshot-literals",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-literals",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-literals" },
        "facts": [
          { "key": "null", "value": null },
          { "key": "true", "value": true },
          { "key": "false", "value": false },
          { "key": "empty_object", "value": {} },
          { "key": "empty_array", "value": [] },
          { "key": "nested_empty", "value": { "a": [], "b": {} } }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "163681389a84d2e5f37d569eb181927192090a478599dd6797ab2ab29e61d93f"
    },
    {
      "name": "snapshot-duplicate-looking-keys",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-duplicate-looking",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-duplicate-looking" },
        "facts": [
          { "key": "case", "value": { "a": 1, "A": 2 } },
          { "key": "normalization", "value": { "\u00e9": "NFC", "e\u0301": "NFD" } },
          { "key": "casefold", "value": { "KEY": 1, "key": 2, "Key": 3 } }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "7f7df83c426ea591136d491bdac90ef4c0244af71f96f820236f4eed7d3457e5"
    },
    {
      "name": "snapshot-code-point-key-order",
      "digests": "snapshot",
      "payload": {
        "schema_version": 1,
        "snapshot_id": "snap-code-points",
        "timestamp": "2026-10-01T00:00:00Z",
        "domain": "software",
        "subject": { "type": "task", "id": "t-code-points" },
        "facts": [
          {
            "key": "ordering",
            "value": { "\ud83d\ude00": "astral", "\ufffd": "bmp", "\u00e9": "bmp-accented" }
          }
        ],
        "capabilities": [],
        "constraints": []
      },
      "sha256": "d3bc452ffdefea5f62627bfd4e43acfcc56711c423c13ed95c2a4f967d75c9d9"
    },
    {
      "name": "proposal-reexecute-minimal",
      "digests": "proposal",
      "payload": {
        "schema_version": 2,
        "proposal_id": "proposal-1",
        "decision": "REEXECUTE",
        "subject": { "type": "task", "id": "t1" },
        "capability": "demo.verify",
        "provider": { "provider_type": "RULES", "provider_id": "rules.v1" },
        "evidence": [],
        "proposed_at": "2026-10-01T00:00:00Z"
      },
      "sha256": "de292568baba8761d4653a7a9b07d457ddec12170fc34ebe1fd29269d4b8e292"
    },
    {
      "name": "proposal-key-order",
      "digests": "proposal",
      "payload": {
        "proposed_at": "2026-10-01T00:00:00Z",
        "evidence": [],
        "provider": { "provider_id": "rules.v1", "provider_type": "RULES" },
        "capability": "demo.verify",
        "subject": { "id": "t1", "type": "task" },
        "decision": "REEXECUTE",
        "proposal_id": "proposal-1",
        "schema_version": 2
      },
      "sha256": "de292568baba8761d4653a7a9b07d457ddec12170fc34ebe1fd29269d4b8e292"
    },
    {
      "name": "proposal-escalate-no-capability",
      "digests": "proposal",
      "payload": {
        "schema_version": 2,
        "proposal_id": "proposal:recovery:obs-0002",
        "decision": "ESCALATE",
        "subject": { "type": "task", "id": "task-123" },
        "provider": {
          "provider_type": "RULES",
          "provider_id": "rules.v1",
          "provider_version": "0.1.0"
        },
        "reason": "the retry budget is exhausted",
        "evidence": [],
        "proposed_at": "2026-10-01T00:05:00Z"
      },
      "sha256": "9ee156c0dd452092f88a9af5fb1729db844efa352db1d9bb901912e1da69e2dc"
    },
    {
      "name": "proposal-route-arguments",
      "digests": "proposal",
      "payload": {
        "schema_version": 2,
        "proposal_id": "proposal-route-1",
        "decision": "ROUTE",
        "subject": { "type": "task", "id": "task-9" },
        "capability": "demo.verify",
        "route": { "preferred_backend": "dagster", "required_runtime": "python3.12" },
        "arguments": {
          "scope": {
            "paths": ["src/**", "docs/**"],
            "note": "h\u00e9llo \ud83d\ude00"
          },
          "budget": { "steps": 12, "wall_seconds": 600.0, "nested": null, "flags": [true, false] }
        },
        "provider": {
          "provider_type": "RULES",
          "provider_id": "rules.v1",
          "confidence": 0.87
        },
        "evidence": [
          {
            "kind": "test_report",
            "uri": "artifact://report.xml",
            "sha256": "38a4835e821a6e9c7ff1f257b0a7ca4459c0f9d6f7bbcc06d543a31f35eac86e",
            "note": "\u00e7"
          }
        ],
        "proposed_at": "2026-10-01T00:00:00Z"
      },
      "sha256": "23646b22b5e523a2f7f078370fd1328417c2c2d4283b41fd6064d275a67b7e96"
    },
    {
      "name": "intent-minimal",
      "digests": "intent",
      "payload": {
        "schema_version": 1,
        "intent_id": "intent:proposal-0001",
        "capability": "demo.verify",
        "target": { "type": "task", "id": "task-123" },
        "requested_by": { "provider": "rules.v1", "decision_id": "proposal-0001" }
      },
      "sha256": "00d0e595b3a8cfaae4fe81efbd1cb2172b35383270ff3c5796e6e8fffdddf83d"
    },
    {
      "name": "intent-key-order",
      "digests": "intent",
      "payload": {
        "requested_by": { "decision_id": "proposal-0001", "provider": "rules.v1" },
        "target": { "id": "task-123", "type": "task" },
        "capability": "demo.verify",
        "intent_id": "intent:proposal-0001",
        "schema_version": 1
      },
      "sha256": "00d0e595b3a8cfaae4fe81efbd1cb2172b35383270ff3c5796e6e8fffdddf83d"
    },
    {
      "name": "intent-full",
      "digests": "intent",
      "payload": {
        "schema_version": 1,
        "intent_id": "intent:proposal-1",
        "capability": "demo.verify",
        "target": { "type": "task", "id": "task-123" },
        "arguments": {
          "scope": { "paths": ["src/**"], "label": "h\u00e9llo \ud83d\ude00" },
          "limits": { "steps": 12, "wall_seconds": 600.0 },
          "flags": [true, false, null],
          "empty_object": {},
          "empty_array": []
        },
        "policy_context": {
          "snapshot_id": "snap-0002",
          "domain": "software",
          "proposal_digest": "de292568baba8761d4653a7a9b07d457ddec12170fc34ebe1fd29269d4b8e292"
        },
        "requested_by": { "provider": "rules.v1", "decision_id": "proposal-1" }
      },
      "sha256": "30f284eb19a48574b4f8604c5db984247d12b2d504ea1caae541c426a4a08a71"
    },
    {
      "name": "intent-escapes",
      "digests": "intent",
      "payload": {
        "schema_version": 1,
        "intent_id": "intent:escapes",
        "capability": "demo.verify",
        "target": { "type": "task", "id": "task-escapes" },
        "arguments": {
          "note": "quote:\" back:\\ slash:/ tab:\t newline:\n cr:\r nul:\u0000 unit:\u001f del:\u007f"
        },
        "requested_by": { "provider": "rules.v1", "decision_id": "proposal-escapes" }
      },
      "sha256": "197fd9e45fc917e74559aaa51edffbe7c4d914e7167c0e9f87a1e1f9b324fca0"
    }
  ],
  "accepts": [
    {
      "name": "literal-spellings-that-are-their-value",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"f.trailing_zero\",\"value\":0.10},{\"key\":\"f.tiny\",\"value\":1e-05},{\"key\":\"f.upper_exponent\",\"value\":1E5}],\"capabilities\":[],\"constraints\":[]}",
      "sha256": "e1ed67f4ef7f1d10960fd1f682ab1e7bd93f78b1b4cc70c596e45e36d9d271db"
    }
  ],
  "rejections": [
    {
      "name": "integer-above-u64",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"big\",\"value\":18446744073709551616}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "number-out-of-domain",
      "path": "$.facts[0].value"
    },
    {
      "name": "integer-below-i64",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"small\",\"value\":-9223372036854775809}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "number-out-of-domain",
      "path": "$.facts[0].value"
    },
    {
      "name": "intent-integer-above-u64",
      "expect": "refusal",
      "digests": "intent",
      "document": "{\"schema_version\":1,\"intent_id\":\"intent:big\",\"capability\":\"demo.verify\",\"target\":{\"type\":\"task\",\"id\":\"t1\"},\"arguments\":{\"limit\":18446744073709551616},\"requested_by\":{\"provider\":\"rules.v1\",\"decision_id\":\"proposal-1\"}}",
      "reason": "number-out-of-domain",
      "path": "$.arguments.limit"
    },
    {
      "name": "non-finite-literal",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"nan\",\"value\":NaN}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "non-finite",
      "path": "$.facts[0].value"
    },
    {
      "name": "non-finite-infinity",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"infinity\",\"value\":Infinity}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "non-finite",
      "path": "$.facts[0].value"
    },
    {
      "name": "non-finite-negative-infinity",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"neg_infinity\",\"value\":-Infinity}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "non-finite",
      "path": "$.facts[0].value"
    },
    {
      "name": "literal-overflows-to-infinity",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"overflow\",\"value\":1e400}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "non-finite",
      "path": "$.facts[0].value"
    },
    {
      "name": "literal-overflows-to-negative-infinity",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"overflow\",\"value\":-1e400}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "non-finite",
      "path": "$.facts[0].value"
    },
    {
      "name": "literal-would-round",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"rounded\",\"value\":0.1000000000000000055511151}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "number-would-round",
      "path": "$.facts[0].value"
    },
    {
      "name": "literal-would-round-integral",
      "expect": "refusal",
      "digests": "snapshot",
      "document": "{\"schema_version\":1,\"snapshot_id\":\"snap-1\",\"timestamp\":\"2026-10-01T00:00:00Z\",\"domain\":\"software\",\"subject\":{\"type\":\"task\",\"id\":\"t1\"},\"facts\":[{\"key\":\"rounded\",\"value\":9007199254740993.0}],\"capabilities\":[],\"constraints\":[]}",
      "reason": "number-would-round",
      "path": "$.facts[0].value"
    }
  ]
}
"""
)

_CASE_IDS = [case["name"] for case in VECTORS["cases"]]
_ACCEPT_IDS = [case["name"] for case in VECTORS["accepts"]]
_REJECTION_IDS = [case["name"] for case in VECTORS["rejections"]]
#: The refusals a parsed value can still be judged by, without its text.
_WRITER_JUDGED = [
    case for case in VECTORS["rejections"] if case["reason"] != "number-would-round"
]


# -- the artefact ----------------------------------------------------------


def test_the_mirror_is_the_published_protocol():
    assert VECTORS["schema_version"] == 1
    assert VECTORS["algorithm"] == "sorted-keys-compact-ascii-escaped-cpython-number"
    assert VECTORS["hash"] == "sha256"
    assert VECTORS["encoding"] == "lowercase-hex"
    assert len(VECTORS["cases"]) == 19
    assert len(VECTORS["rejections"]) == 10


# -- the easy half: the digested values must not move -----------------------


@pytest.mark.parametrize("case", VECTORS["cases"], ids=_CASE_IDS)
def test_digested_case_is_unchanged(case):
    assert canonical_digest(case["payload"]) == case["sha256"]


# -- the half that protects a binding: refuse what the protocol refuses -----


@pytest.mark.parametrize("case", VECTORS["accepts"], ids=_ACCEPT_IDS)
def test_ingress_accepts_a_document_it_must(case):
    payload = parse_digestable_document(case["document"])
    assert canonical_digest(payload) == case["sha256"]


@pytest.mark.parametrize("case", VECTORS["rejections"], ids=_REJECTION_IDS)
def test_ingress_refuses_a_document_with_the_rule_and_the_path(case):
    assert case["expect"] == "refusal"
    with pytest.raises(DigestNumberDomainError) as refused:
        parse_digestable_document(case["document"])
    assert (refused.value.reason, refused.value.path) == (case["reason"], case["path"])
    assert str(refused.value).startswith(f"{case['reason']} at {case['path']}")


@pytest.mark.parametrize(
    "case", _WRITER_JUDGED, ids=[case["name"] for case in _WRITER_JUDGED]
)
def test_digest_writer_refuses_what_it_can_still_judge(case):
    """The domain edges we used to digest silently now refuse in the writer too."""

    parsed = json.loads(case["document"])  # the permissive parser the domain guards
    with pytest.raises(DigestNumberDomainError) as refused:
        canonical_digest(parsed)
    assert (refused.value.reason, refused.value.path) == (case["reason"], case["path"])


def test_a_literal_that_only_rounds_is_judged_on_the_text_alone():
    """Only the text carries a spelling, which is why the ingress exists.

    The parsed value is a legitimate double, so the writer digests it; the
    ingress refuses the document before a value exists.
    """

    case = next(
        case for case in VECTORS["rejections"] if case["reason"] == "number-would-round"
    )
    assert len(canonical_digest(json.loads(case["document"]))) == 64


# -- the protocol's spellings and edges, beyond the published documents -----


@pytest.mark.parametrize(
    ("literal", "value"),
    [
        ("0.10", 0.1),
        ("1e-05", 1e-05),
        ("1E5", 100000.0),
        ("100.00", 100.0),
        ("-0.0", -0.0),
    ],
)
def test_a_spelling_that_is_its_value_is_accepted(literal, value):
    """The rule is numeric, not textual: these spellings are their values."""

    parsed = parse_digestable_document('{"value":' + literal + "}")
    assert parsed["value"] == value
    assert canonical_digest(parsed) == canonical_digest({"value": value})


def test_an_overflowing_literal_is_non_finite_and_not_a_rounding():
    with pytest.raises(NonFiniteNumberError) as refused:
        parse_digestable_document('{"value":1e400}')
    assert (refused.value.reason, refused.value.path) == ("non-finite", "$.value")


@pytest.mark.parametrize("value", [DIGEST_INTEGER_MIN, DIGEST_INTEGER_MAX])
def test_the_integer_domain_edges_are_inclusive(value):
    assert len(canonical_digest({"value": value})) == 64


@pytest.mark.parametrize("value", [DIGEST_INTEGER_MIN - 1, DIGEST_INTEGER_MAX + 1])
def test_an_integer_outside_the_domain_is_refused_by_the_writer(value):
    with pytest.raises(NumberOutOfDomainError) as refused:
        canonical_digest({"value": value})
    assert refused.value.path == "$.value"


def test_a_boolean_is_not_an_out_of_domain_integer():
    assert len(canonical_digest({"value": True})) == 64


def test_a_non_finite_value_is_refused_by_the_writer():
    with pytest.raises(NonFiniteNumberError) as refused:
        canonical_digest({"value": float("nan")})
    assert (refused.value.reason, refused.value.path) == ("non-finite", "$.value")


def test_the_refusal_names_a_nested_path():
    with pytest.raises(NumberOutOfDomainError) as refused:
        canonical_digest({"facts": [{"key": "big", "value": DIGEST_INTEGER_MAX + 1}]})
    assert refused.value.path == "$.facts[0].value"
