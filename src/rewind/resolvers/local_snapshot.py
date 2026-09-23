"""Anchor from a local snapshot taken before the change.

A snapshot is a direct observation of the field at a known time, so when it is valid it is
HIGH confidence. Validity is not assumed - two conditions are checked, and failing either
means the resolver declines rather than guesses:

1. the snapshot was taken **before** the session's first change to this field. A snapshot
   taken afterwards records the new value, which would be exactly the wrong answer;
2. CloudTrail shows nothing moved the field between the snapshot and that first change.
"""

from __future__ import annotations

from typing import Optional

from ..domain import Anchor, Confidence
from ..domain import Snapshot
from .base import AnchorRequest, AnchorResolver, intervening_change


class LocalSnapshotResolver(AnchorResolver):
    name = "local-snapshot"
    description = "a local snapshot file written before the change (optional, no service)"

    def __init__(self, snapshot: Optional[Snapshot] = None) -> None:
        self.snapshot = snapshot

    def available(self) -> bool:
        return self.snapshot is not None

    def unavailable_reason(self) -> Optional[str]:
        return None if self.snapshot else "no snapshot file was supplied (--snapshot)"

    def resolve(self, request: AnchorRequest) -> Anchor:
        snapshot = self.snapshot
        if snapshot is None:  # pragma: no cover - guarded by available()
            return Anchor.unknown(source=self.name, note="no snapshot supplied")

        entry = snapshot.lookup(
            request.operation.resource_type, request.resource_id, request.field_name
        )
        if entry is None:
            return Anchor.unknown(
                source=self.name,
                note="the snapshot does not record %s on %s"
                % (request.field_name, request.resource_id),
            )

        first_change = request.first_mutation.event_time
        if snapshot.taken_at >= first_change:
            return Anchor.unknown(
                source=self.name,
                note="the snapshot was taken at %s, which is not before the first change "
                "at %s, so it records the new value"
                % (snapshot.taken_at.isoformat(), first_change.isoformat()),
            )

        intervening = intervening_change(request, snapshot.taken_at)
        if intervening is not None:
            return Anchor.unknown(source=self.name, note=intervening)

        return Anchor(
            value=entry.value,
            confidence=Confidence.HIGH,
            source=self.name,
            evidence_event_ids=[],
            note="observed as %r in the snapshot taken at %s, with no CloudTrail event "
            "changing it before the session" % (entry.value, snapshot.taken_at.isoformat()),
        )
