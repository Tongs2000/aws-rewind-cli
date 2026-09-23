"""`rewind operations` and `rewind resolvers`: what the tool can do, and on what evidence.

Both are introspection commands - they take no session and touch nothing. They exist so the
answer to "will this tool handle my situation?" does not need a real session to find out.
"""

from __future__ import annotations

from typing import List

from .table import table, truncate


def render_resolvers(described: List[dict]) -> str:
    rows = [
        [
            d["name"],
            "yes" if d["available"] else "no",
            truncate(d["description"], 62),
        ]
        for d in described
    ]
    out = [
        "Anchor resolvers, in priority order. The first one to *prove* a value wins.",
        "",
        table(rows, ["RESOLVER", "ACTIVE", "EVIDENCE"]),
    ]
    unavailable = [d for d in described if not d["available"]]
    if unavailable:
        out.append("")
        for entry in unavailable:
            out.append("%s: %s" % (entry["name"], entry["unavailableReason"]))
    return "\n".join(out)


def render_operations(coverage: List[dict]) -> str:
    """Which fields a plugin covers, and what happens to everything else."""
    rows = [
        [
            truncate(c["resourceType"], 26),
            c["field"],
            truncate(", ".join(c["events"]), 44),
            "yes" if c["asynchronous"] else "",
            "yes" if c["configRecorded"] else "",
        ]
        for c in coverage
    ]
    return "\n".join(
        [
            "Plugins raise one field to AUTO: live-state reads, conflict checks and "
            "execution.",
            "",
            table(rows, ["RESOURCE TYPE", "FIELD", "EVENTS", "ASYNC", "IN CONFIG"]),
            "",
            "Everything else CloudTrail records as a change is still handled, generically:",
            "  DISCOVERED     the change is listed, but its new value is not in the "
            "request parameters",
            "  RECONSTRUCTED  before and after are known; no safe inverse call exists "
            "for that API",
            "  MANUAL         an `aws` command is written out for you to check and run",
            "",
            "So a session is never assumed to contain only the fields above. Writing a "
            "plugin moves",
            "one field from MANUAL to AUTO; it is not what makes the change visible.",
        ]
    )
