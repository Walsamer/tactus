from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tactus.backends import (
    BackendHealth,
    BackendHealthModel,
    BackendId,
    BackendStatusObservation,
)

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(minutes=1)


def test_health_vocabulary_is_exactly_three_values() -> None:
    assert {member.value for member in BackendHealth} == {
        "AVAILABLE",
        "UNAVAILABLE",
        "DISABLED",
    }


def test_observation_freshness() -> None:
    authoritative = BackendStatusObservation(
        BackendId("generic-a"), BackendHealth.DISABLED, observed_at=_T0
    )
    expiring = BackendStatusObservation(
        BackendId("generic-a"), BackendHealth.AVAILABLE, observed_at=_T0, expires_at=_T1
    )

    assert authoritative.is_fresh_at(_T0)
    assert authoritative.is_fresh_at(_T1 + timedelta(days=1))
    assert expiring.is_fresh_at(_T0)
    assert not expiring.is_fresh_at(_T1)


def test_no_fresh_status_is_none_not_unavailable() -> None:
    model = BackendHealthModel()

    assert model.effective_status("generic-a", at=_T0) is None


def test_effective_status_returns_fresh_status() -> None:
    model = BackendHealthModel()
    model.record(
        BackendStatusObservation(
            BackendId("generic-a"), BackendHealth.AVAILABLE, observed_at=_T0, expires_at=_T1
        )
    )

    assert model.effective_status("generic-a", at=_T0) is BackendHealth.AVAILABLE


def test_expiry_is_evaluated_at_query_time_and_history_is_retained() -> None:
    model = BackendHealthModel()
    observation = BackendStatusObservation(
        BackendId("generic-a"), BackendHealth.AVAILABLE, observed_at=_T0, expires_at=_T1
    )
    model.record(observation)

    assert model.effective_status("generic-a", at=_T1) is None
    assert model.history("generic-a") == (observation,)


def test_expires_at_none_is_authoritative_until_superseded() -> None:
    model = BackendHealthModel()
    model.record(
        BackendStatusObservation(
            BackendId("generic-a"), BackendHealth.DISABLED, observed_at=_T0
        )
    )

    assert model.effective_status("generic-a", at=_T0 + timedelta(days=365)) is (
        BackendHealth.DISABLED
    )


def test_latest_observation_wins() -> None:
    model = BackendHealthModel()
    model.record(
        BackendStatusObservation(BackendId("generic-a"), BackendHealth.AVAILABLE, _T0)
    )
    model.record(
        BackendStatusObservation(
            BackendId("generic-a"), BackendHealth.UNAVAILABLE, _T0 + timedelta(minutes=5)
        )
    )

    assert model.effective_status("generic-a", at=_T0) is BackendHealth.UNAVAILABLE


def test_out_of_order_observation_does_not_overwrite_newer_state() -> None:
    model = BackendHealthModel()
    newer = BackendStatusObservation(
        BackendId("generic-a"), BackendHealth.UNAVAILABLE, _T0 + timedelta(minutes=5)
    )
    older = BackendStatusObservation(
        BackendId("generic-a"), BackendHealth.AVAILABLE, _T0
    )
    model.record(newer)
    model.record(older)

    assert model.latest_observation("generic-a") is newer
    assert model.effective_status("generic-a", at=_T0) is BackendHealth.UNAVAILABLE
    assert model.history("generic-a") == (newer, older)


def test_disable_supersedes_availability() -> None:
    model = BackendHealthModel()
    model.record(
        BackendStatusObservation(BackendId("generic-a"), BackendHealth.AVAILABLE, _T0)
    )
    model.record(
        BackendStatusObservation(
            BackendId("generic-a"),
            BackendHealth.DISABLED,
            _T0 + timedelta(minutes=1),
            reason="operator disabled",
        )
    )

    assert model.effective_status("generic-a", at=_T0) is BackendHealth.DISABLED
