"""Human-readable output: one module per command, plus the shared table primitives.

Rendering is kept out of :mod:`~rewind.pipeline` so that a command's result is a value an
operator can also get as JSON, and so changing how something looks cannot change what the
tool concluded. See :mod:`~rewind.report.table` for the two rules every table follows.
"""

from .coverage import render_operations, render_resolvers
from .diff import render_diff
from .plan import render_plan
from .revert import render_revert
from .scan import render_scan
from .snapshot import render_snapshot
from .table import UNPROVEN

__all__ = [
    "UNPROVEN",
    "render_diff",
    "render_operations",
    "render_plan",
    "render_resolvers",
    "render_revert",
    "render_scan",
    "render_snapshot",
]
