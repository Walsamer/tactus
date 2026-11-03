"""Conformance with Ictus's published identity-digest vectors.

Ictus owns the identity-digest protocol and publishes it as
``contracts/digest-vectors.json`` (``schema_version`` 1): a consumer implements
it independently and asserts *all* of the artefact — the digested cases, the
documents the ingress must accept and the documents it must refuse. Passing the
digested cases alone is not conformance: a consumer that hashes what the
protocol refuses makes an unverified proposal look validated.

Every case below is read from that file in the checkout at
``PINNED_ICTUS_COMMIT``: the schema metadata, all 19 digested cases, the accepted
document and all 10 rejections — everything the artefact carries — so a case
Ictus adds or changes reaches this file without anyone copying it here, and a
machine without the checkout fails loudly instead of passing on a stale mirror.

A ``payload`` is an object and a ``document`` is text, and only the text is
judged by the literal rule: how this file spells a number inside a ``payload``
(``9007199254740993.0``, whose double ``9007199254740992.0`` does not hold those
digits) is not a spelling any implementation has to accept, while the same
literal in a ``document`` is refused.
"""

import json

import pytest

import seam_support

from tactus.integrations.ictus.digests import (
    DIGEST_INTEGER_MAX,
    DIGEST_INTEGER_MIN,
    DigestNumberDomainError,
    NonFiniteNumberError,
    NumberOutOfDomainError,
    canonical_digest,
    parse_digestable_document,
)

#: The published artefact, asserted in place from the Ictus checkout at the pin.
#: It is never vendored here and never shadowed by a hand-written mirror: the
#: file Ictus ships is the contract, so a moved layout or a missing checkout
#: raises from :func:`seam_support.load_pinned_artifact` instead of skipping.
VECTORS = seam_support.load_pinned_artifact("contracts/digest-vectors.json")

_CASE_IDS = [case["name"] for case in VECTORS["cases"]]
_ACCEPT_IDS = [case["name"] for case in VECTORS["accepts"]]
_REJECTION_IDS = [case["name"] for case in VECTORS["rejections"]]
#: The refusals a parsed value can still be judged by, without its text.
_WRITER_JUDGED = [
    case for case in VECTORS["rejections"] if case["reason"] != "number-would-round"
]


# -- the artefact ----------------------------------------------------------


def test_the_artefact_carries_the_protocol_this_consumer_implements():
    assert VECTORS["schema_version"] == 1
    assert VECTORS["algorithm"] == "sorted-keys-compact-ascii-escaped-cpython-number"
    assert VECTORS["hash"] == "sha256"
    assert VECTORS["encoding"] == "lowercase-hex"
    # Counts are asserted, not assumed: an artefact that silently lost cases is a
    # contract change, and this file must be revisited deliberately.
    assert len(VECTORS["cases"]) == 19
    assert len(VECTORS["accepts"]) == 1
    assert len(VECTORS["rejections"]) == 10
    assert {case["digests"] for case in VECTORS["cases"]} == {
        "snapshot",
        "proposal",
        "intent",
    }


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
