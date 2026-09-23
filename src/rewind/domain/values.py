"""Rendering and comparing state values.

Every state value the tool handles is carried as a **display string**, so a value read
out of CloudTrail and a value read back from a Describe/Get API compare unambiguously.
Typed values exist only inside revert parameters, where boto3 needs them.

These helpers are pure and sit at the bottom of the graph: ``store`` needs
:func:`parse_time` to read a plan file and must not depend on the CloudTrail layer to
get it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .constants import LIST_SEPARATOR, MAX_VALUE_LENGTH

UTC = timezone.utc


def iso(value: datetime) -> str:
    """The one timestamp format the tool writes."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime:
    """Accept the several shapes boto3, CloudTrail JSON and fixtures hand us."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def bool_str(value: Any) -> str:
    return "true" if bool(value) else "false"


def optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "enabled"):
            return True
        if lowered in ("false", "0", "no", "disabled"):
            return False
        return None
    return bool(value)


def render(value: Any) -> Optional[str]:
    """One display string per scalar leaf, or None when it is not a settable value.

    Used by both the generic parser and the plugins, so a boolean reads the same
    (``"true"``) however it was discovered.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value if 0 < len(value) <= MAX_VALUE_LENGTH else None
    return None


def render_values(values: list) -> str:
    """One display string for a path that carried several values.

    Document order is preserved rather than sorted: order is meaningless for a security
    group set but can matter elsewhere, and silently reordering a list the tool then
    treats as unchanged would be a quieter bug than reporting a reorder that did not
    matter.
    """
    return values[0] if len(values) == 1 else LIST_SEPARATOR.join(values)
