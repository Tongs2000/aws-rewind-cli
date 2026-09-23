r"""Defaults that keep a handler short, without blurring the roles.

:class:`BaseHandler` supplies :class:`~rewind.handlers.protocols.Parser` plumbing and
no-op :class:`~rewind.handlers.protocols.Historian` answers, because asking a handler
"does an earlier event prove this value?" is always safe - a handler that cannot help
returns None and the resolver chain moves on.

For :class:`~rewind.handlers.protocols.Actuator` it supplies only the two methods that
have a sensible default, and deliberately **not** the three that constitute the opt-in:

    read_live_value   \
    revert_plan        >  write these three and you are an Actuator
    apply_revert      /

    read_live_detail  \  defaults here; overriding is a refinement
    verify_revert     /

That asymmetry is the mechanism. ``isinstance(handler, Actuator)`` is what decides whether
a field reaches AUTO, so if the base class stubbed out all five, every handler would look
capable of executing a revert. Writing the three *is* the declaration - there is no flag to
set and no registry to update.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional, Tuple

from ..domain import FieldRef, Mutation
from ..errors import LiveStateError
from ..trail import CloudTrailEvent
from .protocols import MISMATCH, VERIFIED


class BaseHandler:
    """Shared plumbing for a handler. Fills Identified + Parser + Historian."""

    #: public operation name, e.g. "SET_EC2_INSTANCE_TYPE"
    name: str = ""
    #: CloudFormation-style resource type
    resource_type: str = ""
    #: the single field this handler owns
    field_name: str = ""
    #: event names that can change the field
    mutating_event_names: FrozenSet[str] = frozenset()
    #: every event name worth fetching, including anchor evidence (a superset)
    relevant_event_names: FrozenSet[str] = frozenset()
    #: True when AWS applies the change asynchronously
    asynchronous: bool = False
    #: AWS Config resource type, when Config records this resource at all
    config_resource_type: Optional[str] = None

    # -- identity -----------------------------------------------------------

    @property
    def field_path(self) -> Tuple[str, ...]:
        """The synthetic path this handler files its field under.

        A single segment equal to :attr:`field_name`, so a handled field reads the same in
        output whether or not the value happens to live at that path in the request.
        ``monitoring`` is not in requestParameters at all, for instance.
        """
        return (self.field_name,)

    # -- Parser defaults ----------------------------------------------------

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        return frozenset()

    # -- Historian defaults: "I cannot help with that" ----------------------

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        return None

    def anchor_at(
        self, event: CloudTrailEvent, resource_id: str, path: Tuple[str, ...]
    ) -> Optional[str]:
        """A handler owning one field does not need the path; the generic one overrides."""
        return self.anchor_from_event(event, resource_id)

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        return None

    def creation_event_names(self) -> FrozenSet[str]:
        return frozenset()

    def response_anchor(self, mutation: Mutation) -> Optional[str]:
        return None

    def value_from_config(self, configuration: Dict[str, Any]) -> Optional[str]:
        return None

    # -- Actuator defaults: only the two that have a sane default ------------

    def read_live_detail(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        """Extra context to show beside the value. Most fields have none."""
        return {}

    def verify_revert(self, clients: Any, resource_id: str, target_value: str) -> str:
        """Did the revert land? Correct for any synchronous field.

        Calls ``read_live_value``, which this class does not define - by design. Only a
        handler that implemented it is ever asked to verify anything.
        """
        observed = self.read_live_value(clients, resource_id)  # type: ignore[attr-defined]
        return VERIFIED if observed == target_value else MISMATCH

    # -- helpers ------------------------------------------------------------

    def _mutation(
        self, event: CloudTrailEvent, resource_id: str, after: str
    ) -> Mutation:
        return Mutation(
            event_id=event.event_id,
            event_time=event.event_time,
            event_name=event.event_name,
            event_source=event.event_source,
            identity=event.identity_label,
            handler=self.name,
            field=FieldRef(
                resource_id=resource_id,
                path=self.field_path,
                resource_type=self.resource_type,
            ),
            after=after,
            request_parameters=event.request_parameters,
            response_elements=event.response_elements,
        )

    @staticmethod
    def not_readable(resource_id: str, why: str) -> LiveStateError:
        return LiveStateError("cannot read %s: %s" % (resource_id, why))
