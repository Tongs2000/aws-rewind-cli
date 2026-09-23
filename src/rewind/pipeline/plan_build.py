"""Chain construction: turn a flat list of mutations into per-field histories.

This is where the tool's central trick lives. Instead of asking "what was the previous
value?" once per change - each question independently exposed to CloudTrail's retention
limit - it groups a session's changes by ``(resource, parameter path)`` and asks the
question **once per chain**, for the value before the first change. Every later step's
``before`` is simply the previous step's ``after``, which CloudTrail recorded directly.

A session that resized one instance five times therefore has one unknown, not five.

Chains are built the same way whether a plugin claimed the field or the generic layer
handled it. The difference shows up only at the end, in the chain's
:class:`~rewind.models.Capability` tier: what the tool is willing to *do* about it.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from ..trail import CloudTrailEvent
from ..handlers.generic.inverse import symmetric_inverse
from ..domain import Anchor, Capability, Chain, Change, Mutation
from ..handlers import GENERIC, capability_of, handler_for, parse_event
from ..handlers.generic.parser import flatten
from ..resolvers import AnchorRequest, ResolverChain


def extract_mutations(events: List[CloudTrailEvent]) -> List[Mutation]:
    """Every field change in every event, ordered deterministically."""
    mutations: List[Mutation] = []
    for event in events:
        mutations.extend(parse_event(event))
    mutations.sort(key=lambda m: m.sort_key)
    return mutations


def group_mutations(
    mutations: List[Mutation],
) -> "OrderedDict[Tuple[str, Tuple[str, ...]], List[Mutation]]":
    """Group by ``(resourceId, parameter path)``, preserving first-seen order."""
    groups: "OrderedDict[Tuple[str, Tuple[str, ...]], List[Mutation]]" = OrderedDict()
    for mutation in mutations:
        groups.setdefault(mutation.chain_key, []).append(mutation)
    return groups


def build_chains(
    mutations: List[Mutation], resolver: ResolverChain, window, region: str = ""
) -> List[Chain]:
    """Build one anchored chain per ``(resource, field)`` the session touched."""
    chains = [
        _build_chain(group, resolver, window, region)
        for group in group_mutations(mutations).values()
    ]
    chains.sort(key=lambda c: c.sort_key)
    return chains


def _build_chain(
    group: List[Mutation], resolver: ResolverChain, window, region: str
) -> Chain:
    first = group[0]
    operation = handler_for(first)

    if first.value_known:
        anchor = resolver.resolve(
            AnchorRequest(
                operation=operation,
                resource_id=first.resource_id,
                field_name=first.field_name,
                first_mutation=first,
                window=window,
            )
        )
    else:
        # Nothing to chain from: the call changed something the parameters do not name.
        anchor = Anchor.unknown(
            source="none",
            note="the value this call set is not visible in requestParameters, so neither "
            "the old nor the new value can be established",
        )

    changes = _link(group, anchor)
    revert = _revert(operation, first, anchor, changes, region)
    capability = _capability(operation, first, anchor, revert)
    notes = _notes(changes, anchor, operation, first, capability)
    return Chain(
        field=first.field,
        handler=first.handler,
        event_source=first.event_source,
        event_name=first.event_name,
        changes=changes,
        anchor=anchor,
        revert=revert,
        notes=notes,
        capability=capability,
    )


def _link(group: List[Mutation], anchor: Anchor) -> List[Change]:
    """Chain the steps: each ``before`` is the previous ``after``.

    Only the first step's ``before`` comes from the anchor, so an UNKNOWN anchor leaves
    exactly one hole and every later transition is still fully known.
    """
    changes: List[Change] = []
    previous: Optional[str] = anchor.value
    for index, mutation in enumerate(group, start=1):
        changes.append(Change.from_mutation(index, mutation, previous))
        previous = mutation.after
    return changes


def _capability(
    operation, first: Mutation, anchor: Anchor, revert: Dict
) -> Capability:
    """How far the tool got with this field. Reported per row, so nothing is silent.

    Describes what the tool *can do about the field*, not whether this particular session
    left work to do. A field the session changed and changed back is still AUTO: the revert
    path re-reads live state and reports ALREADY_AT_ORIGINAL, which is more use than a flat
    "nothing to do".
    """
    return capability_of(
        operation,
        values_known=first.value_known,
        anchor_proven=anchor.proven,
        has_manual_command=bool(revert.get("manual")),
    )


def _identifier_params(mutation: Mutation) -> Dict[str, str]:
    """The request parameters that name this resource, for writing an inverse command.

    Only the parameters whose value *is* this resource's id, so a call covering several
    instances yields a command for the right one.
    """
    params: Dict[str, str] = {}
    for path, value in flatten(mutation.request_parameters):
        if path and value == mutation.resource_id:
            params.setdefault(path[-1], mutation.resource_id)
    return params


def _revert(
    operation, first: Mutation, anchor: Anchor, changes: List[Change], region: str
) -> Dict:
    if not anchor.proven:
        return {
            "executable": False,
            "reason": "the previous value of %s is not proven" % first.field_name,
        }
    if anchor.value == changes[-1].after:
        return {
            "executable": False,
            "reason": "the field already holds its pre-session value",
        }

    if operation is GENERIC:
        # No plugin: write the command out, never run it. See rewind.inverse for why.
        body = symmetric_inverse(
            event_source=first.event_source,
            event_name=first.event_name,
            resource_id=first.resource_id,
            identifier_params=_identifier_params(first),
            path=first.field.path,
            target_value=anchor.value,
            region=region,
        )
        return body

    plan = operation.revert_plan(first.resource_id, anchor.value)
    plan["executable"] = True
    plan["targetValue"] = anchor.value
    return plan


def _notes(
    changes: List[Change],
    anchor: Anchor,
    operation,
    first: Mutation,
    capability: Capability,
) -> List[str]:
    notes: List[str] = []
    if len(changes) > 1 and first.value_known:
        notes.append(
            "%d changes to this field in the session; the value to restore is the one "
            "from before the first change" % len(changes)
        )
    elif len(changes) > 1:
        # Saying "the value to restore is the one from before the first change" here was
        # flatly wrong, and the very next note said the value cannot be established at all.
        notes.append(
            "%s was called %d times on this resource; the calls are grouped because they "
            "are the same operation on the same resource, not because there is a value to "
            "restore" % (first.event_name, len(changes))
        )
    if not first.value_known:
        notes.append(
            "%s does not record the new value in requestParameters, so this change is "
            "reported but cannot be reconstructed" % first.event_name
        )
    elif not anchor.proven:
        notes.append(
            "not revertible automatically: the previous value is not proven. Supply it "
            "explicitly with --set to revert anyway."
        )
    if capability is Capability.MANUAL:
        notes.append(
            "no plugin covers this field, so the tool will not call AWS for it; the "
            "command is written out for you to run"
        )
    if capability is Capability.RECONSTRUCTED:
        notes.append(
            "both values are known but no safe inverse call can be built for this API"
        )
    no_ops = [c.sequence for c in changes if c.no_op]
    if no_ops:
        notes.append(
            "step(s) %s set the field to the value it already held"
            % ", ".join(str(s) for s in no_ops)
        )
    if anchor.proven and anchor.value == changes[-1].after:
        notes.append(
            "net effect is zero: the session ended with the field back at its original "
            "value, so there is nothing to revert"
        )
    if getattr(operation, "asynchronous", False):
        notes.append(
            "%s is applied asynchronously by AWS; a revert is reported as submitted "
            "until a Describe call confirms it" % operation.name
        )
    return notes
