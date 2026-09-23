"""Anchor resolver contract.

An *anchor* is the value a field held immediately before a session's first change to
it. Every other value in the chain follows from CloudTrail, so the anchor is the only
thing that can be unprovable - and the only thing worth spending effort on.

Resolvers are tried in priority order and the first *proven* answer wins
(:class:`ResolverChain`). Each one is independently optional: a resolver that cannot
help returns ``Anchor.unknown(...)`` with a reason, and those reasons are surfaced to
the operator so it is clear *why* a value could not be established.

Priority, best evidence first:

1. :class:`~rewind.resolvers.response_elements.ResponseElementsResolver`
   the change event's own response carried the pre-change value. Needs no history at
   all, so it is immune to CloudTrail's retention limit.
2. ``ConfigHistoryResolver`` - AWS Config, when the account already records it. This
   is the real answer to the retention limit. Probed, never required.
3. :class:`~rewind.resolvers.cloudtrail_window.CloudTrailWindowResolver`
   an earlier successful event in the queried window that set this same field.
4. ``LocalSnapshotResolver`` - a snapshot the operator chose to take beforehand.
5. :class:`~rewind.resolvers.creation_event.CreationEventResolver`
   the resource's creation event, when it is inside the window.

Anything past that is UNKNOWN, and the operator can still supply the value by hand.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from ..trail import CloudTrailEvent, EventWindow
from ..domain import Anchor, Mutation
from ..handlers.protocols import Historian


@dataclass(frozen=True)
class AnchorRequest:
    """Everything a resolver may look at to establish one chain's anchor."""

    operation: Historian
    resource_id: str
    field_name: str
    #: the session's first change to this field - the boundary to look before
    first_mutation: Mutation
    window: EventWindow

    @property
    def boundary(self) -> Tuple[datetime, str]:
        """``(eventTime, eventId)`` of the session's first change to this field."""
        return self.first_mutation.event_boundary

    @property
    def path(self) -> Tuple[str, ...]:
        return self.first_mutation.field.path

    def anchor_from(self, event: "CloudTrailEvent") -> Optional[str]:
        """What value does this earlier event prove for *this* field?

        One question, asked of whichever handler owns the field. A plugin answers from its
        knowledge of the API; the generic handler answers by looking for the same parameter
        path on the same resource, which needs no knowledge at all. Resolvers never need to
        know which kind they are holding.
        """
        return self.operation.anchor_at(event, self.resource_id, self.path)


def intervening_change(request: "AnchorRequest", observed_at: datetime) -> Optional[str]:
    """Did anything set this field between ``observed_at`` and the session's first change?

    An observation - an AWS Config item, a local snapshot - only establishes the anchor if
    nothing moved the field between when it was taken and when the session touched it.
    CloudTrail is what can answer that, and both observation-based resolvers use this same
    check so the standard cannot drift between them.

    Returns a human-readable reason when an intervening change was found, else None.
    """
    for event in request.window.before(request.boundary):
        if not event.successful or event.event_time <= observed_at:
            continue
        value = request.anchor_from(event)
        if value is not None:
            return (
                "%s at %s set this field to %r after the observation was taken"
                % (event.event_name, event.event_time.isoformat(), value)
            )
    return None


class AnchorResolver(abc.ABC):
    #: stable identifier recorded as the anchor's ``source``
    name: str = ""
    #: human-readable one-liner for ``rewind plan --explain``
    description: str = ""

    def available(self) -> bool:
        """False when this resolver cannot run at all (a probe failed, say).

        An unavailable resolver is skipped with a note rather than treated as a
        negative answer, which keeps "we did not look" distinct from "we looked and
        found nothing".
        """
        return True

    def unavailable_reason(self) -> Optional[str]:
        return None

    @abc.abstractmethod
    def resolve(self, request: AnchorRequest) -> Anchor:
        """Return a proven anchor, or ``Anchor.unknown`` explaining the miss."""


class ResolverChain:
    """Tries resolvers in order and returns the first proven anchor."""

    def __init__(self, resolvers: Sequence[AnchorResolver]) -> None:
        self.resolvers: List[AnchorResolver] = list(resolvers)

    def resolve(self, request: AnchorRequest) -> Anchor:
        misses: List[str] = []
        for resolver in self.resolvers:
            if not resolver.available():
                reason = resolver.unavailable_reason() or "not available"
                misses.append("%s: %s" % (resolver.name, reason))
                continue
            anchor = resolver.resolve(request)
            if anchor.proven:
                self._note_superseded(request, anchor)
                return anchor
            if anchor.note:
                misses.append("%s: %s" % (resolver.name, anchor.note))
        return Anchor.unknown(
            source="none",
            note="no resolver could prove the previous value of %s on %s (%s)"
            % (request.field_name, request.resource_id, "; ".join(misses) or "no resolvers"),
        )

    def _note_superseded(self, request: AnchorRequest, anchor: Anchor) -> None:
        """Let a later resolver know a stronger source answered first.

        Only the operator resolver cares: a ``--set`` that was never needed has to be
        reported rather than silently dropped, in case it signals a typo or a bad
        assumption about which field was changed.
        """
        for resolver in self.resolvers:
            noter = getattr(resolver, "note_ignored", None)
            if noter is not None and anchor.value is not None:
                noter(request, anchor.value)

    def describe(self) -> List[dict]:
        return [
            {
                "name": r.name,
                "description": r.description,
                "available": r.available(),
                "unavailableReason": r.unavailable_reason(),
            }
            for r in self.resolvers
        ]
