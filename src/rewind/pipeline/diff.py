"""Compare a plan against live state.

Two independent questions are answered per chain, and keeping them separate is the point:

* **has anything else touched this field since the session?** Answered by comparing the
  live value with the chain's ``netAfter``. This needs *no history at all*, so it works
  even when the anchor is UNKNOWN - the tool can always say whether a resource has been
  meddled with, even where it cannot say what the old value was;
* **is the old value known?** Answered by the anchor, resolved back in ``plan``.

Everything here is read-only. There is no code path in this module that mutates anything.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..trail import EventSource
from ..errors import LiveStateError
from ..domain import iso
from .plan_build import extract_mutations
from ..handlers import get_operation
from ..domain import Chain, Plan

UTC = timezone.utc


class Verdict(str, enum.Enum):
    """What a revert of this chain would mean right now."""

    #: live state matches the session's outcome and the old value is known
    REVERTIBLE = "REVERTIBLE"
    #: live state matches the session's outcome but the old value is not proven
    UNPROVEN = "UNPROVEN"
    #: somebody already put this field back to its pre-session value
    ALREADY_REVERTED = "ALREADY_REVERTED"
    #: the session's net effect was zero, so there was never anything to undo
    ALREADY_AT_ORIGINAL = "ALREADY_AT_ORIGINAL"
    #: live state is neither the session's outcome nor the original - something else changed it
    CONFLICT = "CONFLICT"
    #: the field's current value could not be read
    UNREADABLE = "UNREADABLE"
    #: no plugin knows how to read this field, so drift cannot be checked at all
    UNCHECKABLE = "UNCHECKABLE"


#: Verdicts that mean "a revert would do something, and is safe to attempt".
ACTIONABLE = (Verdict.REVERTIBLE,)
#: Verdicts that mean "do not touch this without a human deciding first".
BLOCKING = (Verdict.CONFLICT, Verdict.UNREADABLE, Verdict.UNCHECKABLE)
#: Verdicts reached without ever seeing the field's current value.
NOT_COMPARED = (Verdict.UNREADABLE, Verdict.UNCHECKABLE)

#: AWS error codes that mean "your credentials, not this resource". They are worth telling
#: apart: one bad resource is a fact about that resource, while expired credentials make
#: *every* live read fail - and a diff that reports "no drift" in that state is lying.
CREDENTIAL_ERRORS = (
    "RequestExpired", "ExpiredToken", "ExpiredTokenException", "TokenRefreshRequired",
    "InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch",
    "AuthFailure", "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
)


@dataclass
class ChainDiff:
    chain: Chain
    verdict: Verdict
    live_value: Optional[str]
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)
    blame: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def drift_free(self) -> bool:
        """True when nothing outside the session has changed this field."""
        return self.verdict in (Verdict.REVERTIBLE, Verdict.UNPROVEN, Verdict.ALREADY_AT_ORIGINAL)

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "chainId": self.chain.chain_id,
            "resourceType": self.chain.resource_type,
            "resourceId": self.chain.resource_id,
            "field": self.chain.field_name,
            "handler": self.chain.handler,
            "capability": self.chain.capability.value,
            "verdict": self.verdict.value,
            "confidence": self.chain.confidence.value,
            "planBefore": self.chain.net_before,
            "planAfter": self.chain.net_after,
            "liveValue": self.live_value,
            "driftFree": self.drift_free,
            "reason": self.reason,
        }
        if self.detail:
            body["liveDetail"] = self.detail
        if self.blame:
            body["blame"] = self.blame
        return body


@dataclass
class PlanDiff:
    plan: Plan
    entries: List[ChainDiff]
    checked_at: datetime
    blame_attempted: bool = False

    @property
    def conflicts(self) -> List[ChainDiff]:
        return [e for e in self.entries if e.verdict is Verdict.CONFLICT]

    @property
    def actionable(self) -> List[ChainDiff]:
        return [e for e in self.entries if e.verdict in ACTIONABLE]

    @property
    def compared(self) -> List[ChainDiff]:
        """Entries whose live value was actually read. "No drift" may only be said of these."""
        return [e for e in self.entries if e.verdict not in NOT_COMPARED]

    @property
    def credential_failures(self) -> List[ChainDiff]:
        """Unreadable entries whose cause is the caller's credentials, not the resource."""
        return [
            e
            for e in self.entries
            if e.verdict is Verdict.UNREADABLE
            and any(code in e.reason for code in CREDENTIAL_ERRORS)
        ]

    @property
    def summary(self) -> Dict[str, int]:
        counts = {verdict.value: 0 for verdict in Verdict}
        for entry in self.entries:
            counts[entry.verdict.value] += 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planPath": self.plan.path,
            "planGeneratedAt": iso(self.plan.generated_at),
            "checkedAt": iso(self.checked_at),
            "identity": self.plan.identity,
            "region": self.plan.region,
            "summary": self.summary,
            "driftFree": bool(self.compared) and all(e.drift_free for e in self.compared),
            "fieldsCompared": len(self.compared),
            "credentialFailures": len(self.credential_failures),
            "blameAttempted": self.blame_attempted,
            "entries": [e.to_dict() for e in self.entries],
        }


