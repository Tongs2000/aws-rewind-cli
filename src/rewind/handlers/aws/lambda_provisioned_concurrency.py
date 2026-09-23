"""Lambda provisioned concurrency.

These events routinely arrive with an empty CloudTrail ``Resources`` index, so the
resource identity comes from ``requestParameters`` only.

``NONE`` is a real value here - "no provisioned concurrency config exists" - and is
distinct from an unproven anchor. It may only be inferred when the function's creation
is visible and the window was not truncated; otherwise absence of evidence would be
mistaken for evidence of absence.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ...trail import CloudTrailEvent
from ...domain import ABSENT, Mutation
from ..base import BaseHandler
from ..protocols import performed, step

PUT = "PutProvisionedConcurrencyConfig"
DELETE = "DeleteProvisionedConcurrencyConfig"
CREATE_FUNCTION = frozenset(
    {"CreateFunction20150331", "CreateFunction20141111", "CreateFunction"}
)
#: boto3 raises one of these when no provisioned concurrency config exists. That is a
#: real value (ABSENT), not a read failure.
NOT_FOUND_ERRORS = (
    "ProvisionedConcurrencyConfigNotFoundException",
    "ResourceNotFoundException",
)


def bare_function_name(raw: Any) -> Optional[str]:
    """Accept a bare name, a partial ARN or a full ARN; return the bare name."""
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("arn:"):
        parts = raw.split(":")
        # arn:aws:lambda:region:account:function:name[:qualifier]
        if len(parts) >= 7 and parts[5] == "function":
            return parts[6]
        return None
    return raw.split(":")[0]


def _identity(event: CloudTrailEvent) -> Optional[Tuple[str, str]]:
    name = bare_function_name(event.request_parameters.get("functionName"))
    qualifier = event.request_parameters.get("qualifier")
    if not name or not isinstance(qualifier, str) or not qualifier:
        return None
    return name, qualifier


def _requested(event: CloudTrailEvent) -> Optional[str]:
    raw = event.request_parameters.get("provisionedConcurrentExecutions")
    if raw is None:
        return None
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return None


class LambdaProvisionedConcurrencyOperation(BaseHandler):
    name = "SET_LAMBDA_PROVISIONED_CONCURRENCY"
    resource_type = "AWS::Lambda::Function"
    field_name = "provisionedConcurrency"
    mutating_event_names = frozenset({PUT, DELETE})
    relevant_event_names = frozenset({PUT, DELETE}) | CREATE_FUNCTION

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        return frozenset(
            {("provisionedConcurrentExecutions",), ("qualifier",), ("functionName",)}
        )

    def parse(self, event: CloudTrailEvent) -> List[Mutation]:
        if event.event_name not in self.mutating_event_names or not event.successful:
            return []
        identity = _identity(event)
        if identity is None:
            return []
        name, qualifier = identity
        if event.event_name == PUT:
            after = _requested(event)
            if after is None:
                return []
        else:
            after = ABSENT
        return [self._mutation(event, "%s:%s" % (name, qualifier), after)]

    def anchor_from_event(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name not in (PUT, DELETE):
            return None
        identity = _identity(event)
        if identity is None or "%s:%s" % identity != resource_id:
            return None
        if event.event_name == DELETE:
            return ABSENT
        return _requested(event)

    def creation_event_names(self) -> FrozenSet[str]:
        return CREATE_FUNCTION

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        if event.event_name not in CREATE_FUNCTION:
            return None
        name = bare_function_name(event.request_parameters.get("functionName"))
        if name is None or not resource_id.startswith(name + ":"):
            return None
        # A function is created without provisioned concurrency. This is only sound
        # because the caller has already confirmed no Put/Delete happened in between
        # and that the window is complete.
        return ABSENT

    def read_live_value(self, clients: Any, resource_id: str) -> str:
        name, qualifier = resource_id.split(":", 1)
        try:
            response = clients.client("lambda").get_provisioned_concurrency_config(
                FunctionName=name, Qualifier=qualifier
            )
        except Exception as exc:  # noqa: BLE001 - boto3 error classes are dynamic
            if type(exc).__name__ in NOT_FOUND_ERRORS:
                # No config is a real value, not a read failure.
                return ABSENT
            raise self.not_readable(resource_id, "%s: %s" % (type(exc).__name__, exc))
        requested = response.get("RequestedProvisionedConcurrentExecutions")
        if requested is None:
            raise self.not_readable(resource_id, "no requested concurrency reported")
        return str(int(requested))

    def read_live_detail(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        name, qualifier = resource_id.split(":", 1)
        try:
            response = clients.client("lambda").get_provisioned_concurrency_config(
                FunctionName=name, Qualifier=qualifier
            )
        except Exception:  # noqa: BLE001
            return {}
        return {"status": response.get("Status")}

    #: AWS Config records Lambda functions but not their provisioned concurrency
    #: configs, so config-history can never answer for this field. Leaving
    #: config_resource_type as None is what makes the resolver decline with a reason.
    config_resource_type = None

    def apply_revert(
        self, clients: Any, resource_id: str, target_value: str, wait: bool = True
    ) -> List[Dict[str, Any]]:
        name, qualifier = resource_id.split(":", 1)
        client = clients.client("lambda")
        base = {"FunctionName": name, "Qualifier": qualifier}
        if target_value == ABSENT:
            # There was no config before the session, so removing it is the revert.
            client.delete_provisioned_concurrency_config(**base)
            return [performed("lambda:DeleteProvisionedConcurrencyConfig", base)]
        params = dict(base, ProvisionedConcurrentExecutions=int(target_value))
        client.put_provisioned_concurrency_config(**params)
        return [performed("lambda:PutProvisionedConcurrencyConfig", params)]

    def revert_plan(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        name, qualifier = resource_id.split(":", 1)
        verify = step(
            "lambda:GetProvisionedConcurrencyConfig",
            {"FunctionName": name, "Qualifier": qualifier},
            expect=target_value,
        )
        if target_value == ABSENT:
            return {
                "operation": self.name,
                "parameters": {"functionName": name, "qualifier": qualifier, "value": None},
                "steps": [
                    step(
                        "lambda:DeleteProvisionedConcurrencyConfig",
                        {"FunctionName": name, "Qualifier": qualifier},
                    )
                ],
                "verify": verify,
            }
        return {
            "operation": self.name,
            "parameters": {
                "functionName": name,
                "qualifier": qualifier,
                "value": int(target_value),
            },
            "steps": [
                step(
                    "lambda:PutProvisionedConcurrencyConfig",
                    {
                        "FunctionName": name,
                        "Qualifier": qualifier,
                        "ProvisionedConcurrentExecutions": int(target_value),
                    },
                )
            ],
            "verify": verify,
        }
