"""plan: turn a session's changes into anchored chains and a described revert.

Grouping and linking live in :mod:`~rewind.pipeline.plan_build`; this module is the
assembly - resolving anchors, deciding each chain's capability tier, and collecting the
statistics and warnings an operator needs to judge the result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import FrozenSet, List, Optional

from ..domain import (
    DELIVERY_LAG_SECONDS,
    RETENTION_DAYS,
    Capability,
    Chain,
    Confidence,
    Plan,
    Query,
)
from ..handlers import is_mutating, relevant_event_names
from ..resolvers import ResolverChain, default_chain, operator_resolver
from ..trail import EventSource, EventWindow, build_window
from .plan_build import build_chains
from .scan import ScanResult, scan

UTC = timezone.utc

#: How far back to look for anchor evidence. Capped at CloudTrail's retention.
DEFAULT_LOOKBACK_DAYS = RETENTION_DAYS


def plan(
    source: EventSource,
    query: Query,
    resolver: Optional[ResolverChain] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: Optional[datetime] = None,
    tool_version: str = "0.1.0",
) -> Plan:
    """Scan, then anchor every chain and describe its revert."""
    resolver = resolver or default_chain()
    now = now or datetime.now(tz=UTC)

    result = scan(source, query)
    window = build_window(
        source,
        result.window_events,
        query.start_time,
        query.end_time,
        sorted(_lookback_event_names(result)),
        lookback_days,
    )
    if result.truncated:
        window.truncated = True

    chains = build_chains(result.mutations, resolver, window, query.region)
    warnings = _warnings(query, now, window, result, lookback_days)
    warnings.extend(_operator_warnings(resolver))
    return Plan(
        query=query,
        generated_at=now,
        chains=chains,
        warnings=warnings,
        stats=_stats(result, window, chains, lookback_days),
        tool_version=tool_version,
    )


def _lookback_event_names(result: ScanResult) -> FrozenSet[str]:
    """Which event names to search the lookback for.

    The plugins' names, plus every mutating event name the session actually used. Without
    the second half a generically-handled field could never find the earlier call that
    carries its old value - the lookback would simply not have fetched it.
    """
    seen = frozenset(
        event.event_name
        for event in result.window_events
        if event.successful and is_mutating(event) and event.event_name
    )
    return relevant_event_names() | seen


def _operator_warnings(resolver: ResolverChain) -> List[str]:
    """Never let a --set pass quietly if it did nothing."""
    operator = operator_resolver(resolver)
    if operator is None:
        return []
    warnings: List[str] = []
    for selector, supplied, proven in operator.ignored:
        warnings.append(
            "--set %s=%s was ignored: the previous value is already proven to be %r. "
            "The evidence wins; nothing was overridden."
            % (selector, supplied, proven)
        )
    unused = operator.unused_selectors()
    if unused:
        warnings.append(
            "these --set selectors matched no changed field in this window: %s. Check the "
            "resource id and field name, or the chain id from a previous plan."
            % ", ".join(unused)
        )
    return warnings


def _stats(result: ScanResult, window: EventWindow, chains: List[Chain], lookback_days: int):
    by_confidence = {c.value: 0 for c in Confidence}
    for chain in chains:
        by_confidence[chain.confidence.value] += 1
    by_capability = {c.value: 0 for c in Capability}
    for chain in chains:
        by_capability[chain.capability.value] += 1
    return {
        "eventsInWindow": len(result.window_events),
        "eventsForIdentity": len(result.owned_events),
        "changes": len(result.mutations),
        "chains": len(chains),
        "revertible": sum(1 for c in chains if c.executable),
        "manual": sum(1 for c in chains if c.capability is Capability.MANUAL),
        "unprovable": sum(1 for c in chains if not c.anchor.proven),
        "netNoOp": sum(1 for c in chains if c.net_no_op),
        "pluginBacked": sum(1 for c in chains if c.plugin_backed),
        "byConfidence": by_confidence,
        "byCapability": by_capability,
        "lookbackDays": min(lookback_days, RETENTION_DAYS),
        "eventsConsulted": len(window.events),
        "historyTruncated": window.truncated,
    }


def _warnings(
    query: Query,
    now: datetime,
    window: EventWindow,
    result: ScanResult,
    lookback_days: int,
) -> List[str]:
    warnings: List[str] = []
    if query.end_time > now - timedelta(seconds=DELIVERY_LAG_SECONDS):
        warnings.append(
            "The window ends less than %d minutes ago. CloudTrail is eventually "
            "consistent, so recent changes may be missing; re-run to refresh."
            % (DELIVERY_LAG_SECONDS // 60)
        )
    if window.truncated:
        warnings.append(
            "A CloudTrail lookup hit the page limit, so the history may be incomplete. "
            "Anchors that depend on completeness were downgraded to UNKNOWN."
        )
    if lookback_days >= RETENTION_DAYS:
        warnings.append(
            "Anchor evidence older than %d days cannot be read from CloudTrail event "
            "history. Fields last changed before then will be UNKNOWN; enable AWS "
            "Config recording, or pass the value with --set." % RETENTION_DAYS
        )
    if not result.window_events:
        warnings.append("No CloudTrail events were found in the requested window.")
    elif not result.owned_events:
        others = result.other_identities
        detail = (" Identities seen: %s." % ", ".join(others[:5])) if others else ""
        warnings.append(
            "Events exist in the window but none match identity %r.%s"
            % (query.identity, detail)
        )
    return warnings
