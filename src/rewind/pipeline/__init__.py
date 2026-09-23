"""Orchestration: one module per verb the CLI exposes.

    scan    -> what changed in a window, CloudTrail only
    plan    -> anchored chains, capability tiers, described reverts
    diff    -> a plan compared against live state
    revert  -> the only mutating path in the tool
    snapshot-> record live values for later use as evidence

Everything below this layer is pure or read-only. ``revert`` is the one place that writes,
and it is gated three ways; see its module docstring.
"""

from .diff import Verdict, diff_plan
from .plan import DEFAULT_LOOKBACK_DAYS, plan
from .plan_build import build_chains, extract_mutations
from .revert import Outcome, Reverter, revert_order
from .scan import ScanResult, scan
from .snapshot import take_snapshot

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "Outcome",
    "Reverter",
    "ScanResult",
    "Verdict",
    "build_chains",
    "diff_plan",
    "extract_mutations",
    "plan",
    "revert_order",
    "scan",
    "take_snapshot",
]
