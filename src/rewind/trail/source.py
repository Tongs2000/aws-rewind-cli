"""Where events come from.

Two query shapes, chosen to avoid a dependency the tool cannot rely on:

* the **session window** is read with no ``LookupAttributes`` at all and the identity is
  filtered in code. Windows are short, so reading everything is cheap, and it cannot miss
  a change just because CloudTrail did not index the resource;
* the **lookback** uses one ``EventName`` attribute per API. ``LookupEvents`` accepts a
  single attribute per call, and ``EventName`` is an index that does not depend on
  resource tagging.

The ``ResourceName`` index is never used - a test asserts it.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .event import CloudTrailEvent, from_lookup_record, sort_events

class EventSource(abc.ABC):
    @abc.abstractmethod
    def window_events(
        self, start: datetime, end: datetime
    ) -> Tuple[List[CloudTrailEvent], bool]:
        """Every event in the window. Returns ``(events, truncated)``."""

    @abc.abstractmethod
    def named_events(
        self, start: datetime, end: datetime, event_names: Sequence[str]
    ) -> Tuple[List[CloudTrailEvent], bool]:
        """Events of the given names over the lookback. Returns ``(events, truncated)``."""


class CloudTrailEventSource(EventSource):
    def __init__(self, client: Any, max_pages: int = 40, page_size: int = 50) -> None:
        self.client = client
        self.max_pages = max_pages
        self.page_size = page_size
        self.api_calls = 0

    def _paginate(
        self,
        start: datetime,
        end: datetime,
        lookup_attributes: Optional[List[Dict[str, str]]] = None,
    ) -> Tuple[List[CloudTrailEvent], bool]:
        params: Dict[str, Any] = {
            "StartTime": start,
            "EndTime": end,
            "MaxResults": self.page_size,
        }
        if lookup_attributes:
            params["LookupAttributes"] = lookup_attributes
        collected: List[CloudTrailEvent] = []
        token: Optional[str] = None
        pages = 0
        while True:
            call = dict(params)
            if token:
                call["NextToken"] = token
            response = self.client.lookup_events(**call)
            self.api_calls += 1
            for record in response.get("Events", []):
                collected.append(from_lookup_record(record))
            pages += 1
            token = response.get("NextToken")
            if not token:
                return collected, False
            if pages >= self.max_pages:
                return collected, True

    def window_events(
        self, start: datetime, end: datetime
    ) -> Tuple[List[CloudTrailEvent], bool]:
        return self._paginate(start, end)

    def named_events(
        self, start: datetime, end: datetime, event_names: Sequence[str]
    ) -> Tuple[List[CloudTrailEvent], bool]:
        collected: List[CloudTrailEvent] = []
        truncated = False
        for name in sorted(set(event_names)):
            events, page_truncated = self._paginate(
                start, end, [{"AttributeKey": "EventName", "AttributeValue": name}]
            )
            collected.extend(events)
            truncated = truncated or page_truncated
        return collected, truncated


class StaticEventSource(EventSource):
    """Fixture-backed source for tests and offline demos."""

    def __init__(self, events: Iterable[CloudTrailEvent], truncated: bool = False) -> None:
        self.events = sort_events(events)
        self.truncated = truncated

    def window_events(
        self, start: datetime, end: datetime
    ) -> Tuple[List[CloudTrailEvent], bool]:
        return ([e for e in self.events if start <= e.event_time <= end], self.truncated)

    def named_events(
        self, start: datetime, end: datetime, event_names: Sequence[str]
    ) -> Tuple[List[CloudTrailEvent], bool]:
        wanted = set(event_names)
        return (
            [e for e in self.events if e.event_name in wanted and start <= e.event_time <= end],
            self.truncated,
        )
