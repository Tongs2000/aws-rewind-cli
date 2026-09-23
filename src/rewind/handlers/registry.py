"""Handler registry: plugins first, generic handling for everything else.

The registry is the piece that makes the tool general. A change is never invisible for lack
of a plugin - it is handled generically and reported at a lower
:class:`~rewind.domain.Capability` tier. Plugins are refinements that unlock live-state
reads and execution, not the price of admission.

Adding an AWS field means one module under :mod:`rewind.handlers.aws` and one line in
:data:`PLUGINS`. Nothing else in the tool changes.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Tuple

from ..trail import CloudTrailEvent
from ..domain import GENERIC_HANDLER, Capability, Mutation
from .base import BaseHandler
from .protocols import MISMATCH, PENDING, VERIFIED, Actuator, performed, step
from .aws.ec2_detailed_monitoring import Ec2DetailedMonitoringOperation
from .aws.ec2_instance_attribute import all_attribute_operations
from .generic.parser import GENERIC, GenericOperation, is_mutating
from .aws.lambda_provisioned_concurrency import LambdaProvisionedConcurrencyOperation
from .aws.rds_multi_az import RdsMultiAzOperation

#: Plugins, in registration order. Each lifts one field from MANUAL to AUTO.
PLUGINS: List[BaseHandler] = [
    Ec2DetailedMonitoringOperation(),
    LambdaProvisionedConcurrencyOperation(),
    RdsMultiAzOperation(),
    # One class, one instance per EC2 instance attribute - instanceType included. Adding
    # another attribute is a line in ec2_instance_attribute.ATTRIBUTES, not a new file.
    *all_attribute_operations(),
]

#: Kept as an alias: several call sites want "everything that can handle a field".
ALL_OPERATIONS: List[BaseHandler] = PLUGINS

def register_plugin(plugin: BaseHandler) -> BaseHandler:
    """Add a plugin at runtime. Returns it, so it can be used as a decorator."""
    PLUGINS.append(plugin)
    return plugin


def get_operation(name: str) -> Optional[BaseHandler]:
    """A plugin by its operation name, or the generic handler.

    Scans :data:`PLUGINS` rather than a dict built at import time. An index snapshot goes
    stale the moment a plugin is registered later - from a test, or from a future plugin
    entry point - and a stale lookup silently falls back to generic handling, which looks
    like the plugin simply not working.
    """
    if name == GENERIC_HANDLER:
        return GENERIC
    for plugin in PLUGINS:
        if plugin.name == name:
            return plugin
    return None


def handler_for(mutation: Mutation) -> BaseHandler:
    """The handler that produced a mutation, for anchoring and reverting it."""
    return get_operation(mutation.handler) or GENERIC


def parse_event(event: CloudTrailEvent) -> List[Mutation]:
    """Every field change in one event: plugin-claimed first, then everything else.

    A plugin that declines an event it nominally owns does not suppress it. A
    ``ModifyInstanceAttribute`` that set ``disableApiTermination`` produces no plugin
    mutation and is picked up generically - which is exactly the change a plugin-only
    design would have dropped on the floor.
    """
    mutations: List[Mutation] = []
    claimed: FrozenSet[Tuple[str, ...]] = frozenset()
    for plugin in PLUGINS:
        found = plugin.parse(event)
        if found:
            mutations.extend(found)
            claimed = claimed | plugin.claimed_paths(event)

    # Safety net for plugin authors. `claimed_paths` is how a plugin tells the generic
    # layer to keep off its field, and forgetting it is an easy mistake - but whenever the
    # plugin files its field under the same path the parameter actually lives at (the
    # common case for a plain scalar), deduping by chain key catches the omission anyway.
    # `claimed_paths` is still needed for nested shapes like instanceType.value.
    already = {m.chain_key for m in mutations}
    for mutation in GENERIC.parse_unclaimed(
        event, claimed, plugin_handled=bool(mutations)
    ):
        if mutation.chain_key not in already:
            mutations.append(mutation)
            already.add(mutation.chain_key)
    return mutations


def relevant_event_names() -> FrozenSet[str]:
    """Event names worth a dedicated lookback query, on top of the window scan."""
    names: FrozenSet[str] = frozenset()
    for plugin in PLUGINS:
        names = names | plugin.relevant_event_names
    return names


def mutating_event_names() -> FrozenSet[str]:
    names: FrozenSet[str] = frozenset()
    for plugin in PLUGINS:
        names = names | plugin.mutating_event_names
    return names


def capability_of(
    handler: object,
    values_known: bool,
    anchor_proven: bool,
    has_manual_command: bool,
) -> Capability:
    """Which tier this field reached.

    Asking ``isinstance(handler, Actuator)`` rather than looking for an ``executable`` key
    in a revert dict. The tier is a property of what the handler can do, not of the shape
    of a return value - so adding a tier does not mean auditing every producer of that
    dict, and a handler cannot accidentally claim AUTO by returning the right key.
    """
    if not values_known or not anchor_proven:
        return Capability.DISCOVERED
    if isinstance(handler, Actuator):
        return Capability.AUTO
    if has_manual_command:
        return Capability.MANUAL
    return Capability.RECONSTRUCTED


def plugin_coverage() -> List[Dict[str, object]]:
    """What the plugins cover, for `rewind operations`."""
    return [
        {
            "operation": p.name,
            "resourceType": p.resource_type,
            "field": p.field_name,
            "events": sorted(p.mutating_event_names),
            "asynchronous": p.asynchronous,
            "configRecorded": p.config_resource_type is not None,
        }
        for p in PLUGINS
    ]


__all__ = [
    "GENERIC",
    "GENERIC_HANDLER",
    "GenericOperation",
    "MISMATCH",
    "BaseHandler",
    "PENDING",
    "PLUGINS",
    "VERIFIED",
    "get_operation",
    "handler_for",
    "is_mutating",
    "mutating_event_names",
    "parse_event",
    "performed",
    "plugin_coverage",
    "register_plugin",
    "relevant_event_names",
    "step",
]
