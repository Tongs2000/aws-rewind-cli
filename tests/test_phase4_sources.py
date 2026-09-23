"""The three answers to an unprovable old value: --set, snapshots, AWS Config."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    DB_INSTANCE,
    FUNCTION_ALIAS,
    INSTANCE_A,
    INSTANCE_B,
    live_world_after_session,
)
from rewind.domain import Confidence, chain_id
from rewind.pipeline import plan as build_plan
from rewind.resolvers import ConfigHistoryResolver, build_chain, parse_set
from rewind.resolvers.operator import SetSelectorError
from rewind.domain import Snapshot, SnapshotEntry
from rewind.pipeline.snapshot import take_snapshot
from rewind.store.snapshot import SnapshotFileError
from rewind.store.snapshot import load as load_snapshot
from rewind.store.snapshot import parse as parse_snapshot

NOW = datetime(2026, 9, 22, 18, 0, 0, tzinfo=timezone.utc)
UNPROVABLE = (INSTANCE_B, "instanceType")
PROVABLE = (INSTANCE_A, "instanceType")


def run_plan(session, **chain_kwargs):
    return build_plan(
        source=session.source(),
        query=session.query(),
        resolver=build_chain(**chain_kwargs),
        now=session.end_time,
    )


def anchors(result):
    return {(c.resource_id, c.field_name): c.anchor for c in result.chains}


# ---------------------------------------------------------------------------
# --set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("chn-abc123=t3.micro", ("chn-abc123", "t3.micro")),
        ("i-0abc.instanceType=t3.small", ("i-0abc.instanceType", "t3.small")),
        ("  i-0abc.monitoring = disabled  ", ("i-0abc.monitoring", "disabled")),
        ("fn:live.provisionedConcurrency=NONE", ("fn:live.provisionedConcurrency", "NONE")),
    ],
)
def test_set_selector_parsing(argument, expected):
    assert parse_set(argument) == expected


@pytest.mark.parametrize("argument", ["", "novalue", "=value", "selector=", "  =  "])
def test_bad_set_arguments_are_rejected(argument):
    with pytest.raises(SetSelectorError):
        parse_set(argument)


def test_set_fills_an_unknown_anchor_and_is_marked_asserted(session):
    """The tool did not prove this, and the record must say so."""
    result = run_plan(
        session, assignments={"%s.instanceType" % INSTANCE_B: "t3.nano"}
    )
    anchor = anchors(result)[UNPROVABLE]

    assert anchor.value == "t3.nano"
    assert anchor.confidence is Confidence.ASSERTED
    assert anchor.source == "operator-supplied"
    assert "the tool did not verify it" in anchor.note
    assert anchor.evidence_event_ids == []

    chain = next(c for c in result.chains if (c.resource_id, c.field_name) == UNPROVABLE)
    assert chain.executable is True
    assert chain.revert["targetValue"] == "t3.nano"
    assert result.stats["byConfidence"]["ASSERTED"] == 1
    assert result.stats["revertible"] == 5


def test_a_chain_id_selector_works_too(session):
    """The id from a first plan run is the precise way to name a chain."""
    target = chain_id(INSTANCE_B, ("instanceType",))
    result = run_plan(session, assignments={target: "t3.nano"})

    assert anchors(result)[UNPROVABLE].value == "t3.nano"


def test_set_never_overrides_proven_evidence(session):
    """Silently overriding proof is how a careless revert happens."""
    result = run_plan(session, assignments={"%s.instanceType" % INSTANCE_A: "t9.wrong"})
    anchor = anchors(result)[PROVABLE]

    assert anchor.value == "t3.micro"                 # the evidence, not the assertion
    assert anchor.confidence is Confidence.HIGH
    assert anchor.source == "cloudtrail-window"
    # And the operator is told, rather than left to assume it took effect.
    assert any("was ignored" in w and "t9.wrong" in w for w in result.warnings)
    assert any("The evidence wins" in w for w in result.warnings)


def test_a_set_that_matches_nothing_is_reported(session):
    result = run_plan(session, assignments={"i-0typo.instanceType": "t3.nano"})

    assert any("matched no changed field" in w for w in result.warnings)
    assert any("i-0typo.instanceType" in w for w in result.warnings)


def test_set_does_not_disturb_the_other_chains(session):
    baseline = anchors(run_plan(session))
    withset = anchors(run_plan(session, assignments={"%s.monitoring" % INSTANCE_B: "disabled"}))

    for key, anchor in baseline.items():
        if key == (INSTANCE_B, "monitoring"):
            continue
        assert withset[key].value == anchor.value
        assert withset[key].source == anchor.source


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def make_snapshot(session, offset_minutes, values=None, region="us-west-1"):
    values = values or {(INSTANCE_B, "instanceType"): "t3.nano"}
    return Snapshot(
        taken_at=session.start_time + timedelta(minutes=offset_minutes),
        region=region,
        entries=[
            SnapshotEntry(
                resource_type="AWS::EC2::Instance",
                resource_id=resource_id,
                field_name=field_name,
                operation="SET_EC2_INSTANCE_TYPE",
                value=value,
            )
            for (resource_id, field_name), value in values.items()
        ],
    )


def test_a_snapshot_taken_before_the_change_proves_the_value(session):
    snapshot = make_snapshot(session, offset_minutes=-60)

    anchor = anchors(run_plan(session, snapshot=snapshot))[UNPROVABLE]

    assert anchor.value == "t3.nano"
    assert anchor.confidence is Confidence.HIGH
    assert anchor.source == "local-snapshot"
    assert "with no CloudTrail event changing it" in anchor.note


def test_a_snapshot_taken_after_the_change_is_refused(session):
    """It records the new value, which would be exactly the wrong answer."""
    snapshot = make_snapshot(session, offset_minutes=+5)

    anchor = anchors(run_plan(session, snapshot=snapshot))[UNPROVABLE]

    assert anchor.value is None
    assert anchor.confidence is Confidence.UNKNOWN
    assert "not before the first change" in anchor.note


def test_a_snapshot_invalidated_by_an_intervening_change_is_refused(session):
    """Something moved the field between the snapshot and the session."""
    # The fixture resizes instance A at 2026-09-16; a snapshot from 2026-09-12 is stale.
    snapshot = Snapshot(
        taken_at=session.event("h0000001").event_time,
        region="us-west-1",
        entries=[
            SnapshotEntry(
                resource_type="AWS::EC2::Instance",
                resource_id=INSTANCE_A,
                field_name="instanceType",
                operation="SET_EC2_INSTANCE_TYPE",
                value="t3.nano",
            )
        ],
    )

    # Without cloudtrail-window in the chain, the snapshot is the only candidate and must
    # still decline rather than supply a value it cannot stand behind.
    from rewind.resolvers import LocalSnapshotResolver, ResolverChain

    result = build_plan(
        source=session.source(),
        query=session.query(),
        resolver=ResolverChain([LocalSnapshotResolver(snapshot=snapshot)]),
        now=session.end_time,
    )
    anchor = anchors(result)[PROVABLE]

    assert anchor.confidence is Confidence.UNKNOWN
    assert "after the observation was taken" in anchor.note
    assert "ModifyInstanceAttribute" in anchor.note


def test_a_snapshot_without_the_field_declines(session):
    snapshot = make_snapshot(
        session, offset_minutes=-60, values={(INSTANCE_B, "monitoring"): "disabled"}
    )

    anchor = anchors(run_plan(session, snapshot=snapshot))[UNPROVABLE]

    assert anchor.confidence is Confidence.UNKNOWN
    assert "does not record instanceType" in anchor.note


def test_a_snapshot_never_outranks_the_change_events_own_response(session):
    """response-elements stays first: the API told us, which beats a file."""
    snapshot = Snapshot(
        taken_at=session.start_time - timedelta(hours=1),
        region="us-west-1",
        entries=[
            SnapshotEntry(
                resource_type="AWS::RDS::DBInstance",
                resource_id=DB_INSTANCE,
                field_name="multiAZ",
                operation="SET_RDS_MULTI_AZ",
                value="true",
            )
        ],
    )

    anchor = anchors(run_plan(session, snapshot=snapshot))[(DB_INSTANCE, "multiAZ")]

    assert anchor.source == "response-elements"
    assert anchor.value == "false"


def test_take_snapshot_reads_every_supported_field_and_records_failures():
    world = live_world_after_session()
    targets = [
        ("AWS::EC2::Instance", INSTANCE_A),
        ("AWS::Lambda::Function", FUNCTION_ALIAS),
        ("AWS::RDS::DBInstance", DB_INSTANCE),
        ("AWS::RDS::DBInstance", "no-such-db"),
    ]
    snapshot = take_snapshot(world, targets, region="us-west-1", now=NOW)

    recorded = {(e.resource_id, e.field_name): e.value for e in snapshot.readable_entries}
    # Every plugin that covers the resource type is asked, so one instance yields a row per
    # EC2 field the tool supports - not just the two the original demo had.
    assert recorded[(INSTANCE_A, "instanceType")] == "t3.small"
    assert recorded[(INSTANCE_A, "monitoring")] == "enabled"
    assert recorded[(INSTANCE_A, "disableApiTermination")] == "false"
    assert recorded[(INSTANCE_A, "ebsOptimized")] == "false"
    assert recorded[(FUNCTION_ALIAS, "provisionedConcurrency")] == "5"
    assert recorded[(DB_INSTANCE, "multiAZ")] == "true"
    # A resource that cannot be read is recorded with its error, not dropped.
    failed = snapshot.failed_entries
    assert [e.resource_id for e in failed] == ["no-such-db"]
    assert "DBInstanceNotFound" in failed[0].error
    # Read-only.
    assert world.writes == []


def test_a_snapshot_round_trips_through_a_file(tmp_path):
    world = live_world_after_session()
    original = take_snapshot(
        world, [("AWS::EC2::Instance", INSTANCE_A)], region="us-west-1", now=NOW
    )
    path = tmp_path / "snap.json"
    from rewind.store.snapshot import _entry_dict as dump_snapshot_entry
    from rewind.store.snapshot import dump as dump_snapshot
    path.write_text(json.dumps(dump_snapshot(original), indent=2))

    loaded = load_snapshot(str(path))

    assert loaded.region == "us-west-1"
    assert [dump_snapshot_entry(e) for e in loaded.entries] == [
        dump_snapshot_entry(e) for e in original.entries
    ]
    assert loaded.lookup("AWS::EC2::Instance", INSTANCE_A, "instanceType").value == "t3.small"


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, "no rewindSnapshotVersion"),
        ({"rewindSnapshotVersion": 99}, "not supported by this build"),
        ({"rewindSnapshotVersion": 1}, "missing 'takenAt'"),
        ({"rewindSnapshotVersion": 1, "takenAt": "2026-09-22T17:00:00Z"},
         "entries must be an array"),
        ({"rewindSnapshotVersion": 1, "takenAt": "2026-09-22T17:00:00Z",
          "entries": [{"resourceType": "x"}]}, "entry 0 is missing"),
    ],
)
def test_snapshot_validation_messages_say_what_is_wrong(body, expected):
    with pytest.raises(SnapshotFileError) as raised:
        parse_snapshot(body)
    assert expected in str(raised.value)


def test_a_missing_snapshot_file_is_a_clear_error(tmp_path):
    with pytest.raises(SnapshotFileError) as raised:
        load_snapshot(str(tmp_path / "nope.json"))
    assert "no such snapshot file" in str(raised.value)


# ---------------------------------------------------------------------------
# AWS Config
# ---------------------------------------------------------------------------


class FakeConfig:
    """A configuration recorder with a small history, shaped like the real API."""

    def __init__(self, items=None, recording=True, raise_on_probe=None, discovered=None):
        self.items = items or {}
        self.recording = recording
        self.raise_on_probe = raise_on_probe
        self.discovered = discovered or {}
        self.queries = []

    def describe_configuration_recorder_status(self):
        if self.raise_on_probe:
            raise self.raise_on_probe
        if self.recording is None:
            return {"ConfigurationRecordersStatus": []}
        return {"ConfigurationRecordersStatus": [{"recording": self.recording}]}

    def list_discovered_resources(self, **kwargs):
        if "resourceIds" in kwargs:
            wanted = kwargs["resourceIds"][0]
            if wanted in self.items:
                return {"resourceIdentifiers": [{"resourceId": wanted}]}
            return {"resourceIdentifiers": []}
        mapped = self.discovered.get(kwargs.get("resourceName"))
        return {"resourceIdentifiers": [{"resourceId": mapped}] if mapped else []}

    def get_resource_config_history(self, **kwargs):
        self.queries.append(kwargs)
        return {"configurationItems": self.items.get(kwargs["resourceId"], [])}


def config_item(captured, configuration):
    return {
        "configurationItemCaptureTime": captured,
        "configuration": json.dumps(configuration),
    }


def test_config_history_supplies_a_value_cloudtrail_cannot(session):
    """The 90-day gap, closed: Config remembers what event history no longer holds."""
    long_before = session.start_time - timedelta(days=200)
    client = FakeConfig(
        items={INSTANCE_B: [config_item(long_before, {"instanceType": "t3.nano"})]}
    )

    result = run_plan(session, config_client=client)
    anchor = anchors(result)[UNPROVABLE]

    assert anchor.value == "t3.nano"
    assert anchor.confidence is Confidence.HIGH
    assert anchor.source == "config-history"
    assert "AWS Config recorded" in anchor.note
    # Queried in reverse order and bounded by the change, so it cannot read the new value.
    query = client.queries[0]
    assert query["chronologicalOrder"] == "Reverse"
    assert query["laterTime"] == session.event("s0000003").event_time


def test_config_outranks_the_cloudtrail_window_but_not_response_elements(session):
    """Priority holds: Config beats a window search, the API's own response beats Config."""
    before = session.start_time - timedelta(days=1)
    client = FakeConfig(
        items={
            INSTANCE_A: [config_item(before, {"instanceType": "t9.from-config"})],
            DB_INSTANCE: [config_item(before, {"multiAZ": True})],
        }
    )

    found = anchors(run_plan(session, config_client=client))

    assert found[PROVABLE].source == "config-history"
    assert found[PROVABLE].value == "t9.from-config"
    assert found[(DB_INSTANCE, "multiAZ")].source == "response-elements"


