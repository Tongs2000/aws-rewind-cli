"""`rewind diff` output: live state versus the plan, most actionable information first."""

from __future__ import annotations

from typing import List

from ..pipeline.diff import BLOCKING, PlanDiff, Verdict
from .table import cell, table, truncate


def render_diff(diff: PlanDiff) -> str:
    """Live state versus the plan, most actionable information first."""
    summary = diff.summary
    out: List[str] = [
        "plan       : %s" % (diff.plan.path or "<stdin>"),
        "generated  : %s  by identity %s in %s"
        % (diff.plan.generated_at.isoformat(), diff.plan.identity, diff.plan.region),
        "checked    : %s" % diff.checked_at.isoformat(),
        "fields     : %d" % len(diff.entries),
    ]
    ordered = [v for v in Verdict if summary[v.value]]
    out.append(
        "verdicts   : %s" % "  ".join("%s=%d" % (v.value, summary[v.value]) for v in ordered)
    )
    if diff.conflicts:
        out.append(
            "CONFLICT   : %d field(s) were changed outside this plan and must not be "
            "overwritten" % len(diff.conflicts)
        )
    elif diff.compared:
        out.append(
            "drift      : none - %d of %d field(s) compared, all still as the session left them"
            % (len(diff.compared), len(diff.entries))
        )
    elif diff.entries:
        # Saying "no drift" here was a lie: no CONFLICT was found because nothing could be
        # read at all. Seen for real with expired credentials - every live read failed and
        # the table still reported that everything matched.
        out.append(
            "drift      : UNKNOWN - not one field could be read, so nothing was compared"
        )

    expired = diff.credential_failures
    if expired:
        out.append(
            "CREDENTIALS: %d read(s) failed on your credentials, not on the resource. "
            "Refresh them and re-run; this diff proves nothing about drift."
            % len(expired)
        )

    if diff.entries:
        rows = [
            [
                truncate(e.chain.resource_id, 28),
                e.chain.field_name,
                cell(e.chain.net_before),
                # A DISCOVERED chain has no "after" either: a call whose new value is not
                # in requestParameters at all, such as StartInstances, is still listed.
                cell(e.chain.net_after),
                cell(e.live_value),
                e.verdict.value,
            ]
            for e in diff.entries
        ]
        out += [
            "",
            table(rows, ["RESOURCE", "FIELD", "WAS", "SESSION SET", "LIVE NOW", "VERDICT"]),
        ]

    notable = [e for e in diff.entries if e.verdict in BLOCKING or e.blame]
    if notable:
        out += ["", "Details", "======="]
        for entry in notable:
            out.append("")
            out.append(
                "%s  %s.%s  [%s]"
                % (
                    entry.chain.chain_id,
                    entry.chain.resource_id,
                    entry.chain.field_name,
                    entry.verdict.value,
                )
            )
            out.append("  %s" % entry.reason)
            for key, value in sorted(entry.detail.items()):
                out.append("  live %s: %s" % (key, value))
            for record in entry.blame:
                out.append(
                    "  changed by %s at %s via %s -> %s  [%s]"
                    % (
                        record["identity"],
                        record["eventTime"],
                        record["eventName"],
                        record["setTo"],
                        record["eventId"],
                    )
                )
            if entry.verdict is Verdict.CONFLICT and not entry.blame:
                out.append(
                    "  no CloudTrail event since the plan explains this change; it may "
                    "predate the plan, be outside event history, or not be recorded"
                    if diff.blame_attempted
                    else "  run with --blame to look for the CloudTrail event that changed it"
                )

    actionable = diff.actionable
    if actionable:
        out += [
            "",
            "%d field(s) are ready to revert. Next: `rewind revert %s` for a dry run, "
            "then add --confirm." % (len(actionable), diff.plan.path or "plan.json"),
        ]
    elif diff.entries:
        out += ["", "Nothing is ready to revert automatically."]
    return "\n".join(out)
