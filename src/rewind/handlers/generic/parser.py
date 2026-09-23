"""The generic layer: understand a change without knowing the API.

This is what stops the tool from presupposing what a session contains. CloudTrail already
carries enough to describe *any* mutating call:

* ``readOnly: false`` says it changed something;
* ``requestParameters`` names the resource and, for declarative APIs, carries the new value
  of each field it set;
* the same parameter path on the same resource in an earlier event carries the old value -
  which is the chain trick, and it needs no per-API code at all.

So a field nobody wrote a plugin for is still discovered, still chained, still gets a
previous value, and still gets an inverse call written out. What it does *not* get is
execution: see :func:`symmetric_inverse` for why.

Nothing here is clever about semantics. It is deliberately mechanical, and everything it
infers - which leaf is the resource, which leaves are values - is reported in the plan so a
wrong guess is visible rather than silent.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import FrozenSet, List, Optional, Sequence, Tuple

from ...trail import CloudTrailEvent, collect, flatten
from ...domain import FieldRef, GENERIC_HANDLER, Mutation, render_values
from ..base import BaseHandler
from .inverse import strip_api_version

#: Event-name prefixes that never change anything. Used only when an event predates the
#: ``readOnly`` flag; modern events carry it and it is trusted first.
READ_PREFIXES = (
    "Describe", "Get", "List", "Lookup", "Head", "Query", "Scan", "Search", "Select",
    "Batch Get", "BatchGet", "Check", "Estimate", "Preview", "Simulate", "Test",
    "Validate", "View", "Verify", "Filter", "Count", "Discover", "Detect", "Poll",
    "Receive", "Read", "Resolve", "Sample",
)

#: Keys that identify a resource rather than describe a change.
IDENTIFIER_SUFFIXES = (
    "id", "identifier", "name", "arn", "key", "ids", "identifiers", "qualifier", "alias",
)

#: Request parameters that are call mechanics, not state. Never treated as changed fields.
NOISE_KEYS = frozenset(
    {
        "clienttoken", "dryrun", "force", "applyimmediately", "region", "maxresults",
        "nexttoken", "marker", "requestid", "idempotencytoken", "skipfinalsnapshot",
        "tags", "tagspecificationset", "tagset", "reason", "description", "comment",
        "expectedbucketowner", "apiversion", "version", "skiposshutdown", "hibernate",
    }
)

#: Event-name prefixes that bring a resource into existence or remove it. Their request
#: parameters are *launch arguments*, not the state of an existing field: ``minCount`` on
#: ``RunInstances`` is not a setting anybody can revert. So these events are reported once
#: per resource, under the event name, and their parameters are not mined for fields.
#:
#: Narrower than :data:`~rewind.handlers.generic.inverse.LIFECYCLE_PREFIXES` on purpose.
#: That list also covers Attach/Authorize/Start/Stop, whose parameters *are* state worth
#: reporting even though no symmetric inverse exists for them.
EXISTENCE_PREFIXES = (
    "Create", "Run", "Register", "Allocate", "Import", "Restore",
    "Delete", "Terminate", "Deregister", "Release",
)

_ARN = re.compile(r"^arn:[a-z0-9-]*:")
_IDENTIFIER_VALUE = re.compile(r"^(i|vol|sg|subnet|vpc|eni|ami|snap|rtb|igw|db|cluster)-[0-9a-z]{4,}$")


def is_mutating(event: CloudTrailEvent) -> bool:
    """Did this call change something? Trust CloudTrail's own flag when it is there."""
    read_only = event.raw.get("readOnly")
    if isinstance(read_only, bool):
        return not read_only
    if isinstance(read_only, str):
        return read_only.strip().lower() != "true"
    return not event.event_name.startswith(READ_PREFIXES)


