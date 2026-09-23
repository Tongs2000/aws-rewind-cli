"""Anchor resolution: which evidence wins, what is rejected, and honest UNKNOWNs."""

from __future__ import annotations

from conftest import DB_INSTANCE, FUNCTION_ALIAS, INSTANCE_A, INSTANCE_B
from rewind.domain import ABSENT, Confidence
from rewind.pipeline import plan
from rewind.resolvers import (
    CloudTrailWindowResolver,
    CreationEventResolver,
    ResolverChain,
    ResponseElementsResolver,
    default_chain,
)


def anchors(session, source=None, resolver=None, **kwargs):
    result = plan(
        source=source or session.source(),
        query=session.query(),
        resolver=resolver,
        now=session.end_time,
        **kwargs
    )
    return {(c.resource_id, c.field_name): c.anchor for c in result.chains}, result


def test_latest_event_that_set_the_same_field_wins(session):
    """An earlier t3.nano launch and a later t3.micro resize: t3.micro wins."""
    found, _ = anchors(session)
    anchor = found[(INSTANCE_A, "instanceType")]

    assert anchor.value == "t3.micro"
    assert anchor.confidence is Confidence.HIGH
    assert anchor.source == "cloudtrail-window"
    assert anchor.evidence_event_ids == [session.event("h0000003").event_id]


def test_a_failed_call_is_not_a_state_transition(session):
    """The most recent instanceType event is a failed t3.2xlarge - it must be ignored."""
    failed = session.event("h0000005")
    assert failed.error_code
    assert failed.request_parameters["instanceType"]["value"] == "t3.2xlarge"
    assert failed.event_time > session.event("h0000003").event_time

    anchor = anchors(session)[0][(INSTANCE_A, "instanceType")]
    assert anchor.value == "t3.micro"
    assert failed.event_id not in anchor.evidence_event_ids


def test_unrelated_events_never_supply_a_value(session):
    """Stop/Start, and a Modify of a different attribute, prove nothing about the type."""
    anchor = anchors(session)[0][(INSTANCE_A, "instanceType")]

    ignored = {
        session.event("h0000004").event_id,  # Modify disableApiTermination
        session.event("h0000006").event_id,  # StopInstances
        session.event("h0000007").event_id,  # StartInstances
    }
    assert not ignored & set(anchor.evidence_event_ids)


def test_response_elements_outrank_everything_else(session):
    """RDS carries the old value in its own response, so no history is consulted."""
    anchor = anchors(session)[0][(DB_INSTANCE, "multiAZ")]

    assert (anchor.value, anchor.confidence) == ("false", Confidence.HIGH)
    assert anchor.source == "response-elements"
    assert anchor.evidence_event_ids == [session.event("s0000007").event_id]


def test_creation_event_is_the_fallback_and_is_only_medium(session):
    """Monitoring has no prior Monitor/Unmonitor call; the launch event proves it."""
    anchor = anchors(session)[0][(INSTANCE_A, "monitoring")]

    assert (anchor.value, anchor.confidence) == ("disabled", Confidence.MEDIUM)
    assert anchor.source == "creation-event"
    assert anchor.evidence_event_ids == [session.event("h0000001").event_id]


def test_lambda_none_is_inferred_only_from_a_visible_creation(session):
    anchor = anchors(session)[0][(FUNCTION_ALIAS, "provisionedConcurrency")]

    assert (anchor.value, anchor.confidence) == (ABSENT, Confidence.MEDIUM)
    assert anchor.source == "creation-event"


def test_without_the_creation_event_lambda_becomes_unknown(session):
    """Absence of evidence must not be read as evidence of absence."""
    anchor = anchors(session, source=session.without("h0000002"))[0][
        (FUNCTION_ALIAS, "provisionedConcurrency")
    ]

    assert anchor.value is None
    assert anchor.confidence is Confidence.UNKNOWN


