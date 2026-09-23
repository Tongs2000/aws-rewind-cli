"""The pool of events available for reconstruction, and how it is assembled."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Sequence, Tuple

from ..domain import RETENTION_DAYS
from .event import CloudTrailEvent, sort_events
from .source import EventSource

@dataclass
class EventWindow:
    """Every event the tool managed to read, ordered by ``(eventTime, eventId)``.

    ``covered_from`` is the earliest instant we actually queried. It is what lets the
    tool distinguish "this field was never set" from "we could not look far enough
    back" - the difference between a sound inference and a guess.
    """

    events: List[CloudTrailEvent]
    covered_from: datetime
    covered_to: datetime
    truncated: bool = False

    def __post_init__(self) -> None:
        self.events = sort_events(self.events)

    def before(self, boundary: Tuple[datetime, str]) -> Iterable[CloudTrailEvent]:
        """Events strictly older than ``boundary``, newest first.

        ``boundary`` must be a ``(eventTime, eventId)`` pair - the same shape as
        :attr:`CloudTrailEvent.sort_key`. A longer tuple would compare as *greater*
        than an equal-prefixed event, so an event would be returned as older than
        itself.
        """
        if len(boundary) != 2:
            raise ValueError("boundary must be an (eventTime, eventId) pair")
        for event in reversed(self.events):
            if event.sort_key < boundary:
                yield event

    def successful_before(
        self, boundary: Tuple[datetime, str], event_names: Sequence[str]
    ) -> Iterable[CloudTrailEvent]:
        wanted = set(event_names)
        for event in self.before(boundary):
            if event.event_name in wanted and event.successful:
                yield event

    @property
    def complete(self) -> bool:
        """False when a lookup was cut short, which invalidates absence-based logic."""
        return not self.truncated


def build_window(
    source: EventSource,
    session_events: List[CloudTrailEvent],
    session_start: datetime,
    session_end: datetime,
    event_names: Sequence[str],
    lookback_days: int,
) -> EventWindow:
    """Union of the session window and the per-EventName lookback, deduped by eventId.

    The lookback is *not* filtered by identity: whoever last set a field established the
    value we need to restore, and that was very often somebody else.
    """
    lookback_days = min(lookback_days, RETENTION_DAYS)
    lookback_start = session_start - timedelta(days=lookback_days)
    history, truncated = source.named_events(lookback_start, session_end, event_names)
    merged: Dict[str, CloudTrailEvent] = {}
    for event in list(history) + list(session_events):
        if event.event_id:
            merged[event.event_id] = event
    return EventWindow(
        events=list(merged.values()),
        covered_from=lookback_start,
        covered_to=session_end,
        truncated=truncated,
    )
