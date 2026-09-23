"""One function per command: read arguments, call the pipeline, print the result.

Each returns an exit code and prints; none contains logic about *what* a revert means or
*where* a previous value comes from. If a rule lives here that an operator would care
about, it is in the wrong place - it belongs in :mod:`~rewind.pipeline`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import List, Tuple

from .. import __version__
from ..handlers import plugin_coverage
from ..pipeline import diff_plan, plan as build_plan, scan as run_scan, take_snapshot
from ..pipeline.revert import Reverter
from ..report import (
    render_diff,
    render_operations,
    render_plan,
    render_resolvers,
    render_revert,
    render_scan,
    render_snapshot,
)
from ..resolvers import SetSelectorError, build_chain, default_chain, parse_set
from ..store.plan import load as load_plan
from ..store.snapshot import dump as dump_snapshot, load as load_snapshot
from ..timeutil import check_retention
from .codes import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE
from . import context


def command_scan(args: argparse.Namespace, now: datetime) -> int:
    query = context.query(args, now)
    result = run_scan(context.source(query.region, args), query)
    retention = check_retention(query.start_time, now)
    if args.output == "json":
        body: dict = {
            "query": query.to_dict(),
            "eventsInWindow": len(result.window_events),
            "eventsForIdentity": len(result.owned_events),
            "truncated": result.truncated,
            # Both lists, always: "who made a change I can describe" and "who is in the
            # window but out of scope" are different questions, and which one is empty
            # depends on whether --identity was given.
            "identities": result.identities,
            "byIdentity": [row.to_dict() for row in result.by_identity],
            "otherIdentities": result.other_identities,
            "changes": [
                {
                    "eventId": m.event_id,
                    "eventTime": m.event_time.isoformat(),
                    "eventName": m.event_name,
                    "identity": m.identity,
                    "eventSource": m.event_source,
                    "handler": m.handler,
                    "resourceType": m.resource_type,
                    "resourceId": m.resource_id,
                    "field": m.field_name,
                    "fieldPath": list(m.field.path),
                    "setTo": m.after,
                    "valueKnown": m.value_known,
                }
                for m in result.mutations
            ],
        }
        if retention:
            body["warnings"] = [retention]
        context.emit(json.dumps(body, indent=2, default=str))
    else:
        text = render_scan(result, detail=getattr(args, "detail", False))
        if retention:
            text += "\nwarning  : %s" % retention
        context.emit(text)
    return EXIT_OK


def command_plan(args: argparse.Namespace, now: datetime) -> int:
    query = context.query(args, now)
    snapshot = load_snapshot(args.snapshot) if args.snapshot else None
    if snapshot is not None and snapshot.region and snapshot.region != query.region:
        print(
            "error: the snapshot was taken in %s but the plan targets %s"
            % (snapshot.region, query.region),
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        resolver = build_chain(
            snapshot=snapshot,
            config_client=context.config_client(query.region, args),
            assignments=dict(parse_set(a) for a in (args.assignments or [])),
        )
    except SetSelectorError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    plan = build_plan(
        source=context.source(query.region, args),
        query=query,
        resolver=resolver,
        lookback_days=args.lookback_days,
        now=now,
        tool_version=__version__,
    )
    retention = check_retention(query.start_time, now)
    if retention:
        plan.warnings.insert(0, retention)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(plan.to_dict(), handle, indent=2, default=str)
            handle.write("\n")

    if args.output == "json":
        context.emit(json.dumps(plan.to_dict(), indent=2, default=str))
    else:
        text = render_plan(plan, explain=args.explain)
        if args.out:
            text += "\nPlan written to %s" % args.out
        context.emit(text)
    return EXIT_OK


def command_diff(args: argparse.Namespace, now: datetime) -> int:
    plan = load_plan(args.plan)
    region = args.region or plan.region
    source = context.source(region, args) if args.blame else None
    result = diff_plan(
        plan=plan, clients=context.clients(region, args), source=source, now=now
    )

    if args.output == "json":
        context.emit(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        context.emit(render_diff(result))

    if args.exit_code and result.conflicts:
        return EXIT_CONFLICT
    return EXIT_OK


def command_revert(args: argparse.Namespace, now: datetime) -> int:
    plan = load_plan(args.plan)
    region = args.region or plan.region
    unknown = [c for c in (args.only or []) if plan.chain(c) is None]
    if unknown:
        print(
            "error: no such chain(s) in %s: %s" % (args.plan, ", ".join(sorted(unknown))),
            file=sys.stderr,
        )
        return EXIT_USAGE

    reverter = Reverter(
        clients=context.clients(region, args),
        dry_run=not args.confirm,
        wait=not args.no_wait,
    )
    run = reverter.run(plan, only=args.only, now=now)

    if args.log:
        with open(args.log, "w", encoding="utf-8") as handle:
            json.dump(run.to_dict(), handle, indent=2, default=str)
            handle.write("\n")

    if args.output == "json":
        context.emit(json.dumps(run.to_dict(), indent=2, default=str))
    else:
        text = render_revert(run)
        if args.log:
            text += "\nLog written to %s" % args.log
        context.emit(text)

    if args.exit_code and run.unfinished:
        return EXIT_CONFLICT
    return EXIT_OK


def command_snapshot(args: argparse.Namespace, now: datetime) -> int:
    targets: List[Tuple[str, str]] = []
    for instance_id in args.instance:
        targets.append(("AWS::EC2::Instance", instance_id))
    for alias in args.function:
        if ":" not in alias:
            print(
                "error: --function needs NAME:QUALIFIER, for example my-fn:live (got %r)"
                % alias,
                file=sys.stderr,
            )
            return EXIT_USAGE
        targets.append(("AWS::Lambda::Function", alias))
    for identifier in args.db_instance:
        targets.append(("AWS::RDS::DBInstance", identifier))

    if not targets:
        print(
            "error: name at least one resource with --instance, --function or --db-instance",
            file=sys.stderr,
        )
        return EXIT_USAGE

    region = context.resolve_region(args)
    snapshot = take_snapshot(
        clients=context.clients(region, args), targets=targets, region=region, now=now
    )
    body = json.dumps(dump_snapshot(snapshot), indent=2, default=str)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")

    if args.output == "json":
        context.emit(body)
    else:
        text = render_snapshot(snapshot)
        if args.out:
            text += "\nSnapshot written to %s" % args.out
        else:
            text += "\n\n" + body
        context.emit(text)
    return EXIT_OK


def command_operations(args: argparse.Namespace, now: datetime) -> int:
    coverage = plugin_coverage()
    if getattr(args, "output", "table") == "json":
        context.emit(json.dumps({"plugins": coverage}, indent=2))
    else:
        context.emit(render_operations(coverage))
    return EXIT_OK


def command_resolvers(args: argparse.Namespace, now: datetime) -> int:
    described = default_chain().describe()
    if getattr(args, "output", "table") == "json":
        context.emit(json.dumps({"resolvers": described}, indent=2))
    else:
        context.emit(render_resolvers(described))
    return EXIT_OK


COMMANDS = {
    "scan": command_scan,
    "plan": command_plan,
    "diff": command_diff,
    "revert": command_revert,
    "snapshot": command_snapshot,
    "operations": command_operations,
    "resolvers": command_resolvers,
}
