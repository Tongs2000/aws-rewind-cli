"""Normalising one CloudTrail record, and deciding whose change it was."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..domain import parse_time
from .inspect import dig

@dataclass(frozen=True)
class CloudTrailEvent:
    event_id: str
    event_time: datetime
    event_name: str
    event_source: str
    aws_region: str
    identity_label: str
    identity_candidates: Tuple[str, ...]
    request_parameters: Dict[str, Any]
    response_elements: Dict[str, Any]
    error_code: Optional[str]
    resources: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def successful(self) -> bool:
        """A failed API call is not a state transition."""
        return not self.error_code

    @property
    def sort_key(self) -> Tuple[datetime, str]:
        return (self.event_time, self.event_id)

    def matches_identity(self, identity: str) -> bool:
        return identity_matches(self.identity_candidates, identity)


def identity_matches(candidates: Sequence[str], identity: str) -> bool:
    """Match a workload identity against an event's identity fields.

    Exact match on any candidate wins; otherwise a substring match is accepted so a
    role-session name matches the assumed-role ARN that contains it.
    """
    wanted = (identity or "").strip().lower()
    if not wanted:
        return False
    lowered = [c.lower() for c in candidates if c]
    if wanted in lowered:
        return True
    return any(wanted in c for c in lowered)


def _identity_candidates(user_identity: Dict[str, Any]) -> Tuple[str, ...]:
    raw: List[str] = []
    for key in ("userName", "arn", "principalId", "invokedBy"):
        value = user_identity.get(key)
        if isinstance(value, str):
            raw.append(value)
    issuer = dig(user_identity, "sessionContext", "sessionIssuer", default={}) or {}
    for key in ("userName", "arn"):
        value = issuer.get(key)
        if isinstance(value, str):
            raw.append(value)
    expanded: List[str] = []
    for value in raw:
        expanded.append(value)
        if "/" in value:
            expanded.append(value.rsplit("/", 1)[-1])
        if ":" in value and not value.startswith("arn:"):
            # principalId looks like "AROAEXAMPLE:role-session-name"
            expanded.append(value.rsplit(":", 1)[-1])
    seen: List[str] = []
    for value in expanded:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def _identity_label(user_identity: Dict[str, Any], fallback: str) -> str:
    for key in ("arn", "userName", "principalId"):
        value = user_identity.get(key)
        if isinstance(value, str) and value:
            return value
    issuer_arn = dig(user_identity, "sessionContext", "sessionIssuer", "arn")
    if isinstance(issuer_arn, str) and issuer_arn:
        return issuer_arn
    return fallback


def from_lookup_record(record: Dict[str, Any]) -> CloudTrailEvent:
    """Normalise one ``LookupEvents`` record.

    CloudTrail returns ``CloudTrailEvent`` as a JSON string; fixtures keep it as a
    nested object for readability. Both are accepted.
    """
    payload = record.get("CloudTrailEvent") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    user_identity = payload.get("userIdentity") or {}
    return CloudTrailEvent(
        event_id=payload.get("eventID") or record.get("EventId") or "",
        event_time=parse_time(payload.get("eventTime") or record.get("EventTime")),
        event_name=payload.get("eventName") or record.get("EventName") or "",
        event_source=payload.get("eventSource") or record.get("EventSource") or "",
        aws_region=payload.get("awsRegion") or "",
        identity_label=_identity_label(user_identity, record.get("Username") or ""),
        identity_candidates=_identity_candidates(user_identity)
        or tuple(filter(None, [record.get("Username")])),
        request_parameters=payload.get("requestParameters") or {},
        response_elements=payload.get("responseElements") or {},
        error_code=payload.get("errorCode"),
        resources=list(record.get("Resources") or []),
        raw=payload,
    )


def sort_events(events: Iterable[CloudTrailEvent]) -> List[CloudTrailEvent]:
    return sorted(events, key=lambda e: e.sort_key)
