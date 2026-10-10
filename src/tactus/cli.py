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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "import-issue":
        return _import_issue(args)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
