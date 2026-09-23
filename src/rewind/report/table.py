"""The two rules every table in this package follows.

* Never show a value the tool cannot prove. An unproven anchor renders as ``?``, never as
  a plausible-looking guess - an operator must be able to tell "we established this" from
  "this is roughly what it probably was".
* Always show how many changes a chain contains, because "reverted to the value before the
  session" is a different promise from "undid the last change".

Everything printed here is also available as JSON (``--output json``), so the CLI stays
scriptable and no report is the only way to reach a fact.
"""

from __future__ import annotations

from typing import List, Optional

#: Rendered in place of a value the tool could not prove. Never a guess.
UNPROVEN = "?"


def cell(value: Optional[str]) -> str:
    return UNPROVEN if value is None else value


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def elide(text: str, width: int) -> str:
    """Shorten from the middle, keeping both ends.

    For an ARN the distinguishing part is the *tail*:
    ``...assumed-role/CloudAWSSystemsManagerDefaultEC2InstanceManagementRole/i-0abc111122``.
    Plain :func:`truncate` cut that off, so six different callers rendered as six identical
    rows - a table that hides exactly what it was printed to distinguish.
    """
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"
    keep = width - 1
    head = keep // 2
    return text[:head] + "…" + text[len(text) - (keep - head) :]


def table(rows: List[List[str]], headers: List[str]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for number, row in enumerate(rows):
        for index, value in enumerate(row):
            if not isinstance(value, str):
                # Reached here once, for real, from a DISCOVERED chain whose net_after was
                # None: a bare "no len()" TypeError that named neither the column nor the
                # caller. Every value belongs in cell() precisely so None becomes "?".
                raise TypeError(
                    "table column %r (%s) got a non-string in row %d: %r - wrap it in cell()"
                    % (headers[index], index, number, value)
                )
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip())
    return "\n".join(lines)
