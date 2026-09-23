"""Shared EC2 orchestration: attributes that can only be changed while stopped.

``instanceType``, ``ebsOptimized``, ``enaSupport``, ``userData`` and others are rejected by
EC2 unless the instance is stopped. Reverting one is therefore three calls, not one, and the
sequence has to leave the instance in the power state it was found in.

Two plugins need exactly this, so it lives here rather than being written twice - a
duplicated stop/start dance is the kind of thing that drifts apart and then only one copy
gets a fix.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...errors import ResourceGone
from ...trail import dig
from ..protocols import performed

#: The only settled state in which a restricted attribute may be modified.
STOPPED = "stopped"

#: States a resource never comes back from. An instance here still answers
#: ``DescribeInstanceAttribute`` - it reports the type it had when it died - so reading the
#: field is not enough to notice. Seen live: a plan built from CloudTrail was reverted
#: against two terminated instances, ``diff`` called both REVERTIBLE with the right value,
#: and ``StopInstances`` then failed with IncorrectInstanceState. A resource that no longer
#: exists is neither drift nor a conflict; it has to be refused before the first write.
TERMINAL_STATES = ("shutting-down", "terminated")


def power_state(clients: Any, instance_id: str) -> str:
    """Current power state, or an empty string when it cannot be read."""
    response = clients.client("ec2").describe_instances(InstanceIds=[instance_id])
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            if instance.get("InstanceId") == instance_id:
                return str(dig(instance, "State", "Name", default=""))
    return ""


def ensure_present(clients: Any, instance_id: str) -> str:
    """The instance's power state, or a LiveStateError if it is gone or on its way out.

    Costs one ``DescribeInstances`` per field read, which is the price of not issuing a write
    against a resource that no longer exists.
    """
    state = power_state(clients, instance_id)
    if not state:
        raise ResourceGone(
            "cannot read %s: the instance does not exist" % instance_id
        )
    if state in TERMINAL_STATES:
        raise ResourceGone(
            "cannot read %s: the instance is %s, so its remembered value cannot be restored"
            % (instance_id, state)
        )
    return state


def modify_while_stopped(
    clients: Any,
    instance_id: str,
    modify_params: Dict[str, Any],
    wait: bool = True,
) -> List[Dict[str, Any]]:
    """Stop if needed, modify, then restore the power state that was found.

    The pre-session power state is not part of the chain being reverted, so this restores
    what it observes: an instance found running is running afterwards, one found stopped
    stays stopped. Changing a neighbouring property of the resource is not this revert's
    business.
    """
    ec2 = clients.client("ec2")
    was_running = power_state(clients, instance_id) != STOPPED
    calls: List[Dict[str, Any]] = []

    if was_running:
        ec2.stop_instances(InstanceIds=[instance_id])
        calls.append(performed("ec2:StopInstances", {"InstanceIds": [instance_id]}))
        if wait:
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])

    ec2.modify_instance_attribute(**modify_params)
    calls.append(performed("ec2:ModifyInstanceAttribute", modify_params))

    if was_running:
        ec2.start_instances(InstanceIds=[instance_id])
        calls.append(performed("ec2:StartInstances", {"InstanceIds": [instance_id]}))
        if wait:
            ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    return calls
