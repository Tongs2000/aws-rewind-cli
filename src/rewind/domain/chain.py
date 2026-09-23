"""The chain: every change a session made to one field, plus its anchor.

CloudTrail records the value each call *set*, never the value it replaced. A session's
changes to one field therefore form a chain in which each step's ``before`` is the
previous step's ``after`` - so only one value per field is genuinely unknown, the one
from before the first change. That value is the **anchor**.

One anchor per chain rather than one lookup per change is the whole point: the number of
unprovable values stops growing with how busy the session was.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .constants import GENERIC_HANDLER
from .field import Change, FieldRef
from .values import iso

class Capability(str, enum.Enum):
    """How far the tool got with one changed field. Every row reports its own tier.

    The point of naming these is that a change the tool cannot revert is still worth
    showing. Silently omitting it - which is what a plugin-only design does - is worse
    than reporting it honestly at a lower tier.

    DISCOVERED     CloudTrail says this identity changed something here, but the new
                   value could not be read out of requestParameters.
    RECONSTRUCTED  the before/after values are known, but no inverse call can be built.
    MANUAL         an inverse call can be written out for a human to run, but the tool
                   will not issue it.
    AUTO           a plugin vouches for this field: conflict check, execution and
                   verification are all available.
    """

    DISCOVERED = "DISCOVERED"
    RECONSTRUCTED = "RECONSTRUCTED"
    MANUAL = "MANUAL"
    AUTO = "AUTO"


#: Tiers at which `revert --confirm` is willing to call AWS.
EXECUTABLE_TIERS = (Capability.AUTO,)


class Confidence(str, enum.Enum):
    """Where the anchor - and therefore the whole chain - came from.

    HIGH     the change event's own response carried the pre-change value, an earlier
             successful event in the window explicitly set this field, or a recorded
             observation (AWS Config item, local snapshot) captured it.
    MEDIUM   the value came from the resource's creation event, or from an inference
             that is sound only because the history is provably complete.
    ASSERTED an operator supplied the value with ``--set``. The tool did not prove it,
             and says so: the audit trail must never blur "we established this" with
             "somebody told us".
    UNKNOWN  not proven and not supplied. Never reverted.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    ASSERTED = "ASSERTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Anchor:
    """The value a field held before the session's first change to it."""

    value: Optional[str]
    confidence: Confidence
    source: str
    evidence_event_ids: List[str] = field(default_factory=list)
    note: Optional[str] = None

    @property
    def proven(self) -> bool:
        return self.confidence is not Confidence.UNKNOWN and self.value is not None

    @classmethod
    def unknown(cls, source: str, note: str) -> "Anchor":
        return cls(value=None, confidence=Confidence.UNKNOWN, source=source, note=note)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "confidence": self.confidence.value,
            "source": self.source,
            "evidenceEventIds": list(self.evidence_event_ids),
            "note": self.note,
        }


def chain_id(resource_id: str, path: Tuple[str, ...]) -> str:
    """Stable across runs, so plan files can be compared and --only/--set can name a chain."""
    digest = hashlib.sha256(
        "|".join([resource_id, ".".join(path)]).encode("utf-8")
    ).hexdigest()
    return "chn-" + digest[:12]


@dataclass
class Chain:
    """Every session change to one ``(resource, field)``, plus its anchor."""

    field: FieldRef
    #: the plugin that handled this, or "generic"
    handler: str
    event_source: str
    event_name: str
    changes: List[Change]
    anchor: Anchor
    revert: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    capability: Capability = Capability.DISCOVERED

    @property
    def chain_id(self) -> str:
        return chain_id(self.field.resource_id, self.field.path)

    @property
    def resource_id(self) -> str:
        return self.field.resource_id

    @property
    def resource_type(self) -> str:
        return self.field.resource_type

    @property
    def field_name(self) -> str:
        return self.field.label

    @property
    def plugin_backed(self) -> bool:
        return self.handler != GENERIC_HANDLER

    @property
    def anchor_proven(self) -> bool:
        """Whether the previous value was established at all."""
        return self.anchor.proven

    @property
    def change_count(self) -> int:
        return len(self.changes)

    @property
    def net_before(self) -> Optional[str]:
        """What the field held before the session. The value a revert restores."""
        return self.anchor.value

    @property
    def net_after(self) -> Optional[str]:
        """What the field holds now, according to CloudTrail."""
        return self.changes[-1].after

    @property
    def values_known(self) -> bool:
        """Could the new value be read out of requestParameters at all?"""
        return all(c.after is not None for c in self.changes)

    @property
    def confidence(self) -> Confidence:
        return self.anchor.confidence

    @property
    def net_no_op(self) -> bool:
        """The session ended where it started - nothing to revert."""
        return self.anchor.proven and self.net_before == self.net_after

    @property
    def executable(self) -> bool:
        """Revertible *by this tool*: proven old value, a real change, and a plugin."""
        return (
            self.anchor.proven
            and not self.net_no_op
            and self.capability in EXECUTABLE_TIERS
        )

    @property
    def reconstructed(self) -> bool:
        """Old and new values both known, whether or not a revert can be performed."""
        return self.anchor.proven and self.values_known

    @property
    def first_change_at(self) -> datetime:
        return self.changes[0].event_time

    @property
    def last_change_at(self) -> datetime:
        return self.changes[-1].event_time

    @property
    def sort_key(self) -> Tuple[datetime, str, str, str]:
        """Chains are presented in the order the session first touched each field."""
        first = self.changes[0]
        return (first.event_time, first.event_id, self.resource_id, self.field_name)

    @property
    def evidence_event_ids(self) -> List[str]:
        """Anchor evidence plus every session event that moved this field."""
        collected = list(self.anchor.evidence_event_ids)
        for change in self.changes:
            if change.event_id not in collected:
                collected.append(change.event_id)
        return collected

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chainId": self.chain_id,
            "resourceType": self.resource_type,
            "resourceId": self.resource_id,
            "field": self.field_name,
            "fieldPath": list(self.field.path),
            "netBefore": self.net_before,
            "netAfter": self.net_after,
            "confidence": self.confidence.value,
            "capability": self.capability.value,
            "handler": self.handler,
            "eventSource": self.event_source,
            "eventName": self.event_name,
            "executable": self.executable,
            "changeCount": len(self.changes),
            "firstChangeAt": iso(self.first_change_at),
            "lastChangeAt": iso(self.last_change_at),
            "anchor": self.anchor.to_dict(),
            "revert": self.revert,
            "changes": [c.to_dict() for c in self.changes],
            "evidenceEventIds": self.evidence_event_ids,
            "notes": list(self.notes),
        }
