"""Writing and reading a plan document.

One :class:`~rewind.domain.Chain` type serves both directions. Everything the loader can
derive - the confidence, the capability, the change count, the first and last change times -
is recomputed from the chain's own parts rather than read back, so a hand-edited file cannot
make a chain disagree with itself. The fields are still *written*, because a human reading
the JSON wants them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..domain import (
    PLAN_FORMAT_VERSION,
    Anchor,
    Capability,
    Chain,
    Change,
    Confidence,
    FieldRef,
    Plan,
    Query,
)
from .document import (
    DocumentError,
    read_json,
    require,
    require_list,
    require_mapping,
    require_object,
    require_version,
    required_time,
)

REGENERATE = "re-run `rewind plan -o` to regenerate it"

#: Everything a chain cannot be rebuilt without.
REQUIRED_CHAIN_KEYS = ("chainId", "resourceId", "field", "confidence", "changes")


class PlanFileError(DocumentError):
    """The plan document is missing, unreadable, or not one this build understands."""


def dump(plan: Plan) -> Dict[str, Any]:
    return plan.to_dict()


def parse(body: Any, path: Optional[str] = None) -> Plan:
    body = require_object(body, "plan", PlanFileError)
    require_version(
        body, "rewindPlanVersion", PLAN_FORMAT_VERSION, "plan", REGENERATE, PlanFileError
    )
    query = require(body, "query", "plan", PlanFileError)
    if not isinstance(query, dict):
        raise PlanFileError("plan query must be an object")

    chains = [
        _chain(require_mapping(raw, index, "chain", PlanFileError), index)
        for index, raw in enumerate(require_list(body, "chains", "plan", PlanFileError))
    ]
    return Plan(
        query=Query(
            identity=require(query, "identity", "plan query", PlanFileError),
            region=require(query, "region", "plan query", PlanFileError),
            start_time=required_time(
                require(query, "startTime", "plan query", PlanFileError),
                "plan query", PlanFileError,
            ),
            end_time=required_time(
                require(query, "endTime", "plan query", PlanFileError),
                "plan query", PlanFileError,
            ),
        ),
        generated_at=required_time(
            require(body, "generatedAt", "plan", PlanFileError), "plan", PlanFileError
        ),
        chains=chains,
        warnings=list(body.get("warnings") or []),
        stats=dict(body.get("stats") or {}),
        tool_version=str((body.get("tool") or {}).get("version", "unknown")),
        path=path,
    )


def _chain(raw: Dict[str, Any], index: int) -> Chain:
    where = "chain %d" % index
    for key in REQUIRED_CHAIN_KEYS:
        require(raw, key, where, PlanFileError)
    try:
        confidence = Confidence(raw["confidence"])
    except ValueError:
        raise PlanFileError("%s has an unknown confidence %r" % (where, raw["confidence"]))
    try:
        capability = Capability(raw.get("capability", Capability.AUTO.value))
    except ValueError:
        raise PlanFileError("%s has an unknown capability %r" % (where, raw["capability"]))

    anchor_body = raw.get("anchor") or {}
    changes = [
        _change(require_mapping(c, i, "%s change" % where, PlanFileError), i, where)
        for i, c in enumerate(raw["changes"])
    ]
    if not changes:
        raise PlanFileError("%s has no changes; a chain describes at least one" % where)

    return Chain(
        field=FieldRef(
            resource_id=raw["resourceId"],
            path=tuple(raw.get("fieldPath") or (raw["field"],)),
            resource_type=raw.get("resourceType", ""),
        ),
        handler=raw.get("handler", ""),
        event_source=raw.get("eventSource", ""),
        event_name=raw.get("eventName", ""),
        changes=changes,
        anchor=Anchor(
            value=anchor_body.get("value", raw.get("netBefore")),
            confidence=confidence,
            source=anchor_body.get("source", "unknown"),
            evidence_event_ids=list(anchor_body.get("evidenceEventIds") or []),
            note=anchor_body.get("note"),
        ),
        revert=dict(raw.get("revert") or {}),
        notes=list(raw.get("notes") or []),
        capability=capability,
    )


def _change(raw: Dict[str, Any], index: int, where: str) -> Change:
    for key in ("eventId", "eventTime", "after"):
        if key not in raw:
            raise PlanFileError("%s change %d is missing %r" % (where, index, key))
    return Change(
        sequence=int(raw.get("sequence", index + 1)),
        event_id=raw["eventId"],
        event_time=required_time(raw["eventTime"], "%s change %d" % (where, index), PlanFileError),
        event_name=raw.get("eventName", ""),
        identity=raw.get("identity", ""),
        before=raw.get("before"),
        after=raw.get("after"),
    )


def load(path: str) -> Plan:
    return parse(read_json(path, "plan", PlanFileError), path=str(path))
