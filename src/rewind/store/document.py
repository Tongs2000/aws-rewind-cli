"""Reading a versioned JSON document the tool wrote.

Plan files and snapshot files are the tool's only state, so they are a real contract -
between commands and across versions. A document may have been hand-edited, produced by a
different build, or truncated by a failed write, so every one is validated rather than
trusted.

The validation itself was written twice, once per format, with the error wording drifting
between them. It lives here now: one place that decides what "not a rewind document" and
"version I do not understand" sound like.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Type

from ..domain import parse_time
from ..errors import RewindError


class DocumentError(RewindError):
    """A document is missing, unreadable, or not one this build understands."""


def read_json(path: str, kind: str, error: Type[DocumentError]) -> Any:
    """Load a JSON file, turning every failure into one clear sentence."""
    location = Path(path)
    try:
        text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise error("no such %s file: %s" % (kind, path))
    except OSError as exc:
        raise error("cannot read %s: %s" % (path, exc))
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise error("%s is not valid JSON: %s" % (path, exc))


def require_object(body: Any, kind: str, error: Type[DocumentError]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise error("a %s must be a JSON object" % kind)
    return body


def require_version(
    body: Dict[str, Any],
    key: str,
    expected: int,
    kind: str,
    hint: str,
    error: Type[DocumentError],
) -> int:
    """A missing version means "not ours"; a different one means "regenerate it".

    The two are worth distinguishing: the first is usually the wrong file, the second is a
    build mismatch the operator can fix.
    """
    version = body.get(key)
    if version is None:
        raise error("not a rewind %s: no %s. %s" % (kind, key, hint))
    if version != expected:
        raise error(
            "%s format version %r is not supported by this build (expected %d); %s"
            % (kind, version, expected, hint)
        )
    return int(version)


def require(body: Dict[str, Any], key: str, where: str, error: Type[DocumentError]) -> Any:
    if key not in body:
        raise error("%s is missing %r" % (where, key))
    return body[key]


def require_list(
    body: Dict[str, Any], key: str, kind: str, error: Type[DocumentError]
) -> List[Any]:
    value = body.get(key)
    if not isinstance(value, list):
        raise error("%s %s must be an array" % (kind, key))
    return value


def require_mapping(
    raw: Any, index: int, kind: str, error: Type[DocumentError]
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("%s %d is not an object" % (kind, index))
    return raw


def optional_time(raw: Any, where: str, error: Type[DocumentError]):
    if raw is None:
        return None
    try:
        return parse_time(raw)
    except (TypeError, ValueError) as exc:
        raise error("%s has an unparseable timestamp %r: %s" % (where, raw, exc))


def required_time(raw: Any, where: str, error: Type[DocumentError]):
    try:
        return parse_time(raw)
    except (TypeError, ValueError) as exc:
        raise error("%s has an unparseable timestamp %r: %s" % (where, raw, exc))
