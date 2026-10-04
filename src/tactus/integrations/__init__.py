"""Tactus integration boundaries with external planes.

This package contains anti-corruption / compatibility boundaries. It does not
re-implement external systems; it validates and normalizes their versioned
contracts at the edge.

The only boundary implemented so far is the **inbound** Ictus
``ExecutionObservation`` v1 validation/translation boundary
(:mod:`tactus.integrations.ictus`). Execution observations are produced by Ictus
and consumed by Tactus; Tactus never fabricates or originates them.
"""

__all__: list[str] = []
