"""`rewind snapshot` output: what was recorded, and what could not be."""

from __future__ import annotations

from typing import List

from .table import cell, table, truncate


def render_snapshot(snapshot) -> str:
    """What was recorded, and what could not be."""
    out: List[str] = [
        "taken   : %s" % snapshot.taken_at.isoformat(),
        "region  : %s" % snapshot.region,
        "fields  : %d recorded, %d unreadable"
        % (len(snapshot.readable_entries), len(snapshot.failed_entries)),
    ]
    if snapshot.entries:
        rows = [
            [
                truncate(e.resource_id, 28),
                e.field_name,
                cell(e.value),
                e.error or "",
            ]
            for e in snapshot.entries
        ]
        out += ["", table(rows, ["RESOURCE", "FIELD", "VALUE", "ERROR"])]
    out += [
        "",
        "Pass this file to `rewind plan --snapshot <file>`. It is used only where "
        "CloudTrail cannot prove the previous value, and only when it predates the change.",
    ]
    return "\n".join(out)
