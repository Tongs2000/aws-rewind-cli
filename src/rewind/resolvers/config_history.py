"""Anchor from AWS Config's recorded configuration history.

This is the real answer to CloudTrail's 90-day event-history limit: Config keeps
configuration items far longer, and where an account already records them the data is
**already paid for**. So the resolver probes for a recorder and uses it when present,
while never requiring one.

Two AWS-specific details are handled rather than glossed over:

* Config identifies an RDS instance by its ``DbiResourceId`` (``db-ABC…``), not by the
  ``DBInstanceIdentifier`` everything else uses. The resolver looks the id up with
  ``ListDiscoveredResources`` when a direct lookup finds nothing;
* Config does **not** record Lambda provisioned concurrency. Operations whose
  ``config_resource_type`` is None therefore get an explicit "Config cannot answer this"
  rather than a silent miss.

As with a local snapshot, a recorded observation only establishes the anchor if CloudTrail
shows nothing moved the field between the item's capture time and the session.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..domain import Anchor, Confidence, parse_time
from .base import AnchorRequest, AnchorResolver, intervening_change


class ConfigHistoryResolver(AnchorResolver):
    name = "config-history"
    description = "AWS Config configuration history, when the account records it"

    def __init__(self, client: Any = None, probe: bool = True, limit: int = 20) -> None:
        self.client = client
        self.limit = limit
        self._probed = not probe
        self._reason: Optional[str] = None if client else "not enabled (pass --use-config)"
        #: resolved DbiResourceId-style ids, cached per run
        self._config_ids: Dict[str, Optional[str]] = {}

    # -- availability -------------------------------------------------------

    def available(self) -> bool:
        if self.client is None:
            return False
        if not self._probed:
            self._probe()
        return self._reason is None

    def unavailable_reason(self) -> Optional[str]:
        if self.client is not None and not self._probed:
            self._probe()
        return self._reason

    def _probe(self) -> None:
        """Is a configuration recorder actually running? Never assume it is."""
        self._probed = True
        try:
            response = self.client.describe_configuration_recorder_status()
        except Exception as exc:  # noqa: BLE001 - AccessDenied is the common case
            self._reason = "AWS Config could not be queried: %s: %s" % (
                type(exc).__name__,
                exc,
            )
            return
        statuses = response.get("ConfigurationRecordersStatus") or []
        if not statuses:
            self._reason = "this account/region has no AWS Config configuration recorder"
        elif not any(s.get("recording") for s in statuses):
            self._reason = "the AWS Config configuration recorder is not recording"
        else:
            self._reason = None

    # -- resolution ---------------------------------------------------------

    def resolve(self, request: AnchorRequest) -> Anchor:
        operation = request.operation
        if operation.config_resource_type is None:
            return Anchor.unknown(
                source=self.name,
                note="AWS Config does not record %s" % request.field_name,
            )

        config_id = self._config_resource_id(
            operation.config_resource_type, request.resource_id
        )
        if config_id is None:
            return Anchor.unknown(
                source=self.name,
                note="AWS Config has no record of %s" % request.resource_id,
            )

        try:
            items = self._history(operation.config_resource_type, config_id, request)
        except Exception as exc:  # noqa: BLE001
            return Anchor.unknown(
                source=self.name,
                note="GetResourceConfigHistory failed: %s: %s" % (type(exc).__name__, exc),
            )

        boundary_time = request.first_mutation.event_time
        for item in items:
            captured = item.get("configurationItemCaptureTime")
            if captured is None:
                continue
            captured_at = parse_time(captured)
            if captured_at >= boundary_time:
                continue  # at or after the change: records the new value, not the old
            configuration = _configuration(item)
            value = operation.value_from_config(configuration)
            if value is None:
                continue
            intervening = intervening_change(request, captured_at)
            if intervening is not None:
                return Anchor.unknown(source=self.name, note=intervening)
            return Anchor(
                value=value,
                confidence=Confidence.HIGH,
                source=self.name,
                evidence_event_ids=[],
                note="AWS Config recorded %r at %s, with no CloudTrail event changing it "
                "before the session" % (value, captured_at.isoformat()),
            )
        return Anchor.unknown(
            source=self.name,
            note="no AWS Config item before the change records %s on %s"
            % (request.field_name, request.resource_id),
        )

    def _history(
        self, resource_type: str, config_id: str, request: AnchorRequest
    ) -> List[Dict[str, Any]]:
        response = self.client.get_resource_config_history(
            resourceType=resource_type,
            resourceId=config_id,
            laterTime=request.first_mutation.event_time,
            chronologicalOrder="Reverse",
            limit=self.limit,
        )
        return response.get("configurationItems") or []

    def _config_resource_id(self, resource_type: str, resource_id: str) -> Optional[str]:
        """Map our resource id onto the one Config indexes by.

        For EC2 the two are the same. For RDS, Config's resourceId is the DbiResourceId and
        our identifier is its resourceName, so it has to be looked up.
        """
        cache_key = "%s|%s" % (resource_type, resource_id)
        if cache_key in self._config_ids:
            return self._config_ids[cache_key]

        resolved: Optional[str] = resource_id
        try:
            response = self.client.list_discovered_resources(
                resourceType=resource_type, resourceIds=[resource_id]
            )
            if not (response.get("resourceIdentifiers") or []):
                by_name = self.client.list_discovered_resources(
                    resourceType=resource_type, resourceName=resource_id
                )
                identifiers = by_name.get("resourceIdentifiers") or []
                resolved = identifiers[0].get("resourceId") if identifiers else None
        except Exception:  # noqa: BLE001 - fall back to using the id as given
            resolved = resource_id

        self._config_ids[cache_key] = resolved
        return resolved


def _configuration(item: Dict[str, Any]) -> Dict[str, Any]:
    """Config delivers ``configuration`` as a JSON string."""
    raw = item.get("configuration")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}
