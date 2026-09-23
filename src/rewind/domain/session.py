"""What the operator asked about, and the documents the tool produces.

``Plan`` and ``Snapshot`` are domain types, not file formats. Serialising them is
:mod:`rewind.store`'s job - which is what lets ``store`` depend on nothing but this
package, and lets a resolver take a ``Snapshot`` without importing a file reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .chain import Chain
from .constants import PLAN_FORMAT_VERSION
from .values import iso

@dataclass
class Query:
    """What the operator asked about.

    An empty ``identity`` means "do not filter" - every change in the window is in scope.
    ``scan`` allows it, because working out *who* is the question an operator usually has
    before they have an answer to it. ``plan`` does not: an unfiltered plan would collect
    changes made by AWS service-linked roles and offer to revert them.
    """

    identity: str
    start_time: datetime
    end_time: datetime
    region: str

    @property
    def filtered(self) -> bool:
        return bool(self.identity.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identity": self.identity or None,
            "startTime": iso(self.start_time),
            "endTime": iso(self.end_time),
            "region": self.region,
        }


@dataclass
class Plan:
    """The plan document. This file *is* the tool's state - there is no database."""

    query: Query
    generated_at: datetime
    chains: List[Chain] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    tool_version: str = "0.1.0"
    #: where this plan was read from, when it came off disk
    path: Optional[str] = None

    @property
    def identity(self) -> str:
        """The query's fields, reached through the aggregate that owns them."""
        return self.query.identity

    @property
    def region(self) -> str:
        return self.query.region

    @property
    def start_time(self) -> datetime:
        return self.query.start_time

    @property
    def end_time(self) -> datetime:
        return self.query.end_time

    def chain(self, needle: str) -> Optional[Chain]:
        for candidate in self.chains:
            if candidate.chain_id == needle:
                return candidate
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rewindPlanVersion": PLAN_FORMAT_VERSION,
            "tool": {"name": "rewind", "version": self.tool_version},
            "generatedAt": iso(self.generated_at),
            "query": self.query.to_dict(),
            "stats": dict(self.stats),
            "warnings": list(self.warnings),
            "chains": [c.to_dict() for c in self.chains],
        }


@dataclass(frozen=True)
class SnapshotEntry:
    """One field's value as observed at a known moment."""

    resource_type: str
    resource_id: str
    field_name: str
    operation: str
    value: Optional[str]
    error: Optional[str] = None

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.resource_type, self.resource_id, self.field_name)


@dataclass
class Snapshot:
    """Field values recorded before a change, for use as anchor evidence."""

    taken_at: datetime
    region: str
    entries: List[SnapshotEntry] = field(default_factory=list)
    path: Optional[str] = None

    def lookup(
        self, resource_type: str, resource_id: str, field_name: str
    ) -> Optional[SnapshotEntry]:
        for entry in self.entries:
            if entry.key == (resource_type, resource_id, field_name) and entry.value is not None:
                return entry
        return None

    @property
    def readable_entries(self) -> List[SnapshotEntry]:
        return [e for e in self.entries if e.value is not None]

    @property
    def failed_entries(self) -> List[SnapshotEntry]:
        """Recorded with their error rather than dropped, so the file says what was tried."""
        return [e for e in self.entries if e.value is None]
