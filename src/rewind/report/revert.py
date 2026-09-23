"""`rewind revert` output: what was done, or what would be done."""

from __future__ import annotations

from typing import List

from ..pipeline.revert import Outcome, RevertRun
from .table import cell, table, truncate


def render_revert(run: RevertRun) -> str:
    """What was done, or what would be done. Ordered newest change first."""
    summary = run.summary
    mode = "DRY RUN - nothing was called" if run.dry_run else "APPLIED"
    out: List[str] = [
        "plan       : %s" % (run.plan.path or "<stdin>"),
        "mode       : %s" % mode,
        "region     : %s" % run.plan.region,
        "started    : %s" % run.started_at.isoformat(),
        "fields     : %d  (newest change reverted first)" % len(run.results),
    ]
    present = [o for o in Outcome if summary[o.value]]
    out.append(
        "outcomes   : %s" % "  ".join("%s=%d" % (o.value, summary[o.value]) for o in present)
    )

    if run.results:
        rows = [
            [
                truncate(r.chain.resource_id, 28),
                r.chain.field_name,
                cell(r.observed_before),
                cell(r.target_value),
                cell(r.observed_after),
                r.outcome.value,
            ]
            for r in run.results
        ]
        out += [
            "",
            table(rows, ["RESOURCE", "FIELD", "WAS", "TARGET", "NOW", "OUTCOME"]),
        ]
    else:
        out += ["", "Nothing selected."]

    if run.results:
        out += ["", "Details", "======="]
        for result in run.results:
            out.append("")
            out.append(
                "%s  %s.%s  [%s]"
                % (
                    result.chain.chain_id,
                    result.chain.resource_id,
                    result.chain.field_name,
                    result.outcome.value,
                )
            )
            out.append("  %s" % result.reason)
            for call in result.planned_calls:
                extra = "  # %s" % call["condition"] if call.get("condition") else ""
                out.append("  would call %s%s" % (call["api"], extra))
            for call in result.calls:
                out.append("  called     %s" % call["api"])
            if result.manual_command:
                out.append("  run by hand: %s" % result.manual_command)
            warning = result.chain.revert.get("warning")
            if warning and result.outcome in (Outcome.DRY_RUN, Outcome.REVERTED):
                out.append("  warning    %s" % warning)

    if run.dry_run and any(r.outcome is Outcome.DRY_RUN for r in run.results):
        out += [
            "",
            "Re-run with --confirm to apply. Live state is re-checked immediately before "
            "each field is touched, so a conflict that appears in the meantime still "
            "stops that field.",
        ]
    unfinished = run.unfinished
    if not run.dry_run and unfinished:
        out += ["", "%d field(s) still need attention:" % len(unfinished)]
        for result in unfinished:
            out.append(
                "  %-28s %-22s %s"
                % (result.chain.resource_id, result.chain.field_name, result.outcome.value)
            )
    out_of_scope = run.out_of_scope
    if not run.dry_run and out_of_scope:
        # Listed, never as "attention": CloudTrail did not record a value for these, so no
        # amount of operator effort changes the outcome. Saying otherwise is a false to-do.
        out += [
            "",
            "%d change(s) reported but not revertible - CloudTrail records no value for "
            "them, so there is nothing to restore:" % len(out_of_scope),
        ]
        for result in out_of_scope:
            out.append(
                "  %-28s %s"
                % (result.chain.resource_id, result.chain.field_name)
            )
    return "\n".join(out)
