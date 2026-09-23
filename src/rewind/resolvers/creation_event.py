"""Anchor from the resource's creation event.

Weaker than an explicit field-setting event, because it only establishes the *initial*
value - so it is reported as MEDIUM confidence. It is sound only when two conditions
hold, and both are checked rather than assumed:

* no event between creation and the session's first change set this field. The
  higher-priority :class:`CloudTrailWindowResolver` runs first and would have found one,
  but this resolver re-checks so it stays correct if the chain is ever reordered;
* the window was not truncated. If a lookup was cut short, "we saw no intervening
  event" means nothing, so the resolver declines rather than guessing.
"""

from __future__ import annotations

from ..domain import Anchor, Confidence
from .base import AnchorRequest, AnchorResolver


class CreationEventResolver(AnchorResolver):
    name = "creation-event"
    description = "the resource's creation event, when it is inside the window"

    def resolve(self, request: AnchorRequest) -> Anchor:
        if not request.window.complete:
            return Anchor.unknown(
                source=self.name,
                note="the CloudTrail lookup was truncated, so absence of an intervening "
                "event proves nothing",
            )

        operation = request.operation
        creation_names = operation.creation_event_names()
        if not creation_names:
            return Anchor.unknown(
                source=self.name,
                note="no creation event is defined for %s" % operation.name,
            )

        for event in request.window.before(request.boundary):
            if not event.successful:
                continue
            # An intervening event that set the field outranks creation; if one exists
            # this resolver must not answer.
            if event.event_name not in creation_names:
                if request.anchor_from(event) is not None:
                    return Anchor.unknown(
                        source=self.name,
                        note="a later event than creation set this field; "
                        "cloudtrail-window owns this case",
                    )
                continue
            value = operation.anchor_from_creation(event, request.resource_id)
            if value is None:
                continue
            return Anchor(
                value=value,
                confidence=Confidence.MEDIUM,
                source=self.name,
                evidence_event_ids=[event.event_id],
                note="initial value from %s; no event changed this field between "
                "creation and the session" % event.event_name,
            )
        return Anchor.unknown(
            source=self.name,
            note="the creation of %s is not visible in the window" % request.resource_id,
        )
