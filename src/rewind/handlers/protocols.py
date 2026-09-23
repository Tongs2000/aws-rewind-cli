"""The extension contract: three small roles instead of one fat base class.

The capability tiers a change can reach **are** these interfaces. That is the whole idea:
a tier stops being something the tool infers by inspecting a dict and becomes a consequence
of which roles a handler fills.

    implement nothing      -> DISCOVERED     (the generic layer, free)
    + Parser               -> the field is named and grouped correctly
    + Historian            -> RECONSTRUCTED, and MANUAL if the API is safely re-callable
    + Actuator             -> AUTO

So supporting a new API is a graded commitment. Two methods gets you a correctly named,
chained field with a generated command; four more gets you execution. Nobody has to read a
200-line base class to find out which of thirteen methods are load-bearing.

Every role is a ``Protocol`` with ``runtime_checkable``, so :func:`capability_of` can ask
"is this an Actuator?" rather than looking for a magic key in a return value.
"""

from __future__ import annotations

from typing import (
    Any, Dict, FrozenSet, List, NamedTuple, Optional, Protocol, Tuple, runtime_checkable,
)

from ..domain import Mutation
from ..trail import CloudTrailEvent

#: Verification outcomes. Plain strings, so they travel through JSON unchanged.
VERIFIED = "VERIFIED"
PENDING = "PENDING"
MISMATCH = "MISMATCH"


def performed(api: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Record of one AWS call that was actually issued."""
    return {"api": api, "params": params}


def step(api: str, params: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """One call in a revert plan: what a human reads before approving it."""
    body: Dict[str, Any] = {"api": api, "params": params}
    body.update({k: v for k, v in extra.items() if v is not None})
    return body


class Verification(NamedTuple):
    """A verification verdict and the value the verdict was based on.

    One read produces both. Reading twice - once to judge, once to display - let a field
    that settles in a second be judged on the first read and shown from the second.
    """

    status: str
    observed: Optional[str]


@runtime_checkable
class Identified(Protocol):
    """What every handler must declare about itself. Two attributes, no methods."""

    #: public operation name, e.g. "SET_EC2_INSTANCE_TYPE"
    name: str
    #: CloudFormation-style resource type, or "" when it cannot be established
    resource_type: str
    #: the single field this handler owns
    field_name: str


@runtime_checkable
class Parser(Protocol):
    """Role 1: turn events into named changes.

    The minimum for a plugin. Everything else is optional refinement.
    """

    def parse(self, event: CloudTrailEvent) -> List[Mutation]:
        """Extract this handler's field from an event. Must ignore failed events."""

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        """The requestParameters paths this handler consumes for this event.

        The generic layer skips these so a field is not reported twice. Everything *else*
        in the same call still gets reported - which is how a ``ModifyInstanceAttribute``
        that also set an attribute nobody covers stays visible.
        """


@runtime_checkable
class Historian(Protocol):
    """Role 2: where this field's previous value can be found.

    Fill in whichever sources apply to the API; the resolver chain tries them in priority
    order and takes the first *proven* answer. Returning None everywhere is fine - the
    field then falls back to whatever the generic path can establish.
    """

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        """The value an earlier event proves this field held, or None."""

    def anchor_at(
        self, event: CloudTrailEvent, resource_id: str, path: Tuple[str, ...]
    ) -> Optional[str]:
        """Path-aware form, for a handler that files several fields by path.

        Exists so a resolver can ask one question of any handler. Without it a resolver had
        to recognise the generic handler and call a different method, which meant importing
        the registry from inside the resolve layer - a cycle, papered over with a function-
        local import. One method signature removes it.
        """

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        """The field's value at resource creation. Weaker: reported as MEDIUM."""

    def creation_event_names(self) -> FrozenSet[str]:
        """Which events count as creating this resource."""

    def response_anchor(self, mutation: Mutation) -> Optional[str]:
        """Pre-change value carried by the changing call's own response.

        The strongest anchor there is, because it needs no history at all and so is immune
        to CloudTrail's retention limit. Only RDS does this today.
        """

    def value_from_config(self, configuration: Dict[str, Any]) -> Optional[str]:
        """Read this field out of an AWS Config item's ``configuration`` object.

        None means Config does not record this field - which is the honest answer for
        Lambda provisioned concurrency, and must not be read as "the field was unset".
        """


@runtime_checkable
class Actuator(Protocol):
    """Role 3: read the live value and put it back.

    This is what buys AUTO, and it is the only role that touches AWS in a mutating way.
    A handler that stops at :class:`Historian` still produces a plan and a command; it just
    will not act, which for an API with preconditions or whole-document semantics is the
    correct answer rather than a limitation.
    """

    def read_live_value(self, clients: Any, resource_id: str) -> str:
        """The field's current value, as the same display string CloudTrail yields.

        Read-only. Raises :class:`~rewind.errors.LiveStateError` when it cannot be read,
        which the caller reports per resource rather than treating as fatal.
        """

    def read_live_detail(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        """Extra context worth showing beside the value (an in-flight change, say)."""

    def revert_plan(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        """Describe the calls that would restore ``target_value``. JSON-safe."""

    def apply_revert(
        self, clients: Any, resource_id: str, target_value: str, wait: bool = True
    ) -> List[Dict[str, Any]]:
        """Issue them. Only reached once live state has been re-checked."""

    def verify_revert(
        self, clients: Any, resource_id: str, target_value: str
    ) -> "Verification":
        """The verdict **and the value it was reached from**, in one read.

        Returning the observed value is not a convenience: the caller used to read live state
        a second time to display it, and on a field that settles in a second - EC2 detailed
        monitoring - the two reads landed either side of the transition. That produced a row
        saying FAILED, "the field does not read 'disabled'", beside a column reading
        ``disabled``. One read decides both, or they can disagree.

        Stricter than :meth:`read_live_value`, which reports the value a resource is
        *converging towards*. Verification asks whether it has settled there, so an
        asynchronous change reports PENDING instead of claiming success.
        """
