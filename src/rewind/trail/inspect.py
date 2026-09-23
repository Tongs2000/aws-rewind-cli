"""Reading values out of CloudTrail request and response parameters.

Pure functions over the nested dicts CloudTrail delivers. They are here rather than in the
generic parser because plugins need them too: ``dig`` and ``items_of`` are how every plugin
reaches into an event, and keeping them in the parser module made the inverse builder
import the parser for a constant.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Tuple

from ..domain import render

def dig(source: Any, *path: str, default: Any = None) -> Any:
    """Safe nested lookup: ``dig(rp, "instanceType", "value")``."""
    current = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current if current is not None else default

def items_of(container: Any) -> List[Dict[str, Any]]:
    """EC2 request parameters use the ``{"instancesSet": {"items": [...]}}`` shape."""
    items = dig(container, "items", default=[])
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

def instance_ids(container: Any) -> List[str]:
    return [i["instanceId"] for i in items_of(container) if i.get("instanceId")]

def flatten(source: Any, prefix: Tuple[str, ...] = ()) -> List[Tuple[Tuple[str, ...], Any]]:
    """Every scalar leaf in requestParameters, with its path.

    List indices are dropped from the path, so ``instancesSet.items[0].instanceId`` and
    ``...items[1].instanceId`` share one path. That is what lets one call covering several
    resources become several chains on the same field.
    """
    leaves: List[Tuple[Tuple[str, ...], Any]] = []
    if isinstance(source, dict):
        for key, value in source.items():
            leaves.extend(flatten(value, prefix + (str(key),)))
    elif isinstance(source, (list, tuple)):
        for item in source:
            leaves.extend(flatten(item, prefix))
    else:
        leaves.append((prefix, source))
    return leaves


def collect(source: Any) -> "OrderedDict[Tuple[str, ...], List[str]]":
    """Path -> every rendered value at it, in document order.

    A list-valued field (several security groups, several subnets) has one path and
    several values. Keeping only the first - which is what a plain dict would do -
    silently drops part of the change.
    """
    grouped: "OrderedDict[Tuple[str, ...], List[str]]" = OrderedDict()
    for path, value in flatten(source):
        rendered = render(value)
        if rendered is None:
            continue
        grouped.setdefault(path, []).append(rendered)
    return grouped