def _noun(key: str) -> str:
    """``dBInstanceIdentifier`` -> ``dbinstance``. The thing the parameter identifies."""
    lowered = key.lower()
    for suffix in sorted(IDENTIFIER_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            return lowered[: -len(suffix)]
    return ""


def _rank(path: Tuple[str, ...], value: str, event_name: str) -> int:
    """How strongly does this leaf claim to be the resource the call acted on?

    Three tiers, strongest first, because the weakest of them is what went wrong in
    practice: ``UpdateInstanceInformation`` carries ``platformName`` ("Amazon Linux") and
    ``agentName`` ("amazon-ssm-agent") beside ``instanceId``, and a flat "key ends in a
    noun-ish suffix" test made all three the resource - so every field was reported three
    times, against two things that are not resources at all.

    3. the key names what the API itself says it acts on (``dBInstanceIdentifier`` in
       ``ModifyDBInstance``). An API is not shy about naming its own subject.
    2. the value is shaped like an id or an ARN (``i-...``, ``arn:aws:...``).
    1. the key merely ends in an identifier suffix. Kept, but only as a last resort.
    """
    noun = _noun(path[-1])
    if noun and noun in event_name.lower():
        return 3
    if _ARN.match(value) or _IDENTIFIER_VALUE.match(value):
        return 2
    if path[-1].lower().endswith(IDENTIFIER_SUFFIXES):
        return 1
    return 0


def _from_index(event: CloudTrailEvent) -> List[str]:
    """Resources index entries, restricted to the service that emitted the event.

    RDS populates the index generously: one ``ModifyDBInstance`` lists the DB instance, its
    parameter groups, its subnet group, its security groups *and* its VPC. Keeping all of
    them made the tool announce that a VPC's ``allowMajorVersionUpgrade`` had changed.
    """
    service = event.event_source.split(".")[0].lower()
    typed = [
        entry
        for entry in event.resources
        if isinstance(entry, dict) and entry.get("ResourceName")
    ]
    own = [
        entry["ResourceName"]
        for entry in typed
        if (entry.get("ResourceType") or "").split("::")[1:2] == [service.upper()]
        or (entry.get("ResourceType") or "").lower().split("::")[1:2] == [service]
    ]
    return _unique(own) if own else _unique([e["ResourceName"] for e in typed])


def _best_group(source: object, event_name: str) -> List[str]:
    """The strongest, most homogeneous set of resource ids in one document.

    Two rules, and both were learned from real CloudTrail rather than guessed:

    * **the strongest tier wins.** A weaker candidate sitting beside a stronger one in the
      same event is noise, not a second target.
    * **one parameter path only.** A single path may hold several ids - one
      ``MonitorInstances`` covering two instances - and fanning a field across those is
      right. Several *paths* hold different kinds of thing, and fanning across those is what
      turned one ``RunInstances`` into 224 rows and gave a VPC an RDS field.
    """
    by_path: "OrderedDict[Tuple[str, ...], List[str]]" = OrderedDict()
    best = 0
    for path, value in flatten(source):
        if not isinstance(value, str) or not value or not path:
            continue
        if any(segment.lower() in NOISE_KEYS for segment in path):
            continue
        rank = _rank(path, value, event_name)
        if rank == 0:
            continue
        if rank > best:
            best, by_path = rank, OrderedDict()
        if rank == best:
            by_path.setdefault(path, []).append(value)
    if not by_path:
        return []
    return _unique(next(iter(by_path.values())))


def resource_ids(event: CloudTrailEvent) -> List[str]:
    """Which resource did this call act on?

    Request parameters first, by :func:`_best_group`. A creation is identified from
    ``responseElements`` instead, because the resource it created did not exist to be named
    in the request - and the response names it the same way the request would have.
    """
    if is_existence_event(event):
        created = _best_group(event.response_elements, event.event_name)
        if created:
            return created
    found = _best_group(event.request_parameters, event.event_name)
    return found or _from_index(event)


def is_existence_event(event: CloudTrailEvent) -> bool:
    return strip_api_version(event.event_name).startswith(EXISTENCE_PREFIXES)


def resource_type_from_index(event: CloudTrailEvent, resource_id: str) -> str:
    for entry in event.resources:
        if isinstance(entry, dict) and entry.get("ResourceName") == resource_id:
            return entry.get("ResourceType") or ""
    return ""


def changed_paths(
    event: CloudTrailEvent, claimed: FrozenSet[Tuple[str, ...]] = frozenset()
) -> List[Tuple[Tuple[str, ...], str]]:
    """Parameter paths that look like a field being set, and the value set.

    A leaf is excluded when it *is* one of the resource ids, when its top-level key names a
    resource, when it is call mechanics, or when a plugin already claimed it. Note the first
    rule: asking "is this value the resource?" rather than "is this key identifier-shaped?"
    is what lets a nested ``groupSet.items.groupId`` be recognised as the value being set.
    """
    targets = set(resource_ids(event))
    results: List[Tuple[Tuple[str, ...], str]] = []
    for path, values in collect(event.request_parameters).items():
        if path in claimed or not path:
            continue
        if any(segment.lower() in NOISE_KEYS for segment in path):
            continue
        # Top-level identifier-named parameters name the resource. Deeper ones do not:
        # groupSet.items.groupId is a value even though it ends in "Id".
        if len(path) == 1 and path[0].lower().endswith(IDENTIFIER_SUFFIXES):
            continue
        remaining = [v for v in values if v not in targets]
        if not remaining:
            continue  # every value here names the resource
        results.append((path, render_values(remaining)))
    return results


def _unique(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


class GenericOperation(BaseHandler):
    """Mechanical handling of any mutating event no plugin claimed.

    It fills the Parser and Historian roles - which is all that can be done without API
    knowledge - and deliberately not Actuator. Reading a field's live value needs to know
    which Describe call returns it; executing a revert needs to know the API's
    preconditions. Guessing at either is exactly the kind of invention this tool refuses.
    """

    name = GENERIC_HANDLER
    resource_type = ""
    field_name = ""

    def parse(self, event: CloudTrailEvent) -> List[Mutation]:
        return self.parse_unclaimed(event, frozenset(), plugin_handled=False)

    def parse_unclaimed(
        self,
        event: CloudTrailEvent,
        claimed: FrozenSet[Tuple[str, ...]],
        plugin_handled: bool = False,
    ) -> List[Mutation]:
        if not event.successful or not is_mutating(event):
            return []
        targets = resource_ids(event)
        if not targets:
            return []
        # A creation's parameters are launch arguments, not fields of an existing resource:
        # "minCount was set to 1" is not something anyone can revert. Report that the event
        # happened, against what it created, and stop there.
        paths = [] if is_existence_event(event) else changed_paths(event, claimed)

        mutations: List[Mutation] = []
        for resource_id in targets:
            if paths:
                for path, value in paths:
                    mutations.append(self._make(event, resource_id, path, value))
            elif not plugin_handled:
                # A mutating call whose new value is not in the parameters at all - a
                # Delete, or state encoded in the verb. Worth reporting; not chainable.
                # Filed under the event name, so StopInstances and StartInstances on one
                # instance stay separate rather than merging into a meaningless "chain".
                mutations.append(
                    self._make(event, resource_id, (event.event_name,), None)
                )
        return mutations

    def _make(
        self,
        event: CloudTrailEvent,
        resource_id: str,
        path: Tuple[str, ...],
        value: Optional[str],
    ) -> Mutation:
        return Mutation(
            event_id=event.event_id,
            event_time=event.event_time,
            event_name=event.event_name,
            event_source=event.event_source,
            identity=event.identity_label,
            handler=GENERIC_HANDLER,
            field=FieldRef(
                resource_id=resource_id,
                path=path,
                resource_type=resource_type_from_index(event, resource_id),
            ),
            after=value,
            request_parameters=event.request_parameters,
            response_elements=event.response_elements,
        )

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:  # pragma: no cover - the path-aware variant is used instead
        return None

    def anchor_at(
        self, event: CloudTrailEvent, resource_id: str, path: Tuple[str, ...]
    ) -> Optional[str]:
        """The value an earlier event set at this exact path on this resource.

        The whole chain trick with no API knowledge: if an earlier call wrote
        ``multiAZ=false`` on this database, that was the value.
        """
        if not path or resource_id not in resource_ids(event):
            return None
        values = collect(event.request_parameters).get(path)
        if not values:
            return None
        targets = set(resource_ids(event))
        remaining = [v for v in values if v not in targets]
        return render_values(remaining) if remaining else None

    # No read_live_value / revert_plan / apply_revert here, and that is the declaration:
    # the generic layer is not an Actuator. Stubbing them out - even to raise - would make
    # `isinstance(GENERIC, Actuator)` true and let a generic field be offered for
    # execution. Absence is how the tool says "I do not know how to touch this".


GENERIC = GenericOperation()
