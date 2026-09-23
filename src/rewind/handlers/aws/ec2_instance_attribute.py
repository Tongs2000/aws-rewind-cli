"""EC2 instance attributes, as one parameterised plugin.

``ModifyInstanceAttribute`` sets a dozen different attributes through one event name, and
they all share the same request shape, the same Describe call and the same Modify call. So
this is one class instantiated once per attribute rather than a file per attribute - adding
another is a line in :data:`ATTRIBUTES`.

Two things vary per attribute and are declared rather than inferred:

* **whether EC2 requires the instance to be stopped.** ``instanceType``, ``ebsOptimized``
  and ``enaSupport`` do; ``disableApiTermination`` does not. Getting this wrong means the
  revert either fails outright or needlessly restarts a production instance, so it is
  explicit.
* **whether AWS Config records it.** Attributes that appear in ``DescribeInstances`` output
  are in a Config item; attribute-only ones are not, and claiming otherwise would send the
  config-history resolver looking for something that is never there.

``instanceType`` had its own module first, written before this one existed, and kept it on
the claim that its live value came from a different response shape. That was not true:
``DescribeInstanceAttribute`` answers every attribute here identically, and the data-loss
warning its revert carries is the warning *any* ``requires_stopped`` attribute gets. Folding
it in removed ~160 lines, a dead helper, and three ``LookupEvents`` lookback queries per
plan that no resolver ever consumed.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Tuple

from ...domain import Mutation, bool_str, optional_bool
from ...trail import CloudTrailEvent, dig, items_of
from ..base import BaseHandler
from ..protocols import performed, step
from ._ec2_power import modify_while_stopped, power_state

MODIFY = "ModifyInstanceAttribute"
RUN = "RunInstances"


class Attribute(NamedTuple):
    """One EC2 instance attribute this plugin can revert."""

    #: name as CloudTrail and the EC2 API spell it (leading lower case)
    name: str
    #: True when the value is a boolean rather than a string
    boolean: bool
    #: True when EC2 rejects the change unless the instance is stopped
    requires_stopped: bool
    #: True when the attribute appears in DescribeInstances, and so in an AWS Config item
    in_config: bool


ATTRIBUTES: Tuple[Attribute, ...] = (
    Attribute("instanceType", boolean=False, requires_stopped=True, in_config=True),
    Attribute("disableApiTermination", boolean=True, requires_stopped=False, in_config=False),
    Attribute("disableApiStop", boolean=True, requires_stopped=False, in_config=False),
    Attribute("sourceDestCheck", boolean=True, requires_stopped=False, in_config=True),
    Attribute(
        "instanceInitiatedShutdownBehavior",
        boolean=False,
        requires_stopped=False,
        in_config=False,
    ),
    Attribute("ebsOptimized", boolean=True, requires_stopped=True, in_config=True),
    Attribute("enaSupport", boolean=True, requires_stopped=True, in_config=True),
)


def api_name(attribute_name: str) -> str:
    """``disableApiTermination`` -> ``DisableApiTermination``.

    CloudTrail lowercases the leading letter of the EC2 API's own name, and boto3 wants it
    back. The whole mapping is that one character, so no lookup table is needed.
    """
    return attribute_name[:1].upper() + attribute_name[1:]


class Ec2InstanceAttributeOperation(BaseHandler):
    resource_type = "AWS::EC2::Instance"
    mutating_event_names = frozenset({MODIFY})
    relevant_event_names = frozenset({MODIFY, RUN})

    def __init__(self, attribute: Attribute) -> None:
        self.attribute = attribute
        self.name = "SET_EC2_%s" % _upper_snake(attribute.name)
        self.field_name = attribute.name
        self.config_resource_type = (
            "AWS::EC2::Instance" if attribute.in_config else None
        )

    # -- which changes are mine ---------------------------------------------

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        """The real request path, which differs from :attr:`field_path`.

        EC2 wraps every attribute as ``{"name": {"value": X}}`` while the chain is filed
        under the bare name, so the generic layer cannot dedupe this by chain key - the
        claim is what keeps the field from being reported twice.
        """
        return frozenset({(self.attribute.name, "value")})

    def parse(self, event: CloudTrailEvent) -> List[Mutation]:
        if event.event_name != MODIFY or not event.successful:
            return []
        value = self._rendered(dig(event.request_parameters, self.attribute.name, "value"))
        instance_id = event.request_parameters.get("instanceId")
        if value is None or not isinstance(instance_id, str) or not instance_id:
            return []
        return [self._mutation(event, instance_id, value)]

    # -- where the old value comes from -------------------------------------

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name != MODIFY:
            return None
        if event.request_parameters.get("instanceId") != resource_id:
            return None
        return self._rendered(dig(event.request_parameters, self.attribute.name, "value"))

    def creation_event_names(self) -> FrozenSet[str]:
        return frozenset({RUN})

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        """RunInstances can set some of these at launch - unwrapped, unlike Modify.

        The response is preferred where it carries the attribute, because it reports what
        each instance actually launched as. The request is the fallback: one call applies
        the same value to every instance it launches, so it is equivalent when present, and
        it is the only source for attributes ``RunInstances`` does not echo back.
        """
        if event.event_name != RUN:
            return None
        launched = dig(event.response_elements, "instancesSet", default={})
        for item in items_of(launched):
            if item.get("instanceId") != resource_id:
                continue
            return self._rendered(
                item.get(self.attribute.name, event.request_parameters.get(self.attribute.name))
            )
        return None

    def value_from_config(self, configuration: Dict[str, Any]) -> Optional[str]:
        if not self.attribute.in_config:
            return None
        return self._rendered(configuration.get(self.attribute.name))

    # -- live state ---------------------------------------------------------

    def read_live_value(self, clients: Any, resource_id: str) -> str:
        response = clients.client("ec2").describe_instance_attribute(
            InstanceId=resource_id, Attribute=self.attribute.name
        )
        raw = dig(response, api_name(self.attribute.name), "Value")
        value = self._rendered(raw)
        if value is None:
            raise self.not_readable(
                resource_id,
                "DescribeInstanceAttribute returned no %s" % self.attribute.name,
            )
        return value

    def read_live_detail(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        """Power state, but only where it changes what a revert will do."""
        if not self.attribute.requires_stopped:
            return {}
        return {"instanceState": power_state(clients, resource_id)}

    # -- performing the revert ----------------------------------------------

    def _modify_params(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        return {
            "InstanceId": resource_id,
            api_name(self.attribute.name): {"Value": self._typed(target_value)},
        }

    def revert_plan(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        params = self._modify_params(resource_id, target_value)
        steps = [step("ec2:ModifyInstanceAttribute", params)]
        body: Dict[str, Any] = {
            "operation": self.name,
            "parameters": {
                "instanceId": resource_id,
                self.attribute.name: self._typed(target_value),
            },
            "steps": steps,
            "verify": step(
                "ec2:DescribeInstanceAttribute",
                {"InstanceId": resource_id, "Attribute": self.attribute.name},
                expect=target_value,
            ),
        }
        if self.attribute.requires_stopped:
            body["steps"] = [
                step(
                    "ec2:StopInstances",
                    {"InstanceIds": [resource_id]},
                    condition="EC2 rejects a change to %s on a running instance"
                    % self.attribute.name,
                    waitFor="instance_stopped",
                ),
                steps[0],
                step(
                    "ec2:StartInstances",
                    {"InstanceIds": [resource_id]},
                    condition="only if the instance is running when the revert starts",
                    waitFor="instance_running",
                ),
            ]
            body["warning"] = (
                "%s can only be changed while the instance is stopped, so reverting it "
                "stops and restarts the instance; instance-store data is lost and public "
                "IPv4 addresses can change" % self.attribute.name
            )
        return body

    def apply_revert(
        self, clients: Any, resource_id: str, target_value: str, wait: bool = True
    ) -> List[Dict[str, Any]]:
        params = self._modify_params(resource_id, target_value)
        if self.attribute.requires_stopped:
            return modify_while_stopped(clients, resource_id, params, wait=wait)
        clients.client("ec2").modify_instance_attribute(**params)
        return [performed("ec2:ModifyInstanceAttribute", params)]

    # -- value shaping ------------------------------------------------------

    def _rendered(self, raw: Any) -> Optional[str]:
        """One display string, matching how every other value in the tool is rendered."""
        if raw is None:
            return None
        if self.attribute.boolean:
            flag = optional_bool(raw)
            return None if flag is None else bool_str(flag)
        return str(raw) if str(raw) else None

    def _typed(self, value: str) -> Any:
        """Back to what boto3 expects."""
        return value == "true" if self.attribute.boolean else value


def _upper_snake(name: str) -> str:
    out: List[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.upper())
    return "".join(out)


def all_attribute_operations() -> List[Ec2InstanceAttributeOperation]:
    return [Ec2InstanceAttributeOperation(a) for a in ATTRIBUTES]
