"""Resolver registry and the default chain.

Priority order is the whole design. Strongest, retention-immune evidence first; an
operator's assertion last, so it can only fill a gap and never override proof:

1. ``response-elements``  the changing call's own response carried the old value
2. ``config-history``     AWS Config, when the account already records it
3. ``local-snapshot``     a snapshot the operator chose to take beforehand
4. ``cloudtrail-window``  an earlier event in the window that set this field
5. ``creation-event``     the resource's creation event
6. ``operator-supplied``  a ``--set`` value, recorded as ASSERTED

Resolvers 2, 3 and 6 are inert unless switched on, and report *why* they are inert rather
than vanishing from the chain. "No old value exists" and "the tool did not look there" are
different answers and the operator needs to be able to tell them apart.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..domain import Snapshot
from .base import AnchorRequest, AnchorResolver, ResolverChain, intervening_change
from .cloudtrail_window import CloudTrailWindowResolver
from .config_history import ConfigHistoryResolver
from .creation_event import CreationEventResolver
from .local_snapshot import LocalSnapshotResolver
from .operator import OperatorSuppliedResolver, SetSelectorError, parse_set
from .response_elements import ResponseElementsResolver


def build_chain(
    snapshot: Optional[Snapshot] = None,
    config_client: Any = None,
    assignments: Optional[Dict[str, str]] = None,
) -> ResolverChain:
    """The default chain, with the optional sources enabled if they were supplied."""
    return ResolverChain(
        [
            ResponseElementsResolver(),
            ConfigHistoryResolver(client=config_client),
            LocalSnapshotResolver(snapshot=snapshot),
            CloudTrailWindowResolver(),
            CreationEventResolver(),
            OperatorSuppliedResolver(assignments=assignments),
        ]
    )


def default_chain() -> ResolverChain:
    """CloudTrail-only: what the tool can do with no extra input at all."""
    return build_chain()


def operator_resolver(chain: ResolverChain) -> Optional[OperatorSuppliedResolver]:
    """The ``--set`` resolver in a chain, so callers can report unused selectors."""
    for resolver in chain.resolvers:
        if isinstance(resolver, OperatorSuppliedResolver):
            return resolver
    return None


__all__ = [
    "AnchorRequest",
    "AnchorResolver",
    "CloudTrailWindowResolver",
    "ConfigHistoryResolver",
    "CreationEventResolver",
    "LocalSnapshotResolver",
    "OperatorSuppliedResolver",
    "ResolverChain",
    "ResponseElementsResolver",
    "SetSelectorError",
    "build_chain",
    "default_chain",
    "intervening_change",
    "operator_resolver",
    "parse_set",
]
