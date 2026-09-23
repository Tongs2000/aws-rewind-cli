"""Performing a revert.

This is the only module in the tool that mutates anything, and it is gated three ways:

1. **dry run by default.** Without ``--confirm`` nothing is called; the operator sees the
   exact APIs that would run;
2. **a fresh check immediately before each step.** Not the plan's view, not the diff's
   view - live state is re-read at the moment the tool is about to act on that field.
   An EC2 resize waits on stop/start waiters, so minutes can pass between one chain and
   the next, and a check done up front would be stale by then;
3. **only ``REVERTIBLE`` is touched.** A conflict, an unproven old value, or an unreadable
   resource is skipped and reported, never overwritten.

Reverts run newest change first, and the whole thing is idempotent without storing any
state: re-running re-reads live state, finds the field already at its pre-session value,
and reports it as already reverted. That is also how an asynchronous RDS change is polled
to completion.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .diff import Verdict, classify
from ..errors import LiveStateError, ResourceGone
from ..domain import Capability, iso
from ..handlers import MISMATCH, PENDING, VERIFIED, Verification, get_operation
from ..domain import Chain, Plan

UTC = timezone.utc


class Outcome(str, enum.Enum):
    #: ``--confirm`` was not passed; the calls are reported, not made
    DRY_RUN = "DRY_RUN"
    #: executed and a Describe/Get confirmed the field is back
    REVERTED = "REVERTED"
    #: AWS accepted the change but it has not settled yet (RDS Multi-AZ)
    SUBMITTED = "SUBMITTED"
    #: the field was already at its pre-session value before we did anything
    ALREADY_REVERTED = "ALREADY_REVERTED"
    #: a command is written out for a human; the tool will not issue it
    MANUAL = "MANUAL"
    #: deliberately not touched - conflict, unproven old value, unreadable, nothing to do
    SKIPPED = "SKIPPED"
    #: a call raised, or verification did not observe the expected value
    FAILED = "FAILED"


#: Outcomes that mean the operator still has work to do.
UNFINISHED = (Outcome.FAILED, Outcome.SKIPPED, Outcome.SUBMITTED, Outcome.MANUAL)


@dataclass
class ChainRevert:
    chain: Chain
    outcome: Outcome
    reason: str
    target_value: Optional[str] = None
    observed_before: Optional[str] = None
    observed_after: Optional[str] = None
    verification: Optional[str] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)
    planned_calls: List[Dict[str, Any]] = field(default_factory=list)
    precheck_verdict: Optional[Verdict] = None
    #: an aws-cli command for a human, when the tool will not act itself
    manual_command: Optional[str] = None
    #: True when the resource itself is gone. Not a to-do: nothing closes it.
    resource_gone: bool = False

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "chainId": self.chain.chain_id,
            "resourceType": self.chain.resource_type,
            "resourceId": self.chain.resource_id,
            "field": self.chain.field_name,
            "operation": self.chain.handler,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "targetValue": self.target_value,
            "observedBefore": self.observed_before,
            "observedAfter": self.observed_after,
        }
        if self.precheck_verdict is not None:
            body["precheckVerdict"] = self.precheck_verdict.value
        if self.verification is not None:
            body["verification"] = self.verification
        if self.calls:
            body["performedCalls"] = self.calls
        if self.planned_calls:
            body["plannedCalls"] = self.planned_calls
        if self.manual_command:
            body["manualCommand"] = self.manual_command
        return body


@dataclass
class RevertRun:
    plan: Plan
    results: List[ChainRevert]
    dry_run: bool
    started_at: datetime
    finished_at: datetime

    @property
    def summary(self) -> Dict[str, int]:
        counts = {outcome.value: 0 for outcome in Outcome}
        for result in self.results:
            counts[result.outcome.value] += 1
        return counts

    @property
    def unfinished(self) -> List[ChainRevert]:
        """Results an operator could still do something about.

        A SKIPPED chain is only unfinished if action remains possible. When the new value was
        never in ``requestParameters`` at all there is nothing anybody can do - not with
        ``--set``, not by hand, not ever - so counting it as work outstanding made a fully
        successful revert exit 3 under ``--exit-code``, telling a CI job that a clean run had
        failed. Those rows are :attr:`out_of_scope` instead.
        """
        return [
            r
            for r in self.results
            if r.outcome in UNFINISHED and r.chain.values_known and not r.resource_gone
        ]

    @property
    def out_of_scope(self) -> List[ChainRevert]:
        """Skipped because no value was ever recorded: reported, but not actionable."""
        return [
            r
            for r in self.results
            if r.outcome in UNFINISHED and (not r.chain.values_known or r.resource_gone)
        ]

    @property
    def failed(self) -> List[ChainRevert]:
        return [r for r in self.results if r.outcome is Outcome.FAILED]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planPath": self.plan.path,
            "planGeneratedAt": iso(self.plan.generated_at),
            "identity": self.plan.identity,
            "region": self.plan.region,
            "dryRun": self.dry_run,
            "startedAt": iso(self.started_at),
            "finishedAt": iso(self.finished_at),
            "summary": self.summary,
            # Counted separately because they mean different things to a wrapper script:
            # one is work outstanding, the other is a change with no value to restore.
            "unfinished": len(self.unfinished),
            "outOfScope": len(self.out_of_scope),
            "resourcesGone": sum(1 for r in self.results if r.resource_gone),
            "executionOrder": [r.chain.chain_id for r in self.results],
            "results": [r.to_dict() for r in self.results],
        }


def _verification_of(operation: Any, result: Any) -> Verification:
    """Reject a stale plugin loudly instead of mis-reporting it as a failed revert.

    ``verify_revert`` used to return a bare status string. A plugin still doing that would
    have its return value unpacked as a sequence of characters, raise, be caught by the
    broad handler around the call, and surface as "the revert could not be verified" - which
    blames AWS for the plugin author's out-of-date signature.
    """
    if isinstance(result, Verification):
        return result
    raise TypeError(
        "%s.verify_revert must return a Verification(status, observed), got %r. "
        "The observed value is part of the result so the verdict and the value shown "
        "beside it come from one read." % (type(operation).__name__, result)
    )


def revert_order(
    chains: Sequence[Chain], only: Optional[Sequence[str]] = None
) -> List[Chain]:
    """Newest change first, optionally narrowed to chosen chain ids.

    Ordered by each chain's *last* change, because that is the change being undone.
    Ties break on chain id so the order is reproducible.
    """
    selected = list(chains)
    if only:
        wanted = set(only)
        selected = [c for c in selected if c.chain_id in wanted]
    # A plan may omit the timestamps (hand-written, or written by an older build). Fall
    # back to chain id alone rather than crashing, and keep it deterministic.
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return sorted(
        selected,
        key=lambda c: (c.last_change_at or epoch, c.chain_id),
        reverse=True,
    )


class Reverter:
    def __init__(self, clients: Any, dry_run: bool = True, wait: bool = True) -> None:
        self.clients = clients
        self.dry_run = dry_run
        self.wait = wait

    def run(
        self,
        plan: Plan,
        only: Optional[Sequence[str]] = None,
        now: Optional[datetime] = None,
    ) -> RevertRun:
        started = now or datetime.now(tz=UTC)
        results = [self._revert_one(chain) for chain in revert_order(plan.chains, only)]
        return RevertRun(
            plan=plan,
            results=results,
            dry_run=self.dry_run,
            started_at=started,
            finished_at=now or datetime.now(tz=UTC),
        )

    # -- one chain ----------------------------------------------------------

    def _revert_one(self, chain: Chain) -> ChainRevert:
        operation = get_operation(chain.handler)
        if operation is None:
            return ChainRevert(
                chain=chain,
                outcome=Outcome.SKIPPED,
                reason="this build does not have handler %r" % chain.handler,
            )
        if chain.capability is not Capability.AUTO:
            # The gate that keeps generic handling honest: no plugin, no AWS call.
            manual = chain.revert.get("manual")
            reason = chain.revert.get("reason") or "no plugin covers this field"
            return ChainRevert(
                chain=chain,
                outcome=Outcome.MANUAL if manual else Outcome.SKIPPED,
                reason=reason,
                target_value=chain.net_before,
                manual_command=manual,
            )

        # A fresh read, right now, for this field. Never the plan's or the diff's view.
        live_value: Optional[str] = None
        read_error: Optional[str] = None
        gone = False
        try:
            live_value = operation.read_live_value(self.clients, chain.resource_id)
        except ResourceGone as exc:
            read_error, gone = str(exc), True
        except LiveStateError as exc:
            read_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad resource must not stop the run
            read_error = "%s: %s" % (type(exc).__name__, exc)

        precheck = classify(chain, live_value, read_error)
        target = chain.net_before

        if precheck.verdict is Verdict.ALREADY_REVERTED:
            # Either somebody beat us to it, or this is a re-run polling an asynchronous
            # change. Verification tells the two apart.
            return self._poll(chain, operation, live_value, precheck)

        if precheck.verdict is not Verdict.REVERTIBLE:
            return ChainRevert(
                chain=chain,
                outcome=Outcome.SKIPPED,
                reason=precheck.reason,
                target_value=target,
                observed_before=live_value,
                precheck_verdict=precheck.verdict,
                resource_gone=gone,
            )

        assert target is not None  # REVERTIBLE implies a proven anchor
        planned = [
            dict(s) for s in (chain.revert.get("steps") or [])
        ] or [{"api": "(plan file recorded no steps)", "params": {}}]

        if self.dry_run:
            return ChainRevert(
                chain=chain,
                outcome=Outcome.DRY_RUN,
                reason="would restore %r; nothing was called (pass --confirm to apply)"
                % target,
                target_value=target,
                observed_before=live_value,
                planned_calls=planned,
                precheck_verdict=precheck.verdict,
            )

        try:
            calls = operation.apply_revert(
                self.clients, chain.resource_id, target, wait=self.wait
            )
        except Exception as exc:  # noqa: BLE001 - report the AWS failure faithfully
            return ChainRevert(
                chain=chain,
                outcome=Outcome.FAILED,
                reason="the AWS call failed: %s: %s" % (type(exc).__name__, exc),
                target_value=target,
                observed_before=live_value,
                precheck_verdict=precheck.verdict,
            )

        return self._verify(chain, operation, target, live_value, calls, precheck)

    def _verify(
        self,
        chain: Chain,
        operation: Any,
        target: str,
        observed_before: Optional[str],
        calls: List[Dict[str, Any]],
        precheck,
    ) -> ChainRevert:
        try:
            # One read decides the verdict *and* supplies the value reported beside it.
            # Reading twice let a field that settles in a second - EC2 detailed monitoring -
            # be judged on the first read and displayed from the second, producing a row that
            # said FAILED next to the very value it claimed was missing.
            verification, observed_after = _verification_of(
                operation, operation.verify_revert(self.clients, chain.resource_id, target)
            )
        except Exception as exc:  # noqa: BLE001
            return ChainRevert(
                chain=chain,
                outcome=Outcome.FAILED,
                reason="the revert was issued but could not be verified: %s: %s"
                % (type(exc).__name__, exc),
                target_value=target,
                observed_before=observed_before,
                calls=calls,
                precheck_verdict=precheck.verdict,
            )

        if verification == MISMATCH and getattr(operation, "read_may_lag", False):
            # One retry, not a reclassification. A read that lagged the write resolves within
            # milliseconds; a write that silently did nothing - a mis-scoped IAM policy is the
            # usual cause - never does. So asking twice tells the two apart, and keeps
            # verification a real check rather than downgrading every miss to "still pending".
            try:
                verification, observed_after = _verification_of(
                    operation,
                    operation.verify_revert(self.clients, chain.resource_id, target),
                )
            except Exception:  # noqa: BLE001 - keep the first, honest answer
                pass

        if verification == VERIFIED:
            outcome, reason = Outcome.REVERTED, "restored %r and confirmed it" % target
        elif verification == PENDING:
            outcome, reason = (
                Outcome.SUBMITTED,
                "AWS accepted the change but it has not settled yet; re-run to poll it",
            )
        else:
            outcome, reason = (
                Outcome.FAILED,
                "the revert was issued but the field does not read %r" % target,
            )
        return ChainRevert(
            chain=chain,
            outcome=outcome,
            reason=reason,
            target_value=target,
            observed_before=observed_before,
            observed_after=observed_after,
            verification=verification,
            calls=calls,
            precheck_verdict=precheck.verdict,
        )

    def _poll(
        self, chain: Chain, operation: Any, live_value: Optional[str], precheck
    ) -> ChainRevert:
        """The field already reads as pre-session. Has it actually settled there?"""
        target = chain.net_before
        observed = live_value
        try:
            verification, observed = _verification_of(
                operation, operation.verify_revert(self.clients, chain.resource_id, target)
            )
        except Exception as exc:  # noqa: BLE001
            verification = MISMATCH
            note = " (verification failed: %s)" % exc
        else:
            note = ""

        if verification == PENDING:
            return ChainRevert(
                chain=chain,
                outcome=Outcome.SUBMITTED,
                reason="a revert is still being applied by AWS; re-run to poll it" + note,
                target_value=target,
                observed_before=live_value,
                observed_after=observed,
                verification=verification,
                precheck_verdict=precheck.verdict,
            )
        return ChainRevert(
            chain=chain,
            outcome=Outcome.ALREADY_REVERTED,
            reason=precheck.reason + note,
            target_value=target,
            observed_before=live_value,
            observed_after=observed,
            verification=verification,
            precheck_verdict=precheck.verdict,
        )
