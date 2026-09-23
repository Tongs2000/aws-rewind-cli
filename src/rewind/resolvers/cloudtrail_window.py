"""Anchor from an earlier event in the queried CloudTrail window.

Walks backwards from the session's first change and takes the latest **successful**
event that set **this same field** on **this same resource**. The three emphasised
conditions are each a distinct correctness rule:

* a failed call is not a state transition, so events with an ``errorCode`` are skipped;
* "the previous event" is not "the previous value of this field" - a Stop/Start call,
  or a ``ModifyInstanceAttribute`` that set a different attribute, proves nothing here,
  and the operation decides that by returning ``None``;
* a change to a different resource is irrelevant, however similar it looks.
"""

from __future__ import annotations

from ..domain import Anchor, Confidence
from .base import AnchorRequest, AnchorResolver


class CloudTrailWindowResolver(AnchorResolver):
    name = "cloudtrail-window"
    description = "the latest earlier successful event in the window that set this field"

    def resolve(self, request: AnchorRequest) -> Anchor:
        operation = request.operation
        for event in request.window.before(request.boundary):
            if not event.successful:
                continue
            if operation.relevant_event_names and (
                event.event_name not in operation.relevant_event_names
            ):
                continue  # a plugin narrows the search; the generic layer does not
            value = request.anchor_from(event)
            if value is None:
                continue
            return Anchor(
                value=value,
                confidence=Confidence.HIGH,
                source=self.name,
                evidence_event_ids=[event.event_id],
                note="previous value set by %s at %s"
                % (event.event_name, event.event_time.isoformat()),
            )
        return Anchor.unknown(
            source=self.name,
            note="no earlier successful event in the window sets %s on %s"
            % (request.field_name, request.resource_id),
        )
