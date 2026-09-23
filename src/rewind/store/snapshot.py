"""Writing and reading a snapshot document.

A plain local JSON file. No AWS resource is created, nothing is charged, and the file is the
operator's to keep, commit or throw away. Taking one is an *action* and lives in
:mod:`rewind.pipeline.snapshot`; this module only knows the format, which is what lets
``store`` depend on nothing but :mod:`rewind.domain`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..domain import SNAPSHOT_FORMAT_VERSION, Snapshot, SnapshotEntry, iso
from .document import (
    DocumentError,
    read_json,
    require,
    require_list,
    require_mapping,
    require_object,
    require_version,
    required_time,
)

CREATE = "create one with `rewind snapshot`"


class SnapshotFileError(DocumentError):
    """The snapshot document is missing, unreadable, or not one this build understands."""


def dump(snapshot: Snapshot) -> Dict[str, Any]:
    return {
        "rewindSnapshotVersion": SNAPSHOT_FORMAT_VERSION,
        "takenAt": iso(snapshot.taken_at),
        "region": snapshot.region,
        "entries": [_entry_dict(e) for e in snapshot.entries],
    }


def _entry_dict(entry: SnapshotEntry) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "resourceType": entry.resource_type,
        "resourceId": entry.resource_id,
        "field": entry.field_name,
        "operation": entry.operation,
        "value": entry.value,
    }
    if entry.error:
        body["error"] = entry.error
    return body


def parse(body: Any, path: Optional[str] = None) -> Snapshot:
    body = require_object(body, "snapshot", SnapshotFileError)
    require_version(
        body, "rewindSnapshotVersion", SNAPSHOT_FORMAT_VERSION, "snapshot", CREATE,
        SnapshotFileError,
    )
    require(body, "takenAt", "snapshot", SnapshotFileError)

    entries: List[SnapshotEntry] = []
    for index, raw in enumerate(
        require_list(body, "entries", "snapshot", SnapshotFileError)
    ):
        raw = require_mapping(raw, index, "snapshot entry", SnapshotFileError)
        for key in ("resourceType", "resourceId", "field"):
            if key not in raw:
                raise SnapshotFileError("snapshot entry %d is missing %r" % (index, key))
        entries.append(
            SnapshotEntry(
                resource_type=raw["resourceType"],
                resource_id=raw["resourceId"],
                field_name=raw["field"],
                operation=raw.get("operation", ""),
                value=raw.get("value"),
                error=raw.get("error"),
            )
        )
    return Snapshot(
        taken_at=required_time(body["takenAt"], "snapshot", SnapshotFileError),
        region=body.get("region", ""),
        entries=entries,
        path=path,
    )


def load(path: str) -> Snapshot:
    return parse(read_json(path, "snapshot", SnapshotFileError), path=str(path))
