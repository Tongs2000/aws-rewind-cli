"""Anchor from the change event's own response.

The strongest evidence available, and the only source that is completely immune to
CloudTrail's 90-day retention: the API told us the old value while it was changing it.
Today only RDS ``ModifyDBInstance`` does this, but the hook exists on every operation
because any service that echoes pre-change state gets this for free.
"""

from __future__ import annotations

from ..domain import Anchor, Confidence
from .base import AnchorRequest, AnchorResolver


class ResponseElementsResolver(AnchorResolver):
    name = "response-elements"
    description = (
        "the changing call's own responseElements carried the pre-change value "
        "(no history needed)"
    )

    def resolve(self, request: AnchorRequest) -> Anchor:
        value = request.operation.response_anchor(request.first_mutation)
        if value is None:
            return Anchor.unknown(
                source=self.name,
                note="the %s response does not carry a pre-change %s"
                % (request.first_mutation.event_name, request.field_name),
            )
        return Anchor(
            value=value,
            confidence=Confidence.HIGH,
            source=self.name,
            evidence_event_ids=[request.first_mutation.event_id],
            note="pre-change value read from the change event's own responseElements",
        )
