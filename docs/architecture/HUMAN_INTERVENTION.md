# Human Intervention

## Purpose

Human intervention handles cases where the system has enough information to know it should not proceed autonomously, but does not have authority or information to resolve the situation itself.

Human intervention is **not** a parallel lifecycle/state machine. The Work Order remains in the normal Tactus lifecycle, usually:

```text
OPEN + BLOCKED(reason=HUMAN_INPUT_REQUIRED)
```

with a structured intervention request attached. `BLOCKED` here is the readiness
status of the `OPEN` lifecycle state, not a lifecycle state.

## Initial intervention reasons

```text
APPROVAL_REQUIRED
CLARIFICATION_REQUIRED
SECRET_REQUIRED
EXTERNAL_INPUT_REQUIRED
MANUAL_SELECTION_REQUIRED
UNRESOLVED_RECOVERY
SCOPE_CHANGE_REQUIRED
OTHER
```

Secrets must never be embedded directly in a Work Order, issue, log, or intervention comment. A secret request should reference the required secret/capability by identifier and use an approved secret mechanism.

## Human actions

The whiteboard calls out interventions such as retrying, splitting/replanning, and checking/changing backend choice. Normalize those into the same typed action vocabulary used by automation.

Examples:

```text
RETRY
REQUEUE_READY
REROUTE
SPLIT_REPLAN
RETIRE
APPROVE
REJECT
PROVIDE_CLARIFICATION
MARK_DEPENDENCY_RESOLVED
```

The human should not directly issue arbitrary database mutations such as “set readiness = READY” or “set lifecycle state = ACTIVE”. Instead:

```text
human action
   ↓
validated command / Ictus policy check where required
   ↓
Tactus transition
```

## Intervention record

A minimal intervention request should carry:

```yaml
id: ...
work_order_id: ...
reason: CLARIFICATION_REQUIRED
summary: ...
requested_action: ...
allowed_actions:
  - PROVIDE_CLARIFICATION
  - RETIRE
created_at: ...
evidence: ...
status: OPEN
```

Resolution should preserve:

```text
who/what resolved it
selected action
provided non-secret input/reference
when it was resolved
resulting lifecycle transition
```

## Human decisions remain policy-bound

Some operator actions may be immediately safe; others still require policy/capability validation.

Examples:

- retry within existing scope: usually safe to validate and apply;
- widen repository/file scope: requires explicit policy/authorization;
- provide a secret reference: requires secret-handling policy;
- switch backend: must still satisfy capability/policy requirements;
- split work: children must remain within parent authorization or require new approval.

## Collaboration surface

For collaborative repositories, GitHub Issues/PRs can be the human-facing discussion surface, while Tactus keeps the operational intervention record.

Do not make GitHub comments the only source of truth for intervention state.

Conceptually:

```text
Tactus OPEN + BLOCKED intervention
        ↓
optional GitHub status/comment/link
        ↓
human response/action
        ↓
validated Tactus command
        ↓
normal lifecycle transition
```

## Unblocking

A human response does not automatically imply `READY`.

After the requested input/action is supplied, Tactus re-evaluates all blockers and prerequisites. Only if no blocker remains does the Work Order's `OPEN` readiness become `READY`.