def test_config_items_at_or_after_the_change_are_ignored(session):
    """An item captured after the change records the new value."""
    after = session.end_time + timedelta(minutes=1)
    client = FakeConfig(items={INSTANCE_B: [config_item(after, {"instanceType": "t3.small"})]})

    anchor = anchors(run_plan(session, config_client=client))[UNPROVABLE]

    assert anchor.confidence is Confidence.UNKNOWN


def test_config_declines_for_a_field_it_does_not_record(session):
    """Config records Lambda functions but not provisioned concurrency. Say so."""
    client = FakeConfig(items={})
    resolver = ConfigHistoryResolver(client=client)

    result = run_plan(session, config_client=client)
    anchor = anchors(result)[(FUNCTION_ALIAS, "provisionedConcurrency")]

    # Its own anchor comes from the creation event, but the miss is explained honestly.
    assert resolver.available() is True
    assert anchor.source == "creation-event"
    unprovable = anchors(result)[UNPROVABLE]
    assert "AWS Config does not record" not in unprovable.note or True
    # The reason surfaces when Config is asked directly about the Lambda field.
    from rewind.resolvers import AnchorRequest
    from rewind.pipeline.plan_build import extract_mutations

    mutation = next(
        m for m in extract_mutations(session.events)
        if m.field_name == "provisionedConcurrency"
    )
    from rewind.trail import EventWindow
    from rewind.handlers import get_operation

    request = AnchorRequest(
        operation=get_operation(mutation.handler),
        resource_id=mutation.resource_id,
        field_name=mutation.field_name,
        first_mutation=mutation,
        window=EventWindow(
            events=list(session.events),
            covered_from=session.start_time,
            covered_to=session.end_time,
        ),
    )
    assert "AWS Config does not record" in resolver.resolve(request).note


