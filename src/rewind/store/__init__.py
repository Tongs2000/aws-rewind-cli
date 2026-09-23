"""Reading and writing the tool's documents. Depends only on :mod:`rewind.domain`.

There is no database: a plan file *is* the state, and a snapshot file is optional evidence
the operator chose to keep. Both are validated on the way in by
:mod:`~rewind.store.document`, which is the single place that decides what a bad document
sounds like.
"""

from . import plan, snapshot
from .document import DocumentError
from .plan import PlanFileError
from .snapshot import SnapshotFileError

__all__ = [
    "DocumentError",
    "PlanFileError",
    "SnapshotFileError",
    "plan",
    "snapshot",
]
