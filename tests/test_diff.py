"""diff: drift detection, conflicts, unreadable resources, blame, plan-file validation."""

from __future__ import annotations

import json

import pytest

from conftest import (
    DB_INSTANCE,
    FUNCTION_ALIAS,
    INSTANCE_A,
    INSTANCE_B,
    FakeAws,
    WriteAttempted,
    live_world_after_session,
)
from rewind.pipeline.diff import Verdict, classify, diff_plan
from rewind.store.plan import PlanFileError
from rewind.domain import Capability, Confidence
from rewind.domain import Chain
from rewind.store.plan import load as load_plan, parse as parse_plan
from rewind.pipeline import plan as build_plan


def written_plan(session, tmp_path, **kwargs):
    """Generate a plan the way `rewind plan -o` does, and read it back."""
    result = build_plan(
        source=session.source(),
        query=session.query(),
        now=session.end_time,
        **kwargs
    )
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return load_plan(str(path))


def synthetic_chain(resource_id, resource_type, field, handler, event_name, before, after):
    """Build a Chain by hand, the way the planner would.

    One Chain type serves both writing and reading now, so a test fixture is the same object
    the pipeline produces - no parallel "as read back" class to keep in step.
    """
    from datetime import datetime, timezone

    from rewind.domain import Anchor, Change, FieldRef

    when = datetime(2026, 9, 22, 17, 25, tzinfo=timezone.utc)
    return Chain(
        field=FieldRef(resource_id=resource_id, path=(field,), resource_type=resource_type),
        handler=handler,
        event_source="%s.amazonaws.com" % handler.split("_")[1].lower(),
        event_name=event_name,
        changes=[
            Change(
                sequence=1,
                event_id="e-synthetic",
                event_time=when,
                event_name=event_name,
                identity="tester",
                before=before,
                after=after,
            )
        ],
        anchor=Anchor(value=before, confidence=Confidence.HIGH, source="test"),
        capability=Capability.AUTO,
    )


def verdicts(diff):
    return {(e.chain.resource_id, e.chain.field_name): e.verdict for e in diff.entries}


# -- the clean case ---------------------------------------------------------