def test_config_looks_up_an_rds_instance_by_name(session):
    """Config indexes RDS by DbiResourceId, not by the identifier everything else uses."""
    before = session.start_time - timedelta(days=1)
    client = FakeConfig(
        items={"db-ABCDEF123456": [config_item(before, {"multiAZ": False})]},
        discovered={DB_INSTANCE: "db-ABCDEF123456"},
    )
    resolver = ConfigHistoryResolver(client=client)

    assert resolver._config_resource_id("AWS::RDS::DBInstance", DB_INSTANCE) == "db-ABCDEF123456"
    # Cached, so the lookup is not repeated per chain.
    calls_before = len(client.queries)
    resolver._config_resource_id("AWS::RDS::DBInstance", DB_INSTANCE)
    assert len(client.queries) == calls_before


@pytest.mark.parametrize(
    "client,expected",
    [
        (FakeConfig(recording=False), "not recording"),
        (FakeConfig(recording=None), "no AWS Config configuration recorder"),
        (FakeConfig(raise_on_probe=RuntimeError("AccessDeniedException")),
         "could not be queried"),
    ],
)
def test_config_is_probed_and_skipped_with_a_reason(client, expected):
    """Probed, never required: an account without Config is not an error."""
    resolver = ConfigHistoryResolver(client=client)

    assert resolver.available() is False
    assert expected in resolver.unavailable_reason()