def classify(
    chain: Chain, live_value: Optional[str], read_error: Optional[str]
) -> ChainDiff:
    """Decide the verdict for one chain. Pure - no AWS, no I/O."""
    if read_error is not None:
        return ChainDiff(
            chain=chain,
            verdict=Verdict.UNREADABLE,
            live_value=None,
            reason=read_error,
        )

    if chain.net_no_op:
        if live_value == chain.net_after:
            return ChainDiff(
                chain=chain,
                verdict=Verdict.ALREADY_AT_ORIGINAL,
                live_value=live_value,
                reason="the session's net effect was zero and the field still holds that "
                "value; there is nothing to revert",
            )
        return ChainDiff(
            chain=chain,
            verdict=Verdict.CONFLICT,
            live_value=live_value,
            reason="the session left this field at %r but it now reads %r"
            % (chain.net_after, live_value),
        )

    if live_value == chain.net_after:
        if chain.anchor_proven:
            return ChainDiff(
                chain=chain,
                verdict=Verdict.REVERTIBLE,
                live_value=live_value,
                reason="unchanged since the session; a revert would restore %r"
                % chain.net_before,
            )
        return ChainDiff(
            chain=chain,
            verdict=Verdict.UNPROVEN,
            live_value=live_value,
            reason="unchanged since the session, so nothing else has touched it - but the "
            "pre-session value is not proven, so the target must be supplied explicitly",
        )

    if chain.net_before is not None and live_value == chain.net_before:
        return ChainDiff(
            chain=chain,
            verdict=Verdict.ALREADY_REVERTED,
            live_value=live_value,
            reason="the field already holds its pre-session value %r" % chain.net_before,
        )

    return ChainDiff(
        chain=chain,
        verdict=Verdict.CONFLICT,
        live_value=live_value,
        reason="the session left this field at %r but it now reads %r; something outside "
        "this plan changed it" % (chain.net_after, live_value),
    )


def diff_plan(
    plan: Plan,
    clients: Any,
    source: Optional[EventSource] = None,
    now: Optional[datetime] = None,
) -> PlanDiff:
    """Read every planned field's live value and classify it.

    When ``source`` is supplied, conflicts are attributed by re-reading CloudTrail for
    the period since the plan was generated. That query is only issued if a conflict was
    actually found, so a clean diff costs nothing extra.
    """
    now = now or datetime.now(tz=UTC)
    entries: List[ChainDiff] = []

    for chain in plan.chains:
        operation = get_operation(chain.handler)
        if operation is None:
            entries.append(
                ChainDiff(
                    chain=chain,
                    verdict=Verdict.UNREADABLE,
                    live_value=None,
                    reason="this build does not have handler %r" % chain.handler,
                )
            )
            continue
        if not chain.plugin_backed:
            # Honest about the limit: without a plugin there is no Describe/Get call to
            # read this field, so the tool cannot say whether it has drifted.
            entries.append(
                ChainDiff(
                    chain=chain,
                    verdict=Verdict.UNCHECKABLE,
                    live_value=None,
                    reason="no plugin knows which Describe/Get call reads %s on %s, so "
                    "drift cannot be checked. The plan still records what changed."
                    % (chain.field_name, chain.resource_id),
                )
            )
            continue

        live_value: Optional[str] = None
        read_error: Optional[str] = None
        detail: Dict[str, Any] = {}
        try:
            live_value = operation.read_live_value(clients, chain.resource_id)
        except LiveStateError as exc:
            read_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad resource must not abort the diff
            read_error = "%s: %s" % (type(exc).__name__, exc)

        if read_error is None:
            try:
                detail = operation.read_live_detail(clients, chain.resource_id)
            except Exception:  # noqa: BLE001 - detail is a nicety, never load-bearing
                detail = {}

        entry = classify(chain, live_value, read_error)
        entry.detail = detail
        entries.append(entry)

    result = PlanDiff(plan=plan, entries=entries, checked_at=now)
    if source is not None and result.conflicts:
        attribute_conflicts(result, source, now)
        result.blame_attempted = True
    return result


def attribute_conflicts(result: PlanDiff, source: EventSource, now: datetime) -> None:
    """Name who changed the conflicted fields after the plan was generated.

    Reuses the ordinary scan machinery: fetch the relevant event names for the period
    since the plan, then keep the mutations that land on a conflicted (resource, field).
    """
    conflicts = result.conflicts
    wanted = {(e.chain.resource_id, e.chain.field_name): e for e in conflicts}
    event_names = set()
    for entry in conflicts:
        operation = get_operation(entry.chain.handler)
        if operation is not None:
            event_names |= set(operation.mutating_event_names)
        if entry.chain.event_name:
            event_names.add(entry.chain.event_name)

    events, _ = source.named_events(result.plan.generated_at, now, sorted(event_names))
    for mutation in extract_mutations(events):
        entry = wanted.get((mutation.resource_id, mutation.field_name))
        if entry is None:
            continue
        entry.blame.append(
            {
                "eventId": mutation.event_id,
                "eventTime": iso(mutation.event_time),
                "eventName": mutation.event_name,
                "identity": mutation.identity,
                "setTo": mutation.after,
            }
        )
