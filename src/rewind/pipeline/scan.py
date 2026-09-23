"""scan: what changed in a window, and who changed it.

CloudTrail only. No live-state call, no anchor resolution - just the changes, so an operator
can see the shape of a session before asking the tool to work out what anything used to be.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..domain import GENERIC_HANDLER, Mutation, Query
from ..trail import CloudTrailEvent, EventSource
from .plan_build import extract_mutations


@dataclass
class IdentityChanges:
    """What one identity did in the window, counted rather than listed."""

    identity: str
    changes: int = 0
    plugin_backed: int = 0
    event_names: List[str] = field(default_factory=list)
    _resources: List[str] = field(default_factory=list)

    @property
    def resources(self) -> int:
        return len(self._resources)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identity": self.identity,
            "changes": self.changes,
            "resources": self.resources,
            "pluginBacked": self.plugin_backed,
            "eventNames": sorted(self.event_names),
        }


class ScanResult:
    """What ``rewind scan`` produces: mutations, plus the context to explain them."""

    def __init__(
        self,
        query: Query,
        window_events: List[CloudTrailEvent],
        owned_events: List[CloudTrailEvent],
        mutations: List[Mutation],
        truncated: bool,
    ) -> None:
        self.query = query
        self.window_events = window_events
        self.owned_events = owned_events
        self.mutations = mutations
        self.truncated = truncated

    @property
    def identities(self) -> List[str]:
        """Every identity behind a tracked change, in first-appearance order.

        Built from the mutations rather than the raw events, so it answers "who changed
        something the tool can describe" and not "who called an API".
        """
        labels: List[str] = []
        for mutation in self.mutations:
            if mutation.identity and mutation.identity not in labels:
                labels.append(mutation.identity)
        return labels

    @property
    def by_identity(self) -> List["IdentityChanges"]:
        """One row per identity, ordered so the one worth acting on is first.

        This is the answer to "who should I ask about", which is the question the unscoped
        scan exists for. A detail row per change does not answer it: a real account produces
        hundreds of SSM agent heartbeats for every handful of changes a human made, and the
        signal is invisible in the noise.

        ``plugin_backed`` is what makes the ordering useful - it counts the changes the tool
        could actually execute a revert for, so the identity that matters floats to the top
        even when another identity made twenty times as many changes.
        """
        grouped: "OrderedDict[str, IdentityChanges]" = OrderedDict()
        for mutation in self.mutations:
            label = mutation.identity or "(unknown)"
            row = grouped.get(label)
            if row is None:
                row = grouped[label] = IdentityChanges(identity=label)
            row.changes += 1
            if mutation.resource_id not in row._resources:
                row._resources.append(mutation.resource_id)
            if mutation.event_name and mutation.event_name not in row.event_names:
                row.event_names.append(mutation.event_name)
            if mutation.handler != GENERIC_HANDLER:
                row.plugin_backed += 1
        return sorted(
            grouped.values(),
            key=lambda r: (-r.plugin_backed, -r.changes, r.identity),
        )

    @property
    def other_identities(self) -> List[str]:
        """Identities that changed something in the window but were not asked about."""
        labels: List[str] = []
        owned = {e.event_id for e in self.owned_events}
        for event in self.window_events:
            if event.event_id in owned or not event.successful:
                continue
            if event.identity_label and event.identity_label not in labels:
                labels.append(event.identity_label)
        return labels


def scan(source: EventSource, query: Query) -> ScanResult:
    """Read the window and extract the changes it contains.

    Scoped to one identity when the query names one; otherwise every event in the window is
    in scope, which is how an operator finds out which identity to ask about next.
    """
    window_events, truncated = source.window_events(query.start_time, query.end_time)
    owned = (
        [e for e in window_events if e.matches_identity(query.identity)]
        if query.filtered
        else list(window_events)
    )
    return ScanResult(
        query=query,
        window_events=window_events,
        owned_events=owned,
        mutations=extract_mutations(owned),
        truncated=truncated,
    )