def test_untouched_resources_are_revertible_or_unproven(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()

    diff = diff_plan(plan, world, now=session.end_time)

    assert verdicts(diff) == {
        (INSTANCE_A, "instanceType"): Verdict.REVERTIBLE,
        (INSTANCE_B, "instanceType"): Verdict.UNPROVEN,
        (INSTANCE_A, "monitoring"): Verdict.REVERTIBLE,
        (INSTANCE_B, "monitoring"): Verdict.UNPROVEN,
        (FUNCTION_ALIAS, "provisionedConcurrency"): Verdict.REVERTIBLE,
        (DB_INSTANCE, "multiAZ"): Verdict.REVERTIBLE,
    }
    assert diff.conflicts == []
    assert len(diff.actionable) == 4
    assert all(e.drift_free for e in diff.entries)


def test_drift_detection_works_even_with_an_unproven_anchor(session, tmp_path):
    """The promised property: conflict detection needs no history at all."""
    plan = written_plan(session, tmp_path)
    unprovable = next(
        c for c in plan.chains if c.resource_id == INSTANCE_B and c.field_name == "instanceType"
    )
    assert unprovable.confidence is Confidence.UNKNOWN
    assert unprovable.net_before is None

    # Untouched: the tool can still state that nothing else changed it.
    clean = diff_plan(plan, live_world_after_session(), now=session.end_time)
    entry = next(e for e in clean.entries if e.chain.chain_id == unprovable.chain_id)
    assert entry.verdict is Verdict.UNPROVEN
    assert entry.drift_free is True
    assert "nothing else has touched it" in entry.reason

    # Touched by somebody else: detected, despite the old value still being unknown.
    meddled = live_world_after_session()
    meddled.instance_types[INSTANCE_B] = "t3.2xlarge"
    dirty = diff_plan(plan, meddled, now=session.end_time)
    entry = next(e for e in dirty.entries if e.chain.chain_id == unprovable.chain_id)
    assert entry.verdict is Verdict.CONFLICT
    assert entry.live_value == "t3.2xlarge"
    assert entry.drift_free is False


def test_diff_makes_only_read_calls(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()

    diff_plan(plan, world, now=session.end_time)

    assert world.calls, "the diff must actually read something"
    for service, name, _ in world.calls:
        assert name.startswith(("describe_", "get_")), "%s:%s is not a read" % (service, name)


def test_the_write_guard_really_raises(session):
    """Guards the assertion above: a mutating call would fail loudly."""
    world = FakeAws()
    with pytest.raises(WriteAttempted):
        world.client("ec2").stop_instances(InstanceIds=[INSTANCE_A])


# -- conflicts --------------------------------------------------------------


def test_a_third_party_change_is_a_conflict(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    world.concurrency[FUNCTION_ALIAS] = 25  # somebody else raised it further

    diff = diff_plan(plan, world, now=session.end_time)

    conflicts = diff.conflicts
    assert [(c.chain.resource_id, c.live_value) for c in conflicts] == [(FUNCTION_ALIAS, "25")]
    assert "something outside this plan changed it" in conflicts[0].reason
    # The rest of the plan is unaffected.
    assert len(diff.actionable) == 3


def test_a_field_already_put_back_is_not_a_conflict(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    world.instance_types[INSTANCE_A] = "t3.micro"  # already reverted by hand

    diff = diff_plan(plan, world, now=session.end_time)

    entry = next(
        e for e in diff.entries
        if e.chain.resource_id == INSTANCE_A and e.chain.field_name == "instanceType"
    )
    assert entry.verdict is Verdict.ALREADY_REVERTED
    assert "already holds its pre-session value" in entry.reason
    assert diff.conflicts == []


def test_an_unreadable_resource_does_not_abort_the_diff(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    del world.multi_az[DB_INSTANCE]  # the database was deleted

    diff = diff_plan(plan, world, now=session.end_time)

    entry = next(e for e in diff.entries if e.chain.resource_id == DB_INSTANCE)
    assert entry.verdict is Verdict.UNREADABLE
    assert entry.live_value is None
    assert "DBInstanceNotFound" in entry.reason
    # Every other field was still checked.
    assert len(diff.entries) == 6
    assert len(diff.actionable) == 3


def test_an_unsupported_operation_in_a_plan_is_reported_not_crashed():
    chain = synthetic_chain(
        resource_id="some-bucket",
        resource_type="AWS::S3::Bucket",
        field="versioning",
        handler="SET_S3_VERSIONING",
        event_name="PutBucketVersioning",
        before="Suspended",
        after="Enabled",
    )
    plan = parse_plan(
        {
            "rewindPlanVersion": 2,
            "generatedAt": "2026-09-22T17:30:00Z",
            "query": {"identity": "x", "region": "us-west-1",
                      "startTime": "2026-09-22T17:20:00Z", "endTime": "2026-09-22T17:30:00Z"},
            "chains": [],
        }
    )
    plan.chains.append(chain)

    diff = diff_plan(plan, FakeAws(), now=None)

    assert diff.entries[0].verdict is Verdict.UNREADABLE
    assert "does not have handler" in diff.entries[0].reason


# -- RDS convergence --------------------------------------------------------


def test_a_pending_rds_modification_is_not_a_conflict(session, tmp_path):
    """RDS reports the old value while converging; that is not somebody else's change."""
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    world.multi_az[DB_INSTANCE] = False          # not applied yet
    world.rds_pending[DB_INSTANCE] = True        # but the change is in flight

    diff = diff_plan(plan, world, now=session.end_time)

    entry = next(e for e in diff.entries if e.chain.resource_id == DB_INSTANCE)
    assert entry.verdict is Verdict.REVERTIBLE
    assert entry.live_value == "true"            # the effective value
    assert entry.detail["appliedMultiAZ"] == "false"
    assert entry.detail["pendingMultiAZ"] == "true"
    assert "still being applied" in entry.detail["note"]


def test_lambda_with_no_config_reads_as_none_not_as_an_error(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    del world.concurrency[FUNCTION_ALIAS]        # the config was deleted

    diff = diff_plan(plan, world, now=session.end_time)

    entry = next(e for e in diff.entries if e.chain.resource_id == FUNCTION_ALIAS)
    # NONE is the pre-session value, so this reads as already reverted, not unreadable.
    assert entry.verdict is Verdict.ALREADY_REVERTED
    assert entry.live_value == "NONE"


# -- net no-op --------------------------------------------------------------


def test_a_net_no_op_chain_is_reported_as_already_at_original():
    chain = synthetic_chain(
        resource_id=INSTANCE_A,
        resource_type="AWS::EC2::Instance",
        field="instanceType",
        handler="SET_EC2_INSTANCE_TYPE",
        event_name="ModifyInstanceAttribute",
        before="t3.micro",
        after="t3.micro",
    )
    assert chain.net_no_op is True

    unchanged = classify(chain, "t3.micro", None)
    assert unchanged.verdict is Verdict.ALREADY_AT_ORIGINAL
    assert unchanged.drift_free is True

    moved = classify(chain, "t3.large", None)
    assert moved.verdict is Verdict.CONFLICT


# -- blame ------------------------------------------------------------------


def test_blame_names_the_identity_behind_a_conflict(session, tmp_path):
    """A conflict raises "who?"; --blame answers it from CloudTrail."""
    from datetime import timedelta

    from conftest import as_wire_record
    from rewind.trail import StaticEventSource
    from rewind.trail import from_lookup_record

    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    world.instance_types[INSTANCE_A] = "t3.2xlarge"

    later = session.end_time + timedelta(minutes=30)
    culprit = from_lookup_record(
        as_wire_record(
            {
                "EventId": "x0000001-0000-4000-8000-00000000x001",
                "EventName": "ModifyInstanceAttribute",
                "CloudTrailEvent": {
                    "eventID": "x0000001-0000-4000-8000-00000000x001",
                    "eventTime": later.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "eventName": "ModifyInstanceAttribute",
                    "eventSource": "ec2.amazonaws.com",
                    "awsRegion": "us-west-1",
                    "userIdentity": {
                        "arn": "arn:aws:sts::111122223333:assumed-role/CiDeployRole/ci-deployer"
                    },
                    "requestParameters": {
                        "instanceId": INSTANCE_A,
                        "instanceType": {"value": "t3.2xlarge"},
                    },
                    "responseElements": {"_return": True},
                },
            }
        )
    )
    source = StaticEventSource(list(session.events) + [culprit])

    diff = diff_plan(plan, world, source=source, now=later + timedelta(minutes=1))

    assert diff.blame_attempted is True
    conflict = diff.conflicts[0]
    assert len(conflict.blame) == 1
    assert "ci-deployer" in conflict.blame[0]["identity"]
    assert conflict.blame[0]["setTo"] == "t3.2xlarge"
    assert conflict.blame[0]["eventId"] == culprit.event_id


def test_blame_is_not_queried_when_there_is_nothing_to_blame(session, tmp_path):
    """A clean diff must not pay for a CloudTrail query it does not need."""
    class RefusingSource:
        def window_events(self, start, end):  # pragma: no cover
            raise AssertionError("no CloudTrail query should happen without a conflict")

        def named_events(self, start, end, event_names):  # pragma: no cover
            raise AssertionError("no CloudTrail query should happen without a conflict")

    plan = written_plan(session, tmp_path)

    diff = diff_plan(plan, live_world_after_session(), source=RefusingSource(),
                     now=session.end_time)

    assert diff.conflicts == []
    assert diff.blame_attempted is False


# -- plan file validation ---------------------------------------------------


def test_a_missing_plan_file_is_a_clear_error(tmp_path):
    with pytest.raises(PlanFileError) as raised:
        load_plan(str(tmp_path / "nope.json"))
    assert "no such plan file" in str(raised.value)


def test_invalid_json_is_a_clear_error(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{not json")
    with pytest.raises(PlanFileError) as raised:
        load_plan(str(path))
    assert "not valid JSON" in str(raised.value)


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, "no rewindPlanVersion"),
        ({"rewindPlanVersion": 99}, "not supported by this build"),
        ({"rewindPlanVersion": 2}, "missing 'query'"),
        ({"rewindPlanVersion": 2, "query": {}, "generatedAt": "2026-09-22T17:30:00Z"},
         "chains must be an array"),
    ],
)
def test_plan_validation_messages_say_what_is_wrong(body, expected):
    with pytest.raises(PlanFileError) as raised:
        parse_plan(body)
    assert expected in str(raised.value)


def test_a_chain_missing_a_required_key_is_rejected():
    body = {
        "rewindPlanVersion": 2,
        "generatedAt": "2026-09-22T17:30:00Z",
        "query": {"identity": "x", "region": "us-west-1",
                  "startTime": "2026-09-22T17:20:00Z", "endTime": "2026-09-22T17:30:00Z"},
        "chains": [{"chainId": "chn-x"}],
    }
    with pytest.raises(PlanFileError) as raised:
        parse_plan(body)
    assert "chain 0 is missing" in str(raised.value)


def test_the_plan_round_trips_through_the_file(session, tmp_path):
    generated = build_plan(source=session.source(), query=session.query(), now=session.end_time)
    loaded = written_plan(session, tmp_path)

    assert loaded.identity == generated.query.identity
    assert loaded.region == generated.query.region
    assert [c.chain_id for c in loaded.chains] == [c.chain_id for c in generated.chains]
    assert [c.net_before for c in loaded.chains] == [c.net_before for c in generated.chains]
    assert [c.net_after for c in loaded.chains] == [c.net_after for c in generated.chains]
    assert [c.confidence for c in loaded.chains] == [c.confidence for c in generated.chains]
    assert [c.executable for c in loaded.chains] == [c.executable for c in generated.chains]


def test_an_unexplained_conflict_says_so_rather_than_repeating_the_hint(session, tmp_path):
    """With --blame already attempted, "run with --blame" would be misleading."""
    from rewind.report import render_diff

    plan = written_plan(session, tmp_path)
    world = live_world_after_session()
    world.concurrency[FUNCTION_ALIAS] = 25   # no CloudTrail event explains this

    # The fixture contains no event that touches this field after the plan.
    diff = diff_plan(plan, world, source=session.source(), now=session.end_time)
    text = render_diff(diff)

    assert diff.blame_attempted is True
    assert diff.conflicts[0].blame == []
    assert "run with --blame" not in text
    assert "no CloudTrail event since the plan explains this change" in text


def test_a_plan_round_trips_through_one_chain_type(session, tmp_path):
    """Write a plan, read it back, and get the same Chain objects.

    There used to be a second class for the read-back form, which meant adding a field to a
    chain required editing two places - and the two drifted. This asserts the merge holds.
    """
    from rewind.store.plan import dump, parse

    generated = build_plan(source=session.source(), query=session.query(), now=session.end_time)
    restored = parse(dump(generated))

    assert type(restored.chains[0]) is type(generated.chains[0])
    for before, after in zip(generated.chains, restored.chains):
        # The identity, the values, and everything derived from them survive the trip.
        assert (before.chain_id, before.field_name, before.resource_id) == (
            after.chain_id, after.field_name, after.resource_id
        )
        assert (before.net_before, before.net_after) == (after.net_before, after.net_after)
        assert before.confidence is after.confidence
        assert before.capability is after.capability
        assert before.executable == after.executable
        assert before.change_count == after.change_count
        assert before.last_change_at == after.last_change_at
        assert before.anchor.source == after.anchor.source
        assert before.anchor.evidence_event_ids == after.anchor.evidence_event_ids
        assert [c.to_dict() for c in before.changes] == [c.to_dict() for c in after.changes]
    # And the whole document is byte-identical the second time round.
    assert dump(restored) == dump(generated)


def test_derived_fields_are_recomputed_not_trusted(session, tmp_path):
    """A hand-edited plan cannot make a chain disagree with its own changes."""
    from rewind.store.plan import dump, parse

    body = dump(build_plan(source=session.source(), query=session.query(), now=session.end_time))
    chain = body["chains"][0]
    chain["netAfter"] = "t9.tampered"
    chain["changeCount"] = 99

    restored = parse(body)

    # netAfter comes from the last change, changeCount from the list length.
    assert restored.chains[0].net_after == chain["changes"][-1]["after"]
    assert restored.chains[0].change_count == len(chain["changes"])


def test_a_chain_with_no_changes_is_rejected():
    """A chain describes at least one change; an empty one is a malformed document."""
    from rewind.store.plan import parse

    body = {
        "rewindPlanVersion": 2,
        "generatedAt": "2026-09-22T17:30:00Z",
        "query": {"identity": "x", "region": "us-west-1",
                  "startTime": "2026-09-22T17:20:00Z", "endTime": "2026-09-22T17:30:00Z"},
        "chains": [{"chainId": "chn-x", "resourceId": "r", "field": "f",
                    "confidence": "HIGH", "changes": []}],
    }
    with pytest.raises(PlanFileError) as raised:
        parse(body)
    assert "has no changes" in str(raised.value)


# -- rendering a diff that contains an unvalued change ----------------------


def test_a_diff_containing_a_valueless_change_still_renders(mixed, tmp_path):
    """Regression: found by running the real CLI against real CloudTrail, not by a test.

    A mutating call whose new value is not in ``requestParameters`` at all - StartInstances,
    a Delete - is reported at DISCOVERED with ``net_after`` of None. ``render_diff`` passed
    that column straight into the table while every neighbouring column went through
    ``cell()``, so the table crashed with a bare "NoneType has no len()" naming neither the
    column nor the caller. The JSON output was unaffected, which is why no fixture test had
    tripped over it.
    """
    from rewind.report import render_diff

    plan = written_plan(mixed, tmp_path)
    valueless = [c for c in plan.chains if c.net_after is None]
    assert valueless, "the mixed fixture must contain a change with no value in parameters"

    text = render_diff(diff_plan(plan, live_world_after_session(), now=mixed.end_time))

    assert "?" in text
    for chain in valueless:
        assert chain.field_name in text


def test_the_table_names_the_column_when_a_value_is_not_a_string():
    """The guard that turns the next occurrence of the above into a diagnosable error."""
    from rewind.report.table import table

    with pytest.raises(TypeError) as caught:
        table([["ok", None]], ["FIRST", "SECOND"])

    assert "'SECOND'" in str(caught.value)
    assert "cell()" in str(caught.value)


# -- what a diff is allowed to claim ----------------------------------------


def unreadable_world(error_code):
    """A fake AWS client whose every read fails the way a real one does."""
    from botocore.exceptions import ClientError

    class Failing:
        calls: list = []

        def client(self, service):
            return self

        def __getattr__(self, name):
            def call(**kwargs):
                raise ClientError(
                    {"Error": {"Code": error_code, "Message": "Request has expired."}},
                    name,
                )

            return call

    return Failing()


def test_no_drift_is_never_claimed_when_nothing_could_be_read(session, tmp_path):
    """Seen for real: expired credentials, every read failed, table said "drift: none".

    Absence of CONFLICT is not absence of drift when no field was ever compared. The JSON
    already said ``driftFree: false``; only the human-readable line was lying.
    """
    from rewind.report import render_diff

    plan = written_plan(session, tmp_path)
    diff = diff_plan(plan, unreadable_world("RequestExpired"), now=session.end_time)

    assert diff.compared == [], "this fixture is only meaningful if nothing was read"
    assert diff.conflicts == []
    assert diff.to_dict()["driftFree"] is False

    text = render_diff(diff)
    assert "drift      : UNKNOWN" in text
    assert "none - " not in text


def test_a_credential_failure_is_called_out_as_not_being_about_the_resource(session, tmp_path):
    """One bad resource is a fact about it; expired credentials invalidate the whole diff."""
    from rewind.report import render_diff

    plan = written_plan(session, tmp_path)
    diff = diff_plan(plan, unreadable_world("ExpiredToken"), now=session.end_time)

    assert len(diff.credential_failures) == len(
        [e for e in diff.entries if e.verdict is Verdict.UNREADABLE]
    )
    text = render_diff(diff)
    assert "CREDENTIALS:" in text
    assert "proves nothing about drift" in text


def test_a_resource_specific_failure_is_not_blamed_on_credentials(session, tmp_path):
    """The distinction has to cut both ways or it is just noise."""
    from rewind.report import render_diff

    plan = written_plan(session, tmp_path)
    diff = diff_plan(plan, unreadable_world("InvalidInstanceID.NotFound"), now=session.end_time)

    assert [e for e in diff.entries if e.verdict is Verdict.UNREADABLE]
    assert diff.credential_failures == []
    assert "CREDENTIALS:" not in render_diff(diff)


def test_partial_readability_reports_how_much_was_actually_compared(session, tmp_path):
    """"No drift" is allowed, but it has to say what it covered."""
    from rewind.report import render_diff

    plan = written_plan(session, tmp_path)
    diff = diff_plan(plan, live_world_after_session(), now=session.end_time)

    assert diff.compared, "the standard world reads most fields"
    text = render_diff(diff)
    assert "drift      : none - %d of %d field(s) compared" % (
        len(diff.compared),
        len(diff.entries),
    ) in text


def test_the_next_step_it_suggests_is_a_command_that_exists(session, tmp_path):
    """The footer advertised `revert` as "(not implemented yet)" long after it shipped.

    A stale instruction is worse than none: it tells an operator the tool cannot do the very
    thing it is about to offer.
    """
    from rewind.report import render_diff

    text = render_diff(diff_plan(written_plan(session, tmp_path), live_world_after_session(),
                                 now=session.end_time))

    assert "not implemented" not in text
    assert "for a dry run" in text and "--confirm" in text
