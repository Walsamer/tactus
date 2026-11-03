"""Tactus's Dagster code location for Ictus's two software routes.

This module defines no steps or jobs of its own: it registers Ictus's
``software_agent_job`` and ``software_publish_job`` without replacing their
prepare/execute/verify/finalize graphs. Dagster records the execution plan from
Ictus's job definition and replays the recorded step selection, so a location
that declared its own graph could not receive that run at all.

How the seam is selected, and why it is shaped this way
-------------------------------------------------------
The runtime seam is operator-owned deployment configuration. ``TACTUS_RUNTIME_SEAM``
names the seam configuration file; unset or blank means ``None``, which keeps
Ictus's exact fail-closed placeholder failure. There is no default path and no
intent-derived runtime selection.

It has to be a **class-level** default on the settings resource, not merely an
instance value. ``ictus_dagster/submission.py::_run_config`` always writes
``{"resources": {"software_agent_settings": {"config": {"capability": ...,
"placeholder": true}}}}``, and a run-config document replaces a
``ConfigurableResource``'s instance values wholesale, falling back to the class
defaults. An instance default is therefore wiped by the bridge's own run config,
and a class default is what survives it.

Re-binding the resource changes the resulting job's execution-plan snapshot id
(the snapshot embeds the resource definition), but it does not stop the run: a
run recorded against Ictus's job was executed end to end against a job rebound
this way during this change, with the step keys intact and the class default
arriving in the step. That was measured, not assumed — see the change's report.

``software.promote`` keeps Ictus's own ``software_publish_settings`` default,
which is ``runtime_seam=None``. The seam names one backend for both routes, and
the only backend Tactus has edits a workspace; pointing promotion at it would
report a successful promotion with nothing published. Until a publication
backend exists, promotion stays fail-closed exactly as before.

The configured ``agent_backend:create_backend`` uses Tactus's supervisor, not
``control.spawn``. Ictus's control cannot observe those child processes; our own
confirmed termination is therefore the termination account, not independent
confirmation by Ictus.

What this registration does not establish: worker execution under a real
deployment, delivery of a mid-attempt cancellation (the pinned contract offers
none), containment, or publication safety.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dagster import Definitions
from ictus_dagster.jobs.software_agent_job import (
    SoftwareAgentSettings,
    software_agent_job,
)
from ictus_dagster.jobs.software_publish_job import software_publish_job

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _import_root in (_HERE, _REPO_ROOT / "src"):  # loaded by path (`python_file`)
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

# Load the backend while this bootstrap makes its directory importable: Ictus's
# worker resolves ``agent_backend:create_backend`` lazily, by name, at run time.
import agent_backend  # noqa: F401, E402

#: Named by the operator, never by an intent. Read once, when the code location
#: loads, so it belongs in the daemon's workspace environment — not the bridge's,
#: which is a different process and never imports this module.
_RUNTIME_SEAM = os.environ.get("TACTUS_RUNTIME_SEAM", "").strip() or None


class TactusSoftwareAgentSettings(SoftwareAgentSettings):
    """Ictus's software-agent settings with the operator's seam as a class default.

    The class is the point. A run-config document written by the bridge replaces
    the resource instance's values and falls back to *class* defaults, so a value
    set only on the instance never reaches a durably submitted run.
    """

    runtime_seam: str | None = _RUNTIME_SEAM


# Re-binding the resource keeps Ictus's graph and puts the seam where the run
# config cannot erase it. ``with_top_level_resources`` wants resource
# *definitions*, not configurable-resource instances.
software_agent_job = software_agent_job.with_top_level_resources(
    {
        "software_agent_settings": TactusSoftwareAgentSettings().get_resource_definition()
    }
)

defs = Definitions(jobs=[software_agent_job, software_publish_job])