def test_a_truncated_lookup_blocks_the_creation_inference(session):
    """If a lookup was cut short, "no intervening event" means nothing."""
    anchor = anchors(session, source=session.source(truncated=True))[0][
        (FUNCTION_ALIAS, "provisionedConcurrency")
    ]

    assert anchor.confidence is Confidence.UNKNOWN
    assert "truncated" in anchor.note


def test_a_resource_with_no_history_in_the_window_is_unknown(session):
    """The CloudTrail retention gap: a long-lived instance nobody touched recently."""
    found, result = anchors(session)

    for field_name in ("instanceType", "monitoring"):
        anchor = found[(INSTANCE_B, field_name)]
        assert anchor.value is None
        assert anchor.confidence is Confidence.UNKNOWN
        # The message must say which sources were tried, not just "unknown".
        assert "response-elements" in anchor.note
        assert "cloudtrail-window" in anchor.note
    assert result.stats["unprovable"] == 2


def test_unknown_chains_are_reported_but_not_executable(session):
    _, result = anchors(session)
    chain = next(
        c for c in result.chains if c.resource_id == INSTANCE_B and c.field_name == "instanceType"
    )

    # The change itself is still fully visible - only the old value is missing.
    assert chain.net_after == "t3.small"
    assert chain.changes[0].after == "t3.small"
    assert chain.executable is False
    assert chain.revert == {
        "executable": False,
        "reason": "the previous value of instanceType is not proven",
    }
    assert any("--set" in n for n in chain.notes)


def test_reordering_the_chain_cannot_downgrade_an_anchor(session):
    """Promoting the weaker resolver does not make it answer a question it should not.

    ``CreationEventResolver`` re-checks for an intervening field-setting event, so even
    placed ahead of ``cloudtrail-window`` it defers - the strongest evidence still wins.
    """
    reordered = ResolverChain(
        [ResponseElementsResolver(), CreationEventResolver(), CloudTrailWindowResolver()]
    )
    promoted, _ = anchors(session, resolver=reordered)
    default_order, _ = anchors(session)

    for key in ((DB_INSTANCE, "multiAZ"), (INSTANCE_A, "instanceType"), (INSTANCE_A, "monitoring")):
        assert promoted[key].value == default_order[key].value
        assert promoted[key].source == default_order[key].source
    # Instance A's type comes from the explicit resize, not the weaker launch event.
    assert promoted[(INSTANCE_A, "instanceType")].source == "cloudtrail-window"
    assert promoted[(INSTANCE_A, "instanceType")].value == "t3.micro"


def test_creation_resolver_declines_when_a_later_event_set_the_field(session):
    """Ordering safety: creation must not answer when a field-setting event exists."""
    resolver = ResolverChain([CreationEventResolver()])
    found, _ = anchors(session, resolver=resolver)

    anchor = found[(INSTANCE_A, "instanceType")]
    # h0000003 set instanceType after launch, so creation-event steps aside.
    assert anchor.confidence is Confidence.UNKNOWN
    assert "cloudtrail-window owns this case" in anchor.note


def test_optional_resolvers_are_listed_as_inactive_not_hidden(session):
    """"No old value exists" and "the tool did not look there" must stay distinguishable."""
    described = {d["name"]: d for d in default_chain().describe()}

    assert described["config-history"]["available"] is False
    assert "--use-config" in described["config-history"]["unavailableReason"]
    assert described["local-snapshot"]["available"] is False
    assert "--snapshot" in described["local-snapshot"]["unavailableReason"]
    assert described["operator-supplied"]["available"] is False
    assert "--set" in described["operator-supplied"]["unavailableReason"]
    # The always-on ones are on.
    for name in ("response-elements", "cloudtrail-window", "creation-event"):
        assert described[name]["available"] is True


def test_priority_order_is_part_of_the_contract():
    """Strongest evidence first; an operator's assertion last, so it can only fill a gap."""
    assert [d["name"] for d in default_chain().describe()] == [
        "response-elements",
        "config-history",
        "local-snapshot",
        "cloudtrail-window",
        "creation-event",
        "operator-supplied",
    ]
