"""`rewind scan` output: what changed, and who changed it.

Three shapes, because ``--identity`` is optional here.

* **scoped** - the identity is in the header and every row belongs to it.
* **unscoped** - one row per identity, counted. A real account produces hundreds of SSM
  agent heartbeats for every handful of changes a human made, so listing every change
  cannot answer "who should I ask about" - which is the only reason to scan unscoped.
* **unscoped with ``--detail``** - every change, with the identity as a column.
"""

from __future__ import annotations

import re

from typing import List, Optional

from ..pipeline.scan import ScanResult
from .table import elide, table, truncate

#: How many identities to name in the table before summarising the rest. The count of what
#: is not shown is always printed: a reader must never have to wonder whether the list ended
#: because it ran out or because the renderer stopped.
IDENTITY_LIMIT = 8


def _identity_list(labels: List[str], limit: int = IDENTITY_LIMIT) -> str:
    if len(labels) <= limit:
        return ", ".join(labels)
    hidden = len(labels) - limit
    return "%s, +%d more (use --output json for the full list)" % (
        ", ".join(labels[:limit]),
        hidden,
    )


#: The part of an assumed-role ARN that carries no information: every row in one account
#: shares it. Stripped so the width goes to the role and session names instead.
_ARN_PREFIX = re.compile(r"^arn:[a-z0-9-]*:sts::\d+:assumed-role/")


def _caller(label: Optional[str], width: int) -> str:
    """``...:assumed-role/Role/session`` -> ``Role/session``, elided from the middle."""
    if not label:
        return "(unknown)"
    return elide(_ARN_PREFIX.sub("", label), width)


def _by_identity_table(result: ScanResult) -> str:
    rows = [
        [
            _caller(row.identity, 46),
            str(row.changes),
            str(row.resources),
            str(row.plugin_backed) if row.plugin_backed else "-",
            truncate(", ".join(sorted(row.event_names)), 46),
        ]
        for row in result.by_identity
    ]
    return table(
        rows, ["IDENTITY", "CHANGES", "RESOURCES", "PLUGIN-BACKED", "EVENTS"]
    )


def render_scan(result: ScanResult, detail: bool = False) -> str:
    query = result.query
    scoped = query.filtered

    out: List[str] = [
        "identity : %s" % (query.identity if scoped else "(all - no --identity given)"),
        "window   : %s -> %s" % (query.start_time.isoformat(), query.end_time.isoformat()),
        "region   : %s" % query.region,
    ]
    if scoped:
        out.append(
            "events   : %d in window, %d by this identity"
            % (len(result.window_events), len(result.owned_events))
        )
    else:
        out.append("events   : %d in window (all in scope)" % len(result.window_events))
    out.append("changes  : %d tracked field change(s)" % len(result.mutations))
    if not scoped and result.identities:
        out.append("identities: %d made a tracked change" % len(result.identities))
    if result.truncated:
        out.append("warning  : the CloudTrail lookup was truncated; results may be partial")

    if result.mutations and not scoped and not detail:
        out += ["", _by_identity_table(result)]
        out += [
            "",
            "One row per identity, most actionable first: PLUGIN-BACKED counts the changes "
            "`rewind revert` could execute.",
            "Next: `rewind plan --identity <one of the above>`, or re-run `scan --detail` "
            "for every change.",
        ]
    elif result.mutations:
        headers = ["TIME", "EVENT", "RESOURCE", "FIELD", "SET TO", "HANDLED BY"]
        rows = []
        for m in result.mutations:
            row = [
                m.event_time.strftime("%Y-%m-%d %H:%M:%S"),
                m.event_name,
                truncate(m.resource_id, 24),
                truncate(m.field_name, 26),
                "-> %s" % m.after if m.value_known else "(not in parameters)",
                "plugin" if m.handler != "generic" else "generic",
            ]
            if not scoped:
                row.insert(2, _caller(m.identity, 30))
            rows.append(row)
        if not scoped:
            headers.insert(2, "IDENTITY")

        out += ["", table(rows, headers)]
        out += [
            "",
            "Only the value each call *set* is shown; run `rewind plan` to resolve what "
            "each field held beforehand.",
        ]
        if not scoped:
            out.append(
                "`rewind plan` needs --identity: pick one from the IDENTITY column above."
            )
    elif scoped:
        out.append("")
        out.append("No tracked field changes by this identity in this window.")
        others = result.other_identities
        if others:
            out.append(
                "Other identities that changed things here (%d): %s"
                % (len(others), _identity_list(others))
            )
            out.append("Re-run without --identity to see what they changed.")
    else:
        out.append("")
        out.append("No tracked field changes by anyone in this window.")
    return "\n".join(out)
