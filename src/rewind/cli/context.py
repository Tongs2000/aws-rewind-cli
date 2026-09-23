"""Where command-line arguments become AWS objects.

Every one of these first honours an injected attribute on ``args`` (``_source``,
``_clients``, ``_config_client``, ``_region``). That is the whole test seam: a test builds
a namespace with fakes attached and calls ``main``, so no test needs to patch boto3, and
the real code path it exercises is the same one an operator gets.

boto3 is imported inside the functions on purpose - ``rewind operations`` and
``rewind --help`` should not pay for an SDK import they never use.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Any

from ..aws import AwsClients
from ..domain import Query
from ..timeutil import resolve_window
from ..trail import CloudTrailEventSource, EventSource


def source(region: str, args: argparse.Namespace) -> EventSource:
    injected = getattr(args, "_source", None)
    if injected is not None:
        return injected
    import boto3

    return CloudTrailEventSource(boto3.client("cloudtrail", region_name=region))


def resolve_region(args: argparse.Namespace) -> str:
    if getattr(args, "region", None):
        return args.region
    injected = getattr(args, "_region", None)
    if injected:
        return injected
    import boto3

    region = boto3.session.Session().region_name
    if not region:
        raise SystemExit(
            "no region configured; pass --region or set AWS_REGION / AWS_DEFAULT_REGION"
        )
    return region


def query(args: argparse.Namespace, now: datetime) -> Query:
    start, end = resolve_window(args.since, args.start, args.end, now=now)
    return Query(
        identity=getattr(args, "identity", None) or "",
        start_time=start,
        end_time=end,
        region=resolve_region(args),
    )


def emit(text: str, stream=None) -> None:
    print(text, file=stream or sys.stdout)


def clients(region: str, args: argparse.Namespace) -> AwsClients:
    injected = getattr(args, "_clients", None)
    if injected is not None:
        return injected
    return AwsClients(region=region)


def config_client(region: str, args: argparse.Namespace) -> Any:
    """None unless --use-config was passed; the resolver treats that as "not available"."""
    if not getattr(args, "use_config", False):
        return None
    injected = getattr(args, "_config_client", None)
    if injected is not None:
        return injected
    import boto3

    return boto3.client("config", region_name=region)
