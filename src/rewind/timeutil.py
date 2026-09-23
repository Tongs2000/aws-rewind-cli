"""Parsing the time window an operator asked for.

Pure: no AWS, no CloudTrail. It needs to know the retention limit to warn about a window
that reaches past it, and takes that from :mod:`rewind.domain` rather than importing the
CloudTrail layer for one integer.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from .domain import RETENTION_DAYS, parse_time

UTC = timezone.utc

_DURATION = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


class WindowError(ValueError):
    pass


def parse_duration(text: str) -> timedelta:
    """``90m``, ``2h``, ``3d``, ``1w``."""
    match = _DURATION.match((text or "").strip().lower())
    if not match:
        raise WindowError(
            "cannot parse duration %r; use a number followed by s, m, h, d or w "
            "(for example 90m or 2h)" % text
        )
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise WindowError("duration must be positive")
    return timedelta(**{_UNITS[unit]: amount})


def resolve_window(
    since: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[datetime, datetime]:
    """Resolve ``--since`` or ``--start/--end`` into an explicit UTC window."""
    now = now or datetime.now(tz=UTC)

    if since and (start or end):
        raise WindowError("use either --since or --start/--end, not both")
    if since:
        return now - parse_duration(since), now
    if start and end:
        start_dt, end_dt = parse_time(start), parse_time(end)
    elif start:
        start_dt, end_dt = parse_time(start), now
    else:
        raise WindowError("a window is required: pass --since, or --start and --end")

    if end_dt <= start_dt:
        raise WindowError("--end must be after --start")
    return start_dt, end_dt


def check_retention(start: datetime, now: Optional[datetime] = None) -> Optional[str]:
    """Warn when the window itself reaches past what CloudTrail can answer."""
    now = now or datetime.now(tz=UTC)
    if start < now - timedelta(days=RETENTION_DAYS):
        return (
            "The window starts more than %d days ago, which is past CloudTrail event "
            "history retention. Older events cannot be read." % RETENTION_DAYS
        )
    return None
