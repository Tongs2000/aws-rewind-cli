"""`rewind plan` output, including the per-chain evidence shown by ``--explain``."""

from __future__ import annotations

from typing import List

from ..domain import Capability, Chain, Confidence, Plan
from .table import cell, table, truncate


def render_plan(plan: Plan, explain: bool = False) -> str:
    stats = plan.stats
    query = plan.query
    out: List[str] = [
        "identity   : %s" % query.identity,
        "window     : %s -> %s" % (query.start_time.isoformat(), query.end_time.isoformat()),
        "region     : %s" % query.region,
        "changes    : %d change(s) across %d field(s)" % (stats["changes"], stats["chains"]),
        "revertible : %d of %d automatically  (%d by hand, %d unprovable, %d already "
        "back to original)"
        % (
            stats["revertible"],
            stats["chains"],
            stats.get("manual", 0),
            stats["unprovable"],
            stats["netNoOp"],
        ),
        "capability : %s"
        % (
            " ".join(
                "%s=%d" % (c.value, stats.get("byCapability", {}).get(c.value, 0))
                for c in Capability
                if stats.get("byCapability", {}).get(c.value)
            )
            or "none"
        ),
        "confidence : %s"
        % (
            " ".join(
                "%s=%d" % (c.value, stats["byConfidence"][c.value])
                for c in Confidence
                if stats["byConfidence"].get(c.value)
            )
            or "none"
        ),
    ]
    for warning in plan.warnings:
        out.append("warning    : %s" % warning)

    if plan.chains:
        rows = [
            [
                truncate(c.resource_id, 24),
                truncate(c.field_name, 26),
                "%s -> %s" % (cell(c.net_before), cell(c.net_after)),
                str(len(c.changes)),
                c.confidence.value,
                c.capability.value,
                truncate(c.anchor.source, 18),
            ]
            for c in plan.chains
        ]
        out += [
            "",
            table(
                rows,
                ["RESOURCE", "FIELD", "BEFORE -> NOW", "STEPS", "CONFIDENCE",
                 "CAPABILITY", "ANCHOR"],
            ),
            "",
            "AUTO = `rewind revert` can do it.  MANUAL = a command is written out for you.",
            "RECONSTRUCTED = values known, no safe inverse.  DISCOVERED = change seen only.",
        ]
    else:
        out += ["", "Nothing to plan: no tracked field changes by this identity."]

    if explain and plan.chains:
        out.append("")
        out.append("Evidence")
        out.append("========")
        for chain in plan.chains:
            out.append("")
            out += _explain_chain(chain)

    if plan.chains:
        out += ["", "Next: `rewind diff <plan.json>` to compare against live state."]
    return "\n".join(out)


def _explain_chain(chain: Chain) -> List[str]:
    lines = [
        "%s  %s.%s" % (chain.chain_id, chain.resource_id, chain.field_name),
        "  anchor     : %s  (%s via %s)"
        % (cell(chain.net_before), chain.confidence.value, chain.anchor.source),
    ]
    if chain.anchor.note:
        lines.append("  reason     : %s" % chain.anchor.note)
    if chain.anchor.evidence_event_ids:
        lines.append("  evidence   : %s" % ", ".join(chain.anchor.evidence_event_ids))
    for change in chain.changes:
        lines.append(
            "  step %d     : %s  %s: %s -> %s  [%s]"
            % (
                change.sequence,
                change.event_time.strftime("%H:%M:%S"),
                change.event_name,
                cell(change.before),
                # Also cell(): a DISCOVERED step has no "after" either - a call whose new
                # value is not in requestParameters at all. It rendered as "None".
                cell(change.after),
                change.event_id,
            )
        )
    lines.append(
        "  handled by : %s  (%s)"
        % (chain.handler, chain.event_name or chain.event_source or "unknown event")
    )
    if chain.revert.get("executable"):
        calls = ", ".join(s["api"] for s in chain.revert.get("steps", []))
        lines.append("  revert to  : %s  via %s" % (chain.revert["targetValue"], calls))
    elif chain.revert.get("manual"):
        lines.append("  run by hand: %s" % chain.revert["manual"])
        for caveat in chain.revert.get("caveats", []):
            lines.append("  caveat     : %s" % caveat)
    else:
        lines.append("  revert     : not automatic - %s" % chain.revert.get("reason"))
    for note in chain.notes:
        lines.append("  note       : %s" % note)
    return lines
