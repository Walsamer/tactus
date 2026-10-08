from __future__ import annotations

import pytest

from tactus.backends import (
    BackendDescriptor,
    BackendId,
    BackendRegistry,
    BackendRequirements,
    DuplicateBackendError,
    EffortLevel,
    UnknownBackendError,
)


def test_backend_id_requires_non_empty_value() -> None:
    with pytest.raises(ValueError):
        BackendId("")
    with pytest.raises(ValueError):
        BackendId("   ")


def test_backend_id_strips_whitespace() -> None:
    assert BackendId("  generic-a  ").value == "generic-a"


def test_register_and_get() -> None:
    descriptor = BackendDescriptor(BackendId("generic-a"))
    registry = BackendRegistry([descriptor])

    assert registry.get("generic-a") == descriptor
    assert registry.get(BackendId("generic-a")) == descriptor


def test_duplicate_registration_is_rejected() -> None:
    registry = BackendRegistry([BackendDescriptor(BackendId("generic-a"))])

    with pytest.raises(DuplicateBackendError):
        registry.register(BackendDescriptor(BackendId("generic-a")))


def test_unknown_backend_is_rejected() -> None:
    registry = BackendRegistry()

    with pytest.raises(UnknownBackendError):
        registry.get("missing")


def test_all_is_deterministically_ordered() -> None:
    registry = BackendRegistry(
        [
            BackendDescriptor(BackendId("generic-c")),
            BackendDescriptor(BackendId("generic-a")),
            BackendDescriptor(BackendId("generic-b")),
        ]
    )

    assert [descriptor.backend_id.value for descriptor in registry.all()] == [
        "generic-a",
        "generic-b",
        "generic-c",
    ]


def test_compatible_matches_required_capabilities() -> None:
    registry = BackendRegistry(
        [
            BackendDescriptor(
                BackendId("generic-a"), capabilities=frozenset({"shell", "git"})
            ),
            BackendDescriptor(BackendId("generic-b"), capabilities=frozenset({"shell"})),
        ]
    )

    compatible = registry.compatible(
        BackendRequirements(required_capabilities=frozenset({"shell", "git"}))
    )

    assert [descriptor.backend_id.value for descriptor in compatible] == ["generic-a"]


def test_compatible_treats_optional_fields_as_hard_constraints() -> None:
    registry = BackendRegistry(
        [
            BackendDescriptor(
                BackendId("generic-a"),
                agent_runtime="runtime-x",
                model="model-x",
                provider="provider-x",
                effort=EffortLevel.HIGH,
            ),
            BackendDescriptor(
                BackendId("generic-b"),
                agent_runtime="runtime-x",
                model="model-x",
                provider="provider-x",
                effort=EffortLevel.LOW,
            ),
        ]
    )

    compatible = registry.compatible(
        BackendRequirements(
            agent_runtime="runtime-x",
            model="model-x",
            provider="provider-x",
            effort=EffortLevel.HIGH,
        )
    )

    assert [descriptor.backend_id.value for descriptor in compatible] == ["generic-a"]


def test_descriptor_preserves_supplied_constraints() -> None:
    descriptor = BackendDescriptor(
        BackendId("generic-a"), constraints=frozenset({"workspace:repo"})
    )

    assert descriptor.constraints == frozenset({"workspace:repo"})


def test_unspecified_requirement_is_unconstrained() -> None:
    registry = BackendRegistry(
        [
            BackendDescriptor(BackendId("generic-a"), effort=EffortLevel.HIGH),
            BackendDescriptor(BackendId("generic-b"), effort=None),
        ]
    )

    compatible = registry.compatible(BackendRequirements())

    assert [descriptor.backend_id.value for descriptor in compatible] == [
        "generic-a",
        "generic-b",
    ]


def test_requirement_of_effort_excludes_backends_without_effort() -> None:
    registry = BackendRegistry([BackendDescriptor(BackendId("generic-a"), effort=None)])

    compatible = registry.compatible(BackendRequirements(effort=EffortLevel.HIGH))

    assert compatible == ()


def test_compatible_is_pure_and_order_independent() -> None:
    """compatible() is a mechanical match; repeated calls are identical."""

    registry = BackendRegistry(
        [
            BackendDescriptor(BackendId("generic-a"), capabilities=frozenset({"shell"})),
            BackendDescriptor(BackendId("generic-b"), capabilities=frozenset({"git"})),
        ]
    )
    requirements = BackendRequirements(required_capabilities=frozenset({"shell"}))

    first = registry.compatible(requirements)
    second = registry.compatible(requirements)

    assert first == second
