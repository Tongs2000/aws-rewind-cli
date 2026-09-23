"""Reading CloudTrail. Depends only on :mod:`rewind.domain`."""

from .event import (
    CloudTrailEvent,
    from_lookup_record,
    identity_matches,
    sort_events,
)
from .inspect import collect, dig, flatten, instance_ids, items_of
from .source import CloudTrailEventSource, EventSource, StaticEventSource
from .window import EventWindow, build_window

__all__ = [
    "CloudTrailEvent",
    "CloudTrailEventSource",
    "EventSource",
    "EventWindow",
    "StaticEventSource",
    "build_window",
    "collect",
    "dig",
    "flatten",
    "from_lookup_record",
    "identity_matches",
    "instance_ids",
    "items_of",
    "sort_events",
]
