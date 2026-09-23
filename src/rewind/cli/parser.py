"""Argument definitions.

Separate from :mod:`~rewind.cli.commands` so that the shape of the interface can be read,
reviewed and tested without loading anything that talks to AWS. Help text is part of the
product here: a command that reads state says so, and ``revert`` says plainly that it does
nothing without ``--confirm``.
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..domain import RETENTION_DAYS
from ..pipeline import DEFAULT_LOOKBACK_DAYS
from .codes import EXIT_CONFLICT


def _add_window_arguments(
    parser: argparse.ArgumentParser, identity_required: bool = True
) -> None:
    """Shared window options.

    ``identity_required`` is the one difference between ``scan`` and ``plan``, and it is a
    safety boundary rather than a convenience: see the note on ``scan`` below.
    """
    parser.add_argument(
        "--identity",
        required=identity_required,
        metavar="NAME",
        help="workload identity to attribute changes to; matched against the CloudTrail "
        "userIdentity userName, ARN, principalId and role-session name"
        + ("" if identity_required else ". Omit it to see every identity in the window"),
    )
    parser.add_argument(
        "--since",
        metavar="DURATION",
        help="look back this far from now, e.g. 90m, 2h, 3d",
    )
    parser.add_argument("--start", metavar="ISO8601", help="window start, e.g. 2026-09-22T17:22:13Z")
    parser.add_argument("--end", metavar="ISO8601", help="window end; defaults to now")
    parser.add_argument(
        "--region",
        metavar="REGION",
        help="region to query; defaults to the ambient AWS region configuration",
    )
    parser.add_argument(
        "--output",
        choices=["table", "json"],
        default="table",
        help="output format (default: table)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rewind",
        description="Find configuration changes one identity made, work out what each "
        "field held beforehand, and produce a reviewable revert plan. Uses CloudTrail "
        "event history and read-only Describe calls; creates nothing in your account.",
    )
    parser.add_argument("--version", action="version", version="rewind %s" % __version__)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    scan_parser = subparsers.add_parser(
        "scan",
        help="list the changes made in a window (CloudTrail only)",
        description="Read-only. Shows what each call set, without resolving previous values. "
        "--identity is optional here: omit it to find out which identities changed anything, "
        "which is the question you usually have before you can answer it.",
    )
    _add_window_arguments(scan_parser, identity_required=False)
    scan_parser.add_argument(
        "--detail",
        action="store_true",
        help="with no --identity, list every change instead of one row per identity. "
        "Already the default when --identity is given",
    )

    plan_parser = subparsers.add_parser(
        "plan",
        help="resolve previous values and write a revert plan",
        description="Read-only. Groups changes per (resource, field), resolves the value "
        "held before the session's first change to each field, and describes the revert.\n"
        "--identity is required, unlike `scan`: an unfiltered plan would collect changes made "
        "by AWS service-linked roles and describe reverts for them.",
    )
    _add_window_arguments(plan_parser, identity_required=True)
    plan_parser.add_argument(
        "-o", "--out", metavar="FILE", help="write the plan document to this file"
    )
    plan_parser.add_argument(
        "--explain",
        action="store_true",
        help="show the evidence behind every resolved value",
    )
    plan_parser.add_argument(
        "--set",
        metavar="SELECTOR=VALUE",
        action="append",
        dest="assignments",
        help="supply a previous value the tool cannot prove, e.g. "
        "--set i-0abc.instanceType=t3.micro or --set chn-abc123=t3.micro. Recorded as "
        "ASSERTED, never as proof, and only ever used to fill an UNKNOWN. Repeatable.",
    )
    plan_parser.add_argument(
        "--snapshot",
        metavar="FILE",
        help="a snapshot file from `rewind snapshot`, used as evidence when it predates "
        "the change",
    )
    plan_parser.add_argument(
        "--use-config",
        action="store_true",
        help="also consult AWS Config configuration history, which reaches further back "
        "than CloudTrail's 90 days. Probed first; skipped with a reason if the account "
        "has no recorder.",
    )
    plan_parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        metavar="N",
        help="how far back to search for evidence (default and maximum: %d, the "
        "CloudTrail event history retention)" % RETENTION_DAYS,
    )

    undo_parser = subparsers.add_parser(
        "undo",
        help="plan, diff and revert in one pass (dry run unless --confirm is passed)",
        description="Runs the whole sequence: resolve previous values, compare against live "
        "state, then revert. Dry run unless --confirm is given, exactly like `revert` alone - "
        "the review step is preserved by the default, not by refusing to compose the steps. "
        "The plan is always written to a file so the run stays auditable and re-checkable.",
    )
    _add_window_arguments(undo_parser, identity_required=True)
    undo_parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually perform the revert; without this nothing is called",
    )
    undo_parser.add_argument(
        "-o",
        "--out",
        metavar="FILE",
        help="where to write the plan (default: a temporary file, whose path is printed)",
    )
    undo_parser.add_argument(
        "--log", metavar="FILE", help="write the full result document - plan, diff and "
        "revert - here"
    )
    undo_parser.add_argument(
        "--detail",
        action="store_true",
        help="print each stage's own full report instead of the combined summary",
    )
    undo_parser.add_argument(
        "--only",
        metavar="CHAIN_ID",
        action="append",
        help="revert just this chain; repeatable",
    )
    undo_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="do not wait on EC2 stop/start waiters",
    )
    undo_parser.add_argument(
        "--set",
        metavar="SELECTOR=VALUE",
        action="append",
        dest="assignments",
        help="supply a previous value the tool cannot prove; recorded as ASSERTED. Repeatable",
    )
    undo_parser.add_argument(
        "--snapshot", metavar="FILE", help="a snapshot file from `rewind snapshot`"
    )
    undo_parser.add_argument(
        "--use-config",
        action="store_true",
        help="also consult AWS Config configuration history",
    )
    undo_parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        metavar="N",
        help="how far back to search for evidence (default and maximum: %d)" % RETENTION_DAYS,
    )
    undo_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit %d when a field conflicts or still needs a decision" % EXIT_CONFLICT,
    )

    diff_parser = subparsers.add_parser(
        "diff",
        help="compare a plan against live state and flag conflicts",
        description="Read-only. Reads each planned field's current value and reports "
        "whether anything outside the plan has changed it.",
    )
    diff_parser.add_argument("plan", metavar="PLAN", help="a plan file from `rewind plan -o`")
    diff_parser.add_argument(
        "--region",
        metavar="REGION",
        help="override the region recorded in the plan",
    )
    diff_parser.add_argument(
        "--output", choices=["table", "json"], default="table", help="output format"
    )
    diff_parser.add_argument(
        "--blame",
        action="store_true",
        help="for each conflict, search CloudTrail since the plan was generated for the "
        "event that changed the field",
    )
    diff_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit %d when any field conflicts, for use in scripts" % EXIT_CONFLICT,
    )

    revert_parser = subparsers.add_parser(
        "revert",
        help="put the planned fields back (dry run unless --confirm is passed)",
        description="Reverts newest change first. Live state is re-read immediately "
        "before each field is touched, and only fields that are still exactly where the "
        "session left them are changed. Dry run unless --confirm is given.",
    )
    revert_parser.add_argument("plan", metavar="PLAN", help="a plan file from `rewind plan -o`")
    revert_parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually perform the revert; without this nothing is called",
    )
    revert_parser.add_argument(
        "--only",
        metavar="CHAIN_ID",
        action="append",
        help="revert just this chain; repeatable",
    )
    revert_parser.add_argument(
        "--region", metavar="REGION", help="override the region recorded in the plan"
    )
    revert_parser.add_argument(
        "--output", choices=["table", "json"], default="table", help="output format"
    )
    revert_parser.add_argument(
        "--log", metavar="FILE", help="write the full result document here"
    )
    revert_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="do not wait on EC2 stop/start waiters; faster, but verification may race "
        "the instance",
    )
    revert_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit %d when any field was skipped, failed or is still pending"
        % EXIT_CONFLICT,
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="record the current value of supported fields to a local file",
        description="Read-only. Writes a plain local JSON file - no AWS resource is "
        "created and nothing is charged. Take one before running an agent and `rewind "
        "plan --snapshot` can use it as evidence for values CloudTrail cannot prove.",
    )
    snapshot_parser.add_argument(
        "--instance",
        metavar="INSTANCE_ID",
        action="append",
        default=[],
        help="an EC2 instance to record (instanceType and monitoring). Repeatable.",
    )
    snapshot_parser.add_argument(
        "--function",
        metavar="NAME:QUALIFIER",
        action="append",
        default=[],
        help="a Lambda function alias or version to record. Repeatable.",
    )
    snapshot_parser.add_argument(
        "--db-instance",
        metavar="IDENTIFIER",
        action="append",
        default=[],
        help="an RDS DB instance to record. Repeatable.",
    )
    snapshot_parser.add_argument(
        "--region", metavar="REGION", help="region to read; defaults to the ambient config"
    )
    snapshot_parser.add_argument(
        "-o", "--out", metavar="FILE", help="write the snapshot here (default: stdout)"
    )
    snapshot_parser.add_argument(
        "--output", choices=["table", "json"], default="table", help="output format"
    )

    subparsers.add_parser(
        "operations",
        help="show which fields have a plugin, and what happens to everything else",
        description="Any mutating change CloudTrail records is discovered and reconstructed "
        "generically. A plugin adds live-state reads and execution for one field.",
    )

    subparsers.add_parser(
        "resolvers",
        help="show the anchor resolver chain and which sources are active",
        description="Explains where the tool looks for a previous value, in priority order.",
    )
    return parser
