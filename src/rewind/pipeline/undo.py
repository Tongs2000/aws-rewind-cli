"""undo: the whole sequence in one call, without losing the review step.

``plan`` then ``diff`` then ``revert`` is the flow, and running it by hand means three
commands and a file to carry between them. This runs all three and reports each, but it is
**not** a different safety model: without ``--confirm`` nothing is called, exactly as
``revert`` alone behaves. The review step is preserved by making the dry run the default, not
by refusing to compose the commands.

Two things it does that a human juggling three commands tends to skip:

* the plan is always written to a file, even in a dry run, so the run is reproducible and
  auditable after the fact - ``diff`` and ``revert`` can be re-run against the same document;
* ``diff`` runs before any write and its conflicts are reported up front, so "somebody else
  changed this too" is visible before you decide, not after.

A conflict does not abort the run. ``revert`` already refuses a conflicted field one at a
time, having re-read it immediately beforehand, and aborting everything because one field
drifted would leave the rest of an incident unhandled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..domain import Plan, Query
from ..resolvers import ResolverChain
from ..trail import EventSource
from .diff import PlanDiff, diff_plan
from .plan import DEFAULT_LOOKBACK_DAYS, plan as build_plan
from .revert import Outcome, RevertRun, Reverter

UTC = timezone.utc


@dataclass
class UndoRun:
    """Everything the sequence produced, so a caller can report or re-check any stage."""

    plan: Plan
    diff: PlanDiff
    revert: RevertRun
    plan_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.revert.dry_run

    @property
    def changed_anything(self) -> bool:
        return any(
            r.outcome in (Outcome.REVERTED, Outcome.SUBMITTED) for r in self.revert.results
        )

    @property
    def conflicts(self) -> List[Any]:
        """Fields something outside the plan has touched. Worth seeing before deciding."""
        return self.diff.conflicts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planPath": self.plan_path,
            "dryRun": self.dry_run,
            "warnings": self.warnings,
            "plan": self.plan.to_dict(),
            "diff": self.diff.to_dict(),
            "revert": self.revert.to_dict(),
        }


def undo(
    source: EventSource,
    clients: Any,
    query: Query,
    resolver: Optional[ResolverChain] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    confirm: bool = False,
    wait: bool = True,
    only: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
    tool_version: str = "0.1.0",
) -> UndoRun:
    """Plan, diff and revert in one pass. Nothing is written unless ``confirm`` is true."""
    now = now or datetime.now(tz=UTC)

    plan = build_plan(
        source=source,
        query=query,
        resolver=resolver,
        lookback_days=lookback_days,
        now=now,
        tool_version=tool_version,
    )
    # Diff before reverting, always - including in a dry run, where it is the only thing that
    # can tell "ready to revert" from "somebody else has been here since".
    diff = diff_plan(plan=plan, clients=clients, source=None, now=now)
    revert = Reverter(clients=clients, dry_run=not confirm, wait=wait).run(
        plan, only=only, now=now
    )
    return UndoRun(plan=plan, diff=diff, revert=revert, warnings=list(plan.warnings))
