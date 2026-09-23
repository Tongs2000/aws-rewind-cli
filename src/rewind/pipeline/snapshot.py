"""Taking a snapshot: reading live state for the resources an operator named.

An action, not a format - which is why it is here and not in :mod:`rewind.store`. It asks
every handler that covers the resource type for its field, so one ``--instance`` records
every EC2 field the tool supports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple

from ..domain import Snapshot, SnapshotEntry
from ..handlers import PLUGINS


def take_snapshot(
    clients: Any,
    targets: List[Tuple[str, str]],
    region: str,
    now: datetime,
) -> Snapshot:
    """Read every supported field on each named resource. Read-only.

    ``targets`` is a list of ``(resource_type, resource_id)``. A resource whose field
    cannot be read is recorded with its error rather than omitted, so the file says what
    was attempted.
    """
    entries: List[SnapshotEntry] = []
    for resource_type, resource_id in targets:
        for operation in PLUGINS:
            if operation.resource_type != resource_type:
                continue
            try:
                value: Optional[str] = operation.read_live_value(clients, resource_id)
                error: Optional[str] = None
            except Exception as exc:  # noqa: BLE001 - record the failure, do not abort
                value, error = None, "%s: %s" % (type(exc).__name__, exc)
            entries.append(
                SnapshotEntry(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    field_name=operation.field_name,
                    operation=operation.name,
                    value=value,
                    error=error,
                )
            )
    return Snapshot(taken_at=now, region=region, entries=entries)
