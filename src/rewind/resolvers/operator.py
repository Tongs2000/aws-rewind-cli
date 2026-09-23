"""Anchor supplied by the operator with ``--set``.

The tool's core promise is that it never invents a previous value. An operator-supplied
value does not break that promise - a human asserted it - but it is categorically weaker
than evidence, so it is recorded as :attr:`~rewind.models.Confidence.ASSERTED` and never
as HIGH. An audit trail that blurs "we established this" with "somebody told us" is worse
than no audit trail.

Two deliberate choices:

* this resolver sits **last** in the chain, so a ``--set`` can only fill a genuine
  UNKNOWN. Silently overriding proven evidence is how a careless revert happens;
* every selector that went unused is reported. A typo in a resource id, or a value set for
  a field the tool could already prove, must not pass quietly.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..errors import RewindError
from ..domain import Anchor, Confidence, chain_id
from .base import AnchorRequest, AnchorResolver


class SetSelectorError(RewindError):
    """A ``--set`` argument could not be understood."""


def parse_set(argument: str) -> Tuple[str, str]:
    """``chn-abc123=t3.micro`` or ``i-0abc.instanceType=t3.micro``."""
    if "=" not in argument:
        raise SetSelectorError(
            "--set needs SELECTOR=VALUE, for example "
            "--set i-0abc123.instanceType=t3.micro (got %r)" % argument
        )
    selector, value = argument.split("=", 1)
    selector, value = selector.strip(), value.strip()
    if not selector or not value:
        raise SetSelectorError("--set needs a non-empty selector and value (got %r)" % argument)
    return selector, value


class OperatorSuppliedResolver(AnchorResolver):
    name = "operator-supplied"
    description = "a value the operator passed with --set (asserted, not proven)"

    def __init__(self, assignments: Optional[Dict[str, str]] = None) -> None:
        self.assignments: Dict[str, str] = dict(assignments or {})
        #: selectors that were actually used, so the caller can report the rest
        self.used: List[str] = []
        #: (selector, supplied, proven) where evidence already existed
        self.ignored: List[Tuple[str, str, str]] = []

    @classmethod
    def from_arguments(cls, arguments: Optional[List[str]]) -> "OperatorSuppliedResolver":
        return cls(dict(parse_set(a) for a in (arguments or [])))

    def available(self) -> bool:
        return bool(self.assignments)

    def unavailable_reason(self) -> Optional[str]:
        return None if self.assignments else "no --set values were supplied"

    def _selectors_for(self, request: AnchorRequest) -> List[str]:
        """Both accepted spellings of "this chain": its id, or resource.field."""
        return [
            chain_id(request.resource_id, request.path),
            "%s.%s" % (request.resource_id, request.field_name),
        ]

    def match(self, request: AnchorRequest) -> Optional[Tuple[str, str]]:
        for selector in self._selectors_for(request):
            if selector in self.assignments:
                return selector, self.assignments[selector]
        return None

    def note_ignored(self, request: AnchorRequest, proven_value: str) -> None:
        """Record a --set that was not needed because the value was already proven."""
        matched = self.match(request)
        if matched is not None and matched[1] != proven_value:
            self.ignored.append((matched[0], matched[1], proven_value))

    def unused_selectors(self) -> List[str]:
        ignored = {selector for selector, _, _ in self.ignored}
        return sorted(
            s for s in self.assignments if s not in self.used and s not in ignored
        )

    def resolve(self, request: AnchorRequest) -> Anchor:
        matched = self.match(request)
        if matched is None:
            return Anchor.unknown(
                source=self.name,
                note="no --set value matches %s.%s"
                % (request.resource_id, request.field_name),
            )
        selector, value = matched
        self.used.append(selector)
        return Anchor(
            value=value,
            confidence=Confidence.ASSERTED,
            source=self.name,
            evidence_event_ids=[],
            note="supplied by the operator via --set %s=%s; the tool did not verify it"
            % (selector, value),
        )
