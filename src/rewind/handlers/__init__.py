"""Per-API knowledge. Depends on :mod:`rewind.domain` and :mod:`rewind.trail`.

Layout:

* :mod:`~rewind.handlers.protocols` - the three roles a handler can fill. Read this first.
* :mod:`~rewind.handlers.base` - defaults that keep a handler short.
* :mod:`~rewind.handlers.generic` - handling for APIs nobody wrote code for.
* :mod:`~rewind.handlers.aws` - one module per supported AWS field. **Add files here.**
* :mod:`~rewind.handlers.registry` - the plugin list and the dispatch over it.
"""

from .base import BaseHandler
from .protocols import (
    MISMATCH,
    PENDING,
    VERIFIED,
    Actuator,
    Historian,
    Identified,
    Parser,
    performed,
    step,
)
from .registry import (
    GENERIC,
    GENERIC_HANDLER,
    PLUGINS,
    capability_of,
    get_operation,
    handler_for,
    is_mutating,
    mutating_event_names,
    parse_event,
    plugin_coverage,
    register_plugin,
    relevant_event_names,
)

__all__ = [
    "GENERIC",
    "GENERIC_HANDLER",
    "MISMATCH",
    "PENDING",
    "PLUGINS",
    "VERIFIED",
    "Actuator",
    "BaseHandler",
    "Historian",
    "Identified",
    "Parser",
    "capability_of",
    "get_operation",
    "handler_for",
    "is_mutating",
    "mutating_event_names",
    "parse_event",
    "performed",
    "plugin_coverage",
    "register_plugin",
    "relevant_event_names",
    "step",
]
