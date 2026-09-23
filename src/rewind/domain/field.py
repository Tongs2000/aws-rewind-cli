"""What changed: a field on a resource, and one recorded change to it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .values import iso

@dataclass(frozen=True)
class FieldRef:
    """Which field on which resource, addressed the way CloudTrail addresses it.

    ``path`` is the location inside ``requestParameters`` that carries the value - so the
    generic layer can name a field on an API nobody has written code for. A plugin may
    instead declare a synthetic single-segment path (``("monitoring",)``) when the value
    is not in the parameters at all but encoded in the event name.

    ``resource_type`` is empty when it had to be inferred and could not be.
    """

    resource_id: str
    path: Tuple[str, ...]
    resource_type: str = ""

    @property
    def label(self) -> str:
        """Display name: ``instanceType`` or ``VersioningConfiguration.Status``."""
        return ".".join(self.path) if self.path else "(whole call)"

    @property
    def key(self) -> Tuple[str, Tuple[str, ...]]:
        """What makes two changes "the same field". Type is excluded: it may be inferred."""
        return (self.resource_id, self.path)


@dataclass(frozen=True)
class Mutation:
    """One ``(resource, field)`` change extracted from one CloudTrail event."""

    event_id: str
    event_time: datetime
    event_name: str
    event_source: str
    identity: str
    #: the plugin that claimed this, or "generic"
    handler: str
    field: FieldRef
    #: the value the call set, or None when it could not be read out
    after: Optional[str] = None
    request_parameters: Dict[str, Any] = field(default_factory=dict)
    response_elements: Dict[str, Any] = field(default_factory=dict)

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
    def value_known(self) -> bool:
        return self.after is not None

    @property
    def sort_key(self) -> Tuple[datetime, str, str, str]:
        """Total order over mutations. Two mutations can share an event."""
        return (self.event_time, self.event_id, self.resource_id, self.field_name)

    @property
    def event_boundary(self) -> Tuple[datetime, str]:
        """Boundary for "events strictly before this one" lookups.

        Must have the same arity as :attr:`CloudTrailEvent.sort_key`: comparing a
        2-tuple with a 4-tuple makes the shorter one sort first, which would let an
        event match itself as its own anchor.
        """
        return (self.event_time, self.event_id)

    @property
    def chain_key(self) -> Tuple[str, Tuple[str, ...]]:
        return self.field.key


@dataclass
class Change:
    """One step in a chain: what an event set this field to, and what it held before.

    Flat fields rather than a reference to the whole :class:`Mutation`. Nothing downstream
    of planning needs the raw request parameters, and carrying them meant a chain could not
    be written to a file and read back as the same type - which is why there used to be a
    second class for the read-back form, and why adding a field meant editing two places.
    """

    sequence: int
    event_id: str
    event_time: datetime
    event_name: str
    identity: str
    before: Optional[str]
    after: Optional[str]

    @classmethod
    def from_mutation(
        cls, sequence: int, mutation: Mutation, before: Optional[str]
    ) -> "Change":
        return cls(
            sequence=sequence,
            event_id=mutation.event_id,
            event_time=mutation.event_time,
            event_name=mutation.event_name,
            identity=mutation.identity,
            before=before,
            after=mutation.after,
        )

    @property
    def no_op(self) -> bool:
        """This step set the field to the value it already held."""
        return self.before is not None and self.before == self.after

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "eventId": self.event_id,
            "eventTime": iso(self.event_time),
            "eventName": self.event_name,
            "identity": self.identity,
            "before": self.before,
            "after": self.after,
        }
