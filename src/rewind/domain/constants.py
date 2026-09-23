"""Constants shared across every layer.

They live at the bottom of the dependency graph on purpose. Previously
``timeutil`` reached into the CloudTrail module for ``RETENTION_DAYS`` and the inverse
builder reached into the generic parser for ``LIST_SEPARATOR`` - two modules importing a
layer above them for the sake of one integer. A constant is not a reason to couple
modules.
"""

from __future__ import annotations

#: Handler name used when no plugin claimed the change.
GENERIC_HANDLER = "generic"

#: Sentinel for "this field has no value at all" (no provisioned concurrency config
#: exists). Distinct from an unproven anchor, which is ``None``.
ABSENT = "NONE"

#: Joins the values of a list-valued field into one display string.
LIST_SEPARATOR = ","

#: Leaf values longer than this are documents, not settings, and are not treated as
#: field values.
MAX_VALUE_LENGTH = 256

#: CloudTrail event history retention. Nothing older can be queried this way.
RETENTION_DAYS = 90

#: Typical CloudTrail delivery lag. A window ending inside it may still be incomplete.
DELIVERY_LAG_SECONDS = 15 * 60

#: Plan document format. Bumped to 2 when chains became path-addressed.
PLAN_FORMAT_VERSION = 2

#: Snapshot document format.
SNAPSHOT_FORMAT_VERSION = 1
