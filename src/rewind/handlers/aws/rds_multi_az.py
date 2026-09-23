"""RDS Multi-AZ.

The interesting case in the whole tool: ``ModifyDBInstance`` echoes the *current*
(pre-change) ``multiAZ`` in ``responseElements`` while putting the requested value under
``pendingModifiedValues``. So this operation can anchor itself from the change event
alone - no history, and therefore no exposure to CloudTrail's 90-day retention.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ...domain import Mutation, bool_str, optional_bool
from ...trail import CloudTrailEvent, dig
from ..base import BaseHandler
from ..protocols import Verification, MISMATCH, PENDING, VERIFIED, performed, step

MODIFY = "ModifyDBInstance"
CREATE = "CreateDBInstance"


def _requested(event: CloudTrailEvent) -> Optional[bool]:
    return optional_bool(event.request_parameters.get("multiAZ"))


class RdsMultiAzOperation(BaseHandler):
    name = "SET_RDS_MULTI_AZ"
    resource_type = "AWS::RDS::DBInstance"
    field_name = "multiAZ"
    mutating_event_names = frozenset({MODIFY})
    relevant_event_names = frozenset({MODIFY, CREATE})
    asynchronous = True
    config_resource_type = "AWS::RDS::DBInstance"

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        return frozenset({("multiAZ",)})

    def parse(self, event: CloudTrailEvent) -> List[Mutation]:
        if event.event_name != MODIFY or not event.successful:
            return []
        requested = _requested(event)
        identifier = event.request_parameters.get("dBInstanceIdentifier")
        if requested is None:
            return []  # this Modify changed something else
        if not isinstance(identifier, str) or not identifier:
            return []
        return [self._mutation(event, identifier, bool_str(requested))]

    def response_anchor(self, mutation: Mutation) -> Optional[str]:
        """Pre-change value from the change event's own response."""
        current = optional_bool(mutation.response_elements.get("multiAZ"))
        if current is None:
            return None
        pending = optional_bool(dig(mutation.response_elements, "pendingModifiedValues", "multiAZ"))
        if bool_str(current) != mutation.after:
            # responseElements still reports the old value: exactly what we want.
            return bool_str(current)
        if pending is None:
            # Already at the requested value with nothing pending: the call changed
            # nothing, so before equals after. Still a fact worth recording.
            return bool_str(current)
        return None

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name != MODIFY:
            return None
        if event.request_parameters.get("dBInstanceIdentifier") != resource_id:
            return None
        value = _requested(event)
        return None if value is None else bool_str(value)

    def creation_event_names(self) -> FrozenSet[str]:
        return frozenset({CREATE})

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name != CREATE:
            return None
        if event.request_parameters.get("dBInstanceIdentifier") != resource_id:
            return None
        value = _requested(event)
        return None if value is None else bool_str(value)

    def _describe(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        response = clients.client("rds").describe_db_instances(
            DBInstanceIdentifier=resource_id
        )
        for instance in response.get("DBInstances", []):
            return instance
        raise self.not_readable(resource_id, "db instance not found")

    def read_live_value(self, clients: Any, resource_id: str) -> str:
        """The value the instance is converging towards.

        A Multi-AZ change takes minutes, during which RDS reports the *old* value with
        the new one under PendingModifiedValues. Comparing the applied value would make
        that convergence window look exactly like somebody else having changed the
        instance, so the effective (pending-aware) value is what counts.
        """
        instance = self._describe(clients, resource_id)
        applied = bool(instance.get("MultiAZ"))
        pending = optional_bool(dig(instance, "PendingModifiedValues", "MultiAZ"))
        return bool_str(applied if pending is None else pending)

    def read_live_detail(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        instance = self._describe(clients, resource_id)
        pending = optional_bool(dig(instance, "PendingModifiedValues", "MultiAZ"))
        detail: Dict[str, Any] = {
            "appliedMultiAZ": bool_str(instance.get("MultiAZ")),
            "status": instance.get("DBInstanceStatus"),
        }
        if pending is not None:
            detail["pendingMultiAZ"] = bool_str(pending)
            detail["note"] = "a Multi-AZ modification is still being applied"
        return detail

    def value_from_config(self, configuration: Dict[str, Any]) -> Optional[str]:
        value = optional_bool(configuration.get("multiAZ"))
        return None if value is None else bool_str(value)

    def apply_revert(
        self, clients: Any, resource_id: str, target_value: str, wait: bool = True
    ) -> List[Dict[str, Any]]:
        params = {
            "DBInstanceIdentifier": resource_id,
            "MultiAZ": target_value == "true",
            "ApplyImmediately": True,
        }
        clients.client("rds").modify_db_instance(**params)
        return [performed("rds:ModifyDBInstance", params)]

    def verify_revert(
        self, clients: Any, resource_id: str, target_value: str
    ) -> Verification:
        """Stricter than read_live_value: has the change actually been applied?

        Immediately after a Multi-AZ modification the applied value is still the old one,
        so claiming VERIFIED would be a lie. PENDING says "accepted, still converging",
        and re-running the revert polls it.
        """
        instance = self._describe(clients, resource_id)
        applied = bool_str(instance.get("MultiAZ"))
        pending = optional_bool(dig(instance, "PendingModifiedValues", "MultiAZ"))
        if applied == target_value and pending is None:
            return Verification(VERIFIED, applied)
        if pending is not None and bool_str(pending) == target_value:
            return Verification(PENDING, applied)
        return Verification(MISMATCH, applied)

    def revert_plan(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        target = target_value == "true"
        return {
            "operation": self.name,
            "parameters": {"dBInstanceIdentifier": resource_id, "multiAZ": target},
            "asynchronous": True,
            "steps": [
                step(
                    "rds:ModifyDBInstance",
                    {
                        "DBInstanceIdentifier": resource_id,
                        "MultiAZ": target,
                        "ApplyImmediately": True,
                    },
                    note="asynchronous; reported SUBMITTED until a Describe confirms it",
                )
            ],
            "verify": step(
                "rds:DescribeDBInstances",
                {"DBInstanceIdentifier": resource_id},
                expect=target_value,
            ),
        }
