"""EC2 detailed monitoring.

One ``MonitorInstances`` call can cover several instances, and each instance is its own
chain - because their previous states may differ, and a revert has to restore each one
individually.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ...domain import Mutation, optional_bool
from ...errors import ResourceGone
from ...trail import CloudTrailEvent, dig, instance_ids, items_of
from ..base import BaseHandler
from ..protocols import performed, step
from ._ec2_power import TERMINAL_STATES

MONITOR = "MonitorInstances"
UNMONITOR = "UnmonitorInstances"
RUN = "RunInstances"

ENABLED = "enabled"
DISABLED = "disabled"
#: Transitional states settle onto one of the two stable values.
_SETTLED = {
    "enabled": ENABLED,
    "pending": ENABLED,
    "disabled": DISABLED,
    "disabling": DISABLED,
}


def settle(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return _SETTLED.get(raw.strip().lower())
    flag = optional_bool(raw)
    return None if flag is None else (ENABLED if flag else DISABLED)


class Ec2DetailedMonitoringOperation(BaseHandler):
    name = "SET_EC2_DETAILED_MONITORING"
    resource_type = "AWS::EC2::Instance"
    field_name = "monitoring"
    mutating_event_names = frozenset({MONITOR, UNMONITOR})
    relevant_event_names = frozenset({MONITOR, UNMONITOR, RUN})
    config_resource_type = "AWS::EC2::Instance"
    #: DescribeInstances lags UnmonitorInstances by a moment; see BaseHandler.read_may_lag.
    read_may_lag = True

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        # The value is encoded in the event name, not in a parameter, so nothing in
        # requestParameters is consumed - but the instancesSet is identifiers, which the
        # generic layer already excludes.
        return frozenset()

    def parse(self, event: CloudTrailEvent) -> List[Mutation]:
        if event.event_name not in self.mutating_event_names or not event.successful:
            return []
        after = ENABLED if event.event_name == MONITOR else DISABLED
        targets = instance_ids(dig(event.request_parameters, "instancesSet", default={}))
        return [self._mutation(event, instance_id, after) for instance_id in targets]

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name not in (MONITOR, UNMONITOR):
            return None
        targets = instance_ids(dig(event.request_parameters, "instancesSet", default={}))
        if resource_id not in targets:
            return None
        return ENABLED if event.event_name == MONITOR else DISABLED

    def creation_event_names(self) -> FrozenSet[str]:
        return frozenset({RUN})

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name != RUN:
            return None
        launched = items_of(dig(event.response_elements, "instancesSet", default={}))
        for item in launched:
            if item.get("instanceId") != resource_id:
                continue
            state = settle(dig(item, "monitoring", "state"))
            if state is not None:
                return state
            # The launch event mentions the instance but not its monitoring state. The
            # EC2 default is "disabled", but a default is not evidence - do not invent.
            return settle(dig(event.request_parameters, "monitoring", "enabled"))
        return None

    def read_live_value(self, clients: Any, resource_id: str) -> str:
        response = clients.client("ec2").describe_instances(InstanceIds=[resource_id])
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                if instance.get("InstanceId") != resource_id:
                    continue
                # The same response already carries the power state, so refusing a
                # terminated instance costs nothing here. It answers with the monitoring
                # state it had when it died, which is not a value anybody can restore.
                power = str(dig(instance, "State", "Name", default=""))
                if power in TERMINAL_STATES:
                    raise ResourceGone(
                        "cannot read %s: the instance is %s, so its remembered value "
                        "cannot be restored" % (resource_id, power)
                    )
                state = settle(dig(instance, "Monitoring", "State"))
                if state is None:
                    raise self.not_readable(resource_id, "no monitoring state reported")
                return state
        raise self.not_readable(resource_id, "instance not found")

    def value_from_config(self, configuration: Dict[str, Any]) -> Optional[str]:
        return settle(dig(configuration, "monitoring", "state"))

    def apply_revert(
        self, clients: Any, resource_id: str, target_value: str, wait: bool = True
    ) -> List[Dict[str, Any]]:
        ec2 = clients.client("ec2")
        params = {"InstanceIds": [resource_id]}
        if target_value == DISABLED:
            ec2.unmonitor_instances(**params)
            return [performed("ec2:UnmonitorInstances", params)]
        ec2.monitor_instances(**params)
        return [performed("ec2:MonitorInstances", params)]

    def revert_plan(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        api = "ec2:UnmonitorInstances" if target_value == DISABLED else "ec2:MonitorInstances"
        return {
            "operation": self.name,
            "parameters": {"instanceIds": [resource_id], "enabled": target_value == ENABLED},
            "steps": [step(api, {"InstanceIds": [resource_id]})],
            "verify": step(
                "ec2:DescribeInstances",
                {"InstanceIds": [resource_id]},
                expect=target_value,
            ),
        }
