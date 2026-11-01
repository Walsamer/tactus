"""Explicit local Tactus WorkSource command.

This is the deliberate, human-invoked import entry point. It runs entirely
offline against a deterministic JSON fixture, persists immutable source
provenance in a local SQLite database and prints a machine-readable summary.

Usage::

    uv run python -m tactus.cli import-issue \
        --source tests/integrations/github/data/issue_21.json \
        --database /tmp/tactus-work-source.sqlite \
        --repository walsamer/tactus --issue 21
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from tactus.application import import_issue, recheck_source
from tactus.integrations.github import MockGitHubIssueSource, load_issue_payload
from tactus.persistence import SqliteWorkSource


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tactus", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    import_command = subcommands.add_parser(
        "import-issue", help="import one checked GitHub Issue revision"
    )
    import_command.add_argument("--source", required=True, help="local JSON fixture path")
    import_command.add_argument("--database", required=True, help="SQLite database path")
    import_command.add_argument("--repository", required=True, help="owner/name")
    import_command.add_argument("--issue", type=int, required=True, help="Issue number")
    import_command.add_argument("--work-order-id", default=None)
    import_command.add_argument(
        "--check",
        action="store_true",
        help="recheck authorization/freshness after import and report typed holds",
    )

    publish_command = subcommands.add_parser(
        "publish",
        help="publish one authorized review branch and pull request",
        description=(
            "Open exactly one review pull request for a verified prepared change, "
            "authorized by a durable operator grant. Prints redacted publication "
            "evidence as JSON. It never merges, never force-pushes and never "
            "pushes the trunk."
        ),
    )
    publish_command.add_argument("--grant", required=True, help="durable human grant id")
    publish_command.add_argument(
        "--work-order", required=True, help="Work Order the grant was recorded for"
    )
    publish_command.add_argument(
        "--prepared-change", required=True, help="recorded prepared change id"
    )
    publish_command.add_argument(
        "--database", required=True, help="SQLite database holding the human grant"
    )
    publish_command.add_argument(
        "--state-root",
        required=True,
        help="absolute state root holding preparation manifests and publication records",
    )
    publish_command.add_argument(
        "--repository", required=True, help="canonical owner/name to publish into"
    )
    publish_command.add_argument(
        "--repository-root",
        required=True,
        help="absolute path of the git checkout the review branch is pushed from",
    )
    publish_command.add_argument(
        "--token-file",
        required=True,
        help="absolute path outside the repository holding the fine-grained token",
    )
    publish_command.add_argument(
        "--base", default="main", help="authorized base branch (default: main)"
    )
    return parser


def _import_issue(args: argparse.Namespace) -> int:
    payload = load_issue_payload(args.source)
    source = MockGitHubIssueSource({(args.repository, args.issue): payload})
    store = SqliteWorkSource(args.database)
    try:
        result = import_issue(
            source,
            store,
            args.repository,
            args.issue,
            work_order_id=args.work_order_id,
        )
        summary: dict[str, object] = {
            "created": result.created,
            "work_order_id": str(result.work_order_id),
            "import_key": result.provenance.import_key.value,
            "body_sha256": result.provenance.revision.body_sha256,
            "specification_digest": result.provenance.revision.specification_digest,
            "source_revision": result.provenance.revision.source_revision.value,
            "imported_at": result.provenance.imported_at.isoformat(),
            "holds": [],
        }
        if args.check:
            holds = recheck_source(source, store, result.work_order_id)
            summary["holds"] = [
                {"reason": hold.reason.value, "detail": hold.detail} for hold in holds
            ]
    finally:
        store.close()

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _publish(args: argparse.Namespace) -> int:
    """Compose the durable facts and publish once, printing redacted evidence."""

    from tactus.application.publish_change import publish_change
    from tactus.integrations.github.gh import GhGitHubTransport
    from tactus.integrations.stax.source_control import load_prepared_change
    from tactus.persistence import SqliteHumanIntervention
    from tactus.publication import (
        CredentialPreflightError,
        FileTokenCredentialProvider,
        GitHubLivePublicationPort,
    )
    from tactus.publication.grant_projection import GrantProjectionError

    def refuse(kind: str, detail: str) -> int:
        print(
            json.dumps(
                {"publication": "refused", "error": kind, "detail": detail},
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        change = load_prepared_change(args.state_root, args.prepared_change)
    except Exception as exc:  # noqa: BLE001 - operator-facing typed refusal
        return refuse(type(exc).__name__, str(exc))

    store = SqliteHumanIntervention(args.database)
    try:
        grants = store.grants_for(args.work_order, include_revoked=True)
    finally:
        store.close()
    grant = next((item for item in grants if item.grant_id == args.grant), None)
    if grant is None:
        return refuse(
            "unknown_grant",
            f"no durable grant {args.grant!r} for WorkOrder {args.work_order!r}",
        )

    try:
        provider = FileTokenCredentialProvider(
            args.token_file,
            repository=args.repository,
            repository_root=args.repository_root,
        )
        transport = GhGitHubTransport(
            source_path=args.repository_root,
            authorized_base_branch=args.base,
            authorized_base_revision=change.base_revision,
        )
        port = GitHubLivePublicationPort(
            transport, provider, state_root=args.state_root
        )
    except (CredentialPreflightError, ValueError) as exc:
        return refuse(type(exc).__name__, str(exc))

    try:
        result = publish_change(
            grant=grant,
            change=change,
            repository=args.repository,
            destination=args.base,
            port=port,
        )
    except (GrantProjectionError, ValueError) as exc:
        return refuse(type(exc).__name__, str(exc))

    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.publication_succeeded else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "import-issue":
        return _import_issue(args)
    if args.command == "publish":
        return _publish(args)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
