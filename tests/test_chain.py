"""Chaining: the value before the first change is the only unknown per field."""

from __future__ import annotations

from conftest import DB_INSTANCE, FUNCTION_ALIAS, INSTANCE_A, INSTANCE_B
from rewind.domain import ABSENT, Confidence
from rewind.pipeline import plan


def build(session, source=None, **kwargs):
    return plan(
        source=source or session.source(),
        query=session.query(kwargs.pop("identity", None)),
        now=kwargs.pop("now", session.end_time),
        **kwargs
    )


def chains_by_key(result):
    return {(c.resource_id, c.field_name): c for c in result.chains}


def test_repeated_changes_to_one_field_collapse_into_one_chain(session):
    """Two resizes in the session, one chain, one anchor - not two unknowns."""
    result = build(session)
    chain = chains_by_key(result)[(INSTANCE_A, "instanceType")]

    assert len(chain.changes) == 2
    assert [(c.sequence, c.before, c.after) for c in chain.changes] == [
        (1, "t3.micro", "t3.large"),
        (2, "t3.large", "t3.small"),
    ]
    # Only the first step needed an anchor; the second chained off the first.
    assert chain.net_before == "t3.micro"
    assert chain.net_after == "t3.small"
    assert chain.confidence is Confidence.HIGH
    assert chain.executable is True
    assert any("2 changes to this field" in n for n in chain.notes)


def test_intermediate_value_is_never_the_revert_target(session):
    """The revert restores the pre-session value, not the previous step's value."""
    chain = chains_by_key(build(session))[(INSTANCE_A, "instanceType")]

    assert chain.revert["targetValue"] == "t3.micro"
    assert "t3.large" not in str(chain.revert)


def test_lambda_chain_links_through_an_arn_and_a_bare_name(session):
    """The same alias addressed two ways is one chain."""
    chain = chains_by_key(build(session))[(FUNCTION_ALIAS, "provisionedConcurrency")]

    assert len(chain.changes) == 2
    assert [(c.before, c.after) for c in chain.changes] == [(ABSENT, "1"), ("1", "5")]
    assert chain.net_before == ABSENT
    assert chain.net_after == "5"


def test_each_instance_of_a_multi_instance_call_is_its_own_chain(session):
    """One MonitorInstances call, two instances, two independent anchors."""
    chains = chains_by_key(build(session))

    proven = chains[(INSTANCE_A, "monitoring")]
    unprovable = chains[(INSTANCE_B, "monitoring")]
    assert proven.changes[0].event_id == unprovable.changes[0].event_id
    assert (proven.net_before, proven.confidence) == ("disabled", Confidence.MEDIUM)
    assert (unprovable.net_before, unprovable.confidence) == (None, Confidence.UNKNOWN)


def test_chain_ids_are_stable_across_runs(session):
    first = build(session)
    second = build(session)

    assert [c.chain_id for c in first.chains] == [c.chain_id for c in second.chains]


def test_chains_are_ordered_by_first_change(session):
    result = build(session)
    times = [c.first_change_at for c in result.chains]

    assert times == sorted(times)


def test_net_no_op_is_detected_and_not_offered_as_a_revert(session):
    """An agent that changed a value and changed it back leaves nothing to do."""
    from rewind.trail import from_lookup_record
    from rewind.trail import StaticEventSource
    from conftest import as_wire_record

    records = session.wire_records()
    # Append a third resize taking instance A back to its pre-session type.
    restore = as_wire_record(
        {
            "EventId": "s0000009-0000-4000-8000-00000000s009",
            "EventName": "ModifyInstanceAttribute",
            "CloudTrailEvent": {
                "eventID": "s0000009-0000-4000-8000-00000000s009",
                "eventTime": "2026-09-22T17:29:00Z",
                "eventName": "ModifyInstanceAttribute",
                "eventSource": "ec2.amazonaws.com",
                "awsRegion": "us-west-1",
                "userIdentity": {
                    "arn": "arn:aws:sts::111122223333:assumed-role/PerfAgentRole/perf-agent"
                },
                "requestParameters": {
                    "instanceId": INSTANCE_A,
                    "instanceType": {"value": "t3.micro"},
                },
                "responseElements": {"_return": True},
            },
        }
    )
    events = [from_lookup_record(r) for r in records] + [from_lookup_record(restore)]
    result = build(session, source=StaticEventSource(events))

    chain = chains_by_key(result)[(INSTANCE_A, "instanceType")]
    assert len(chain.changes) == 3
    assert chain.net_before == chain.net_after == "t3.micro"
    assert chain.net_no_op is True
    assert chain.executable is False
    assert chain.revert["executable"] is False
    assert any("net effect is zero" in n for n in chain.notes)
    assert result.stats["netNoOp"] == 1


def test_evidence_lists_the_anchor_plus_every_session_step(session):
    chain = chains_by_key(build(session))[(INSTANCE_A, "instanceType")]

    assert chain.evidence_event_ids == [
        session.event("h0000003").event_id,
        session.event("s0000001").event_id,
        session.event("s0000002").event_id,
    ]


def test_rds_chain_is_anchored_without_any_history(session):
    """The change event's own response carries the old value, so retention is irrelevant."""
    chain = chains_by_key(build(session, source=session.without("h00000")))[
        (DB_INSTANCE, "multiAZ")
    ]

    assert (chain.net_before, chain.net_after) == ("false", "true")
    assert chain.confidence is Confidence.HIGH
    assert chain.anchor.source == "response-elements"
    assert chain.anchor.evidence_event_ids == [session.event("s0000007").event_id]