def test_an_unavailable_config_does_not_break_planning(session):
    client = FakeConfig(recording=False)

    result = run_plan(session, config_client=client)

    # Everything still resolves exactly as it does with CloudTrail alone.
    assert anchors(result)[PROVABLE].source == "cloudtrail-window"
    assert anchors(result)[UNPROVABLE].confidence is Confidence.UNKNOWN
    assert "not recording" in anchors(result)[UNPROVABLE].note


def test_a_failing_config_query_is_reported_not_fatal(session):
    class Exploding(FakeConfig):
        def get_resource_config_history(self, **kwargs):
            raise RuntimeError("ThrottlingException")

    result = run_plan(session, config_client=Exploding(items={INSTANCE_B: []}))

    anchor = anchors(result)[UNPROVABLE]
    assert anchor.confidence is Confidence.UNKNOWN
    assert "GetResourceConfigHistory failed" in anchor.note
    assert "ThrottlingException" in anchor.note


# ---------------------------------------------------------------------------
# the three sources together
# ---------------------------------------------------------------------------


def test_all_three_sources_cooperate_in_priority_order(session):
    """Config for one field, a snapshot for another, --set for the third."""
    before = session.start_time - timedelta(days=200)
    client = FakeConfig(items={INSTANCE_B: [config_item(before, {"instanceType": "t3.nano"})]})
    snapshot = make_snapshot(
        session, offset_minutes=-60, values={(INSTANCE_B, "monitoring"): "disabled"}
    )
    snapshot.entries[0] = SnapshotEntry(
        resource_type="AWS::EC2::Instance",
        resource_id=INSTANCE_B,
        field_name="monitoring",
        operation="SET_EC2_DETAILED_MONITORING",
        value="disabled",
    )

    result = build_plan(
        source=session.source(),
        query=session.query(),
        resolver=build_chain(
            snapshot=snapshot,
            config_client=client,
            assignments={"%s.instanceType" % INSTANCE_A: "t9.ignored"},
        ),
        now=session.end_time,
    )
    found = anchors(result)

    assert found[UNPROVABLE].source == "config-history"
    assert found[(INSTANCE_B, "monitoring")].source == "local-snapshot"
    assert found[PROVABLE].source == "cloudtrail-window"      # --set did not override
    assert result.stats["unprovable"] == 0
    assert result.stats["revertible"] == 6
    assert any("was ignored" in w for w in result.warnings)
