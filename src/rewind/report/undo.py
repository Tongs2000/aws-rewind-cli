"""`rewind undo` output: the three stages, compressed to what a decision needs.

Printing `plan`, `diff` and `revert` in full would be several hundred lines and bury the one
question the operator has to answer. So each stage contributes a summary line, and the table
is the *revert's* view - the last word - with the diff's verdict beside it, because "ready"
and "somebody else touched this" are different reasons to leave a field alone.

``--detail`` prints each stage's own report in full instead, for when the summary is not
enough or the output is going into an incident record.
"""

from __future__ import annotations

from typing import List

from ..pipeline.diff import Verdict
from ..pipeline.revert import Outcome
from ..pipeline.undo import UndoRun
from .diff import render_diff
from .plan import render_plan
from .revert import render_revert
from .table import cell, table, truncate


def render_undo(run: UndoRun, detail: bool = False) -> str:
    if detail:
        return "\n\n".join(
            [
                "=" * 78,
                "PLAN",
                "=" * 78,
                render_plan(run.plan, explain=True),
                "=" * 78,
                "DIFF",
                "=" * 78,
                render_diff(run.diff),
                "=" * 78,
                "REVERT",
                "=" * 78,
                render_revert(run.revert),
            ]
        )

    plan, diff, revert = run.plan, run.diff, run.revert
    stats = plan.stats
    verdicts = {e.chain.chain_id: e.verdict for e in diff.entries}

    out: List[str] = [
        "identity   : %s" % plan.identity,
        "window     : %s -> %s"
        % (plan.start_time.isoformat(), plan.end_time.isoformat()),
        "region     : %s" % plan.region,
        "mode       : %s"
        % (
            "DRY RUN - nothing was called"
            if run.dry_run
            else "APPLIED - AWS was called"
        ),
        "",
        "1 plan     : %d change(s) across %d field(s); %d can be reverted automatically"
        % (stats["changes"], stats["chains"], stats["revertible"]),
        "2 diff     : %s"
        % (
            "%d field(s) conflict - changed outside this plan" % len(diff.conflicts)
            if diff.conflicts
            else "no drift in the %d field(s) that could be compared" % len(diff.compared)
        ),
        "3 revert   : %s"
        % "  ".join(
            "%s=%d" % (o.value, revert.summary[o.value])
            for o in Outcome
            if revert.summary[o.value]
        ),
    ]
    for warning in run.warnings:
        out.append("warning    : %s" % warning)
    if run.plan_path:
        out.append("plan       : %s" % run.plan_path)

    acted = [
        r
        for r in revert.results
        if r.outcome in (Outcome.DRY_RUN, Outcome.REVERTED, Outcome.SUBMITTED)
    ]
    if acted:
        rows = [
            [
                truncate(r.chain.resource_id, 26),
                truncate(r.chain.field_name, 22),
                cell(r.observed_before),
                cell(r.target_value),
                cell(r.observed_after),
                (verdicts.get(r.chain.chain_id) or Verdict.UNCHECKABLE).value,
                r.outcome.value,
            ]
            for r in acted
        ]
        out += [
            "",
            table(
                rows,
                ["RESOURCE", "FIELD", "WAS", "TARGET", "NOW", "DIFF SAID", "OUTCOME"],
            ),
        ]

    # Anything an operator still has to decide about, and nothing they cannot act on.
    blocked = [r for r in revert.unfinished if r not in acted]
    if blocked:
        out += ["", "%d field(s) still need a decision:" % len(blocked)]
        for result in blocked:
            out.append(
                "  %-26s %-22s %s" % (
                    truncate(result.chain.resource_id, 26),
                    truncate(result.chain.field_name, 22),
                    result.reason,
                )
            )

    skipped = len(revert.out_of_scope)
    if skipped:
        out += [
            "",
            "%d further change(s) were reported but are not revertible at all; "
            "`rewind revert --help` explains why." % skipped,
        ]

    if not acted:
        out += ["", "Nothing to revert automatically in this window."]
    elif run.dry_run:
        out += [
            "",
            "Nothing was called. Re-run with --confirm to apply; live state is re-read "
            "immediately before each field is touched.",
        ]
    return "\n".join(out)
