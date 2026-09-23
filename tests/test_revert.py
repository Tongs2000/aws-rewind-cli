"""revert: the dry-run gate, ordering, the fresh pre-check, verification, idempotency."""

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
from rewind.store.plan import load as load_plan
from rewind.pipeline import plan as build_plan
from rewind.pipeline.revert import Outcome, Reverter, revert_order


def written_plan(session, tmp_path):
    result = build_plan(source=session.source(), query=session.query(), now=session.end_time)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return load_plan(str(path))


def revertible_world(**overrides):
    """Live state as the session left it, with writes enabled."""
    world = live_world_after_session()
    world.allow_writes = True
    for name, value in overrides.items():
        if isinstance(value, dict):
            getattr(world, name).update(value)
        else:
            setattr(world, name, value)
    return world


def outcomes(run):
    return {(r.chain.resource_id, r.chain.field_name): r.outcome for r in run.results}


# -- the dry-run gate -------------------------------------------------------


def test_dry_run_is_the_default_and_calls_nothing(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = live_world_after_session()  # allow_writes stays False: any write raises

    run = Reverter(clients=world, dry_run=True).run(plan, now=session.end_time)

    assert run.dry_run is True
    assert world.writes == []
    assert [n for n in world.api_names() if not n.split(":")[1].startswith(("describe_", "get_"))] == []
    # The four revertible fields report exactly what they would call.
    dry = [r for r in run.results if r.outcome is Outcome.DRY_RUN]
    assert len(dry) == 4
    assert all(r.planned_calls for r in dry)
    assert all("pass --confirm" in r.reason for r in dry)
    # Nothing in the account moved.
    assert world.instance_types[INSTANCE_A] == "t3.small"
    assert world.concurrency[FUNCTION_ALIAS] == 5


def test_the_write_guard_really_raises():
    """Guards the assertion above."""
    world = FakeAws()
    with pytest.raises(WriteAttempted):
        world.client("ec2").modify_instance_attribute(
            InstanceId=INSTANCE_A, InstanceType={"Value": "t3.micro"}
        )


# -- ordering ---------------------------------------------------------------


def test_newest_change_is_reverted_first(session, tmp_path):
    plan = written_plan(session, tmp_path)

    ordered = revert_order(plan.chains)

    times = [c.last_change_at for c in ordered]
    assert times == sorted(times, reverse=True)
    # The RDS change was last in the session, so it is undone first.
    assert ordered[0].resource_id == DB_INSTANCE
    # The two EC2 resizes were first, so they are undone last.
    assert ordered[-1].field_name == "instanceType"


def test_ordering_is_deterministic_when_changes_share_a_timestamp(session, tmp_path):
    """The two monitoring chains come from one event, so their times are identical."""
    plan = written_plan(session, tmp_path)
    monitoring = [c for c in plan.chains if c.field_name == "monitoring"]
    assert len({c.last_change_at for c in monitoring}) == 1

    first = [c.chain_id for c in revert_order(plan.chains)]
    second = [c.chain_id for c in revert_order(list(reversed(plan.chains)))]

    assert first == second


def test_only_narrows_the_run(session, tmp_path):
    plan = written_plan(session, tmp_path)
    target = next(c for c in plan.chains if c.resource_id == FUNCTION_ALIAS)
    world = revertible_world()

    run = Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=session.end_time
    )

    assert [r.chain.chain_id for r in run.results] == [target.chain_id]
    assert world.concurrency == {}                       # reverted to NONE
    assert world.instance_types[INSTANCE_A] == "t3.small"  # untouched


# -- applying ---------------------------------------------------------------


def test_a_confirmed_revert_restores_every_revertible_field(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world()

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    assert outcomes(run) == {
        (DB_INSTANCE, "multiAZ"): Outcome.REVERTED,
        (FUNCTION_ALIAS, "provisionedConcurrency"): Outcome.REVERTED,
        (INSTANCE_A, "monitoring"): Outcome.REVERTED,
        (INSTANCE_B, "monitoring"): Outcome.SKIPPED,       # unproven old value
        (INSTANCE_A, "instanceType"): Outcome.REVERTED,
        (INSTANCE_B, "instanceType"): Outcome.SKIPPED,     # unproven old value
    }
    # Live state is back where the session found it.
    assert world.instance_types[INSTANCE_A] == "t3.micro"
    assert world.monitoring[INSTANCE_A] == "disabled"
    assert world.concurrency == {}
    assert world.multi_az[DB_INSTANCE] is False
    # The unprovable instance was not touched at all.
    assert world.instance_types[INSTANCE_B] == "t3.small"
    assert world.monitoring[INSTANCE_B] == "enabled"


def test_a_resize_stops_modifies_and_restarts_a_running_instance(session, tmp_path):
    plan = written_plan(session, tmp_path)
    target = next(
        c for c in plan.chains
        if c.resource_id == INSTANCE_A and c.field_name == "instanceType"
    )
    world = revertible_world()
    assert world.power[INSTANCE_A] == "running"

    run = Reverter(clients=world, dry_run=False, wait=True).run(
        plan, only=[target.chain_id], now=session.end_time
    )

    assert world.write_api_names() == [
        "stop_instances", "modify_instance_attribute", "start_instances"
    ]
    assert world.waiters == ["instance_stopped", "instance_running"]
    assert world.power[INSTANCE_A] == "running"        # left as it was found
    assert world.instance_types[INSTANCE_A] == "t3.micro"
    assert run.results[0].outcome is Outcome.REVERTED


def test_a_stopped_instance_is_left_stopped(session, tmp_path):
    """The revert restores the field it owns and preserves the power state it finds."""
    plan = written_plan(session, tmp_path)
    target = next(
        c for c in plan.chains
        if c.resource_id == INSTANCE_A and c.field_name == "instanceType"
    )
    world = revertible_world(power={INSTANCE_A: "stopped"})

    Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=session.end_time
    )

    assert world.write_api_names() == ["modify_instance_attribute"]
    assert world.power[INSTANCE_A] == "stopped"
    assert world.instance_types[INSTANCE_A] == "t3.micro"


def test_no_wait_skips_the_waiters(session, tmp_path):
    plan = written_plan(session, tmp_path)
    target = next(
        c for c in plan.chains
        if c.resource_id == INSTANCE_A and c.field_name == "instanceType"
    )
    world = revertible_world()

    Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=session.end_time
    )

    assert world.waiters == []
    assert world.write_api_names() == [
        "stop_instances", "modify_instance_attribute", "start_instances"
    ]


def test_the_planned_calls_match_what_is_actually_issued(session, tmp_path):
    """The plan is what a human approved, so apply_revert must not diverge from it."""
    plan = written_plan(session, tmp_path)
    for chain in plan.chains:
        if not chain.executable:
            continue
        world = revertible_world()
        run = Reverter(clients=world, dry_run=False, wait=False).run(
            plan, only=[chain.chain_id], now=session.end_time
        )
        planned = [s["api"] for s in chain.revert["steps"]]
        issued = [c["api"] for c in run.results[0].calls]
        # Conditional steps may be skipped, but nothing unadvertised may be called.
        assert set(issued) <= set(planned), chain.chain_id
        assert issued, chain.chain_id


# -- the fresh pre-check ----------------------------------------------------


def test_a_conflict_appearing_after_the_plan_stops_that_field(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world(instance_types={INSTANCE_A: "t3.2xlarge"})

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    conflicted = next(
        r for r in run.results
        if r.chain.resource_id == INSTANCE_A and r.chain.field_name == "instanceType"
    )
    assert conflicted.outcome is Outcome.SKIPPED
    assert conflicted.precheck_verdict.value == "CONFLICT"
    assert "something outside this plan changed it" in conflicted.reason
    # The third party's value survives, and the instance was never stopped.
    assert world.instance_types[INSTANCE_A] == "t3.2xlarge"
    assert "stop_instances" not in world.write_api_names()
    # Everything else still reverted.
    assert world.concurrency == {}


def test_the_precheck_is_per_field_not_once_up_front(session, tmp_path):
    """A field that drifts mid-run is still caught.

    The Lambda chain is reverted before the EC2 resizes. A hook on the Lambda write moves
    instance A out from under the plan, mimicking a third party acting during the run.
    """
    plan = written_plan(session, tmp_path)
    world = revertible_world()

    original = world._write_delete_provisioned_concurrency_config

    def drift_mid_run(kwargs):
        original(kwargs)
        world.instance_types[INSTANCE_A] = "t3.2xlarge"

    world._write_delete_provisioned_concurrency_config = drift_mid_run

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    lambda_result = next(r for r in run.results if r.chain.resource_id == FUNCTION_ALIAS)
    resize_result = next(
        r for r in run.results
        if r.chain.resource_id == INSTANCE_A and r.chain.field_name == "instanceType"
    )
    assert lambda_result.outcome is Outcome.REVERTED
    assert resize_result.outcome is Outcome.SKIPPED
    assert resize_result.precheck_verdict.value == "CONFLICT"
    assert world.instance_types[INSTANCE_A] == "t3.2xlarge"


def test_an_unreadable_resource_is_skipped_not_guessed_at(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world()
    del world.multi_az[DB_INSTANCE]

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    entry = next(r for r in run.results if r.chain.resource_id == DB_INSTANCE)
    assert entry.outcome is Outcome.SKIPPED
    assert "DBInstanceNotFound" in entry.reason
    assert "modify_db_instance" not in world.write_api_names()
    # The rest of the run continued.
    assert world.concurrency == {}


def test_unproven_chains_are_never_touched(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world()

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    for result in run.results:
        if result.chain.confidence.value == "UNKNOWN":
            assert result.outcome is Outcome.SKIPPED
            assert result.calls == []
            assert "not proven" in result.reason


# -- failures ---------------------------------------------------------------


def test_a_failing_aws_call_is_reported_and_does_not_stop_the_run(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world()
    world.fail_write = "delete_provisioned_concurrency_config"

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    failed = next(r for r in run.results if r.chain.resource_id == FUNCTION_ALIAS)
    assert failed.outcome is Outcome.FAILED
    assert "ServiceException" in failed.reason
    assert run.failed == [failed]
    # The other fields were still reverted.
    assert world.instance_types[INSTANCE_A] == "t3.micro"
    assert world.concurrency == {FUNCTION_ALIAS: 5}  # unchanged by the failed call


def test_a_revert_that_does_not_take_effect_is_reported_as_failed(session, tmp_path):
    """Verification is a real check, not a formality."""
    plan = written_plan(session, tmp_path)
    world = revertible_world()

    # A write that silently does nothing, as a mis-scoped IAM policy might look.
    world._write_unmonitor_instances = lambda kwargs: None

    target = next(
        c for c in plan.chains
        if c.resource_id == INSTANCE_A and c.field_name == "monitoring"
    )
    run = Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=session.end_time
    )

    assert run.results[0].outcome is Outcome.FAILED
    assert run.results[0].verification == "MISMATCH"
    assert "does not read 'disabled'" in run.results[0].reason


# -- idempotency and asynchronous changes -----------------------------------


def test_re_running_a_completed_revert_changes_nothing(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world()
    reverter = Reverter(clients=world, dry_run=False, wait=False)

    reverter.run(plan, now=session.end_time)
    writes_after_first = list(world.write_api_names())
    second = reverter.run(plan, now=session.end_time)

    assert world.write_api_names() == writes_after_first  # no further AWS writes
    already = [r for r in second.results if r.outcome is Outcome.ALREADY_REVERTED]
    assert len(already) == 4
    assert all("already holds its pre-session value" in r.reason for r in already)


def test_an_async_rds_revert_reports_submitted_then_polls_to_reverted(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world(rds_is_async=True)
    target = next(c for c in plan.chains if c.resource_id == DB_INSTANCE)
    reverter = Reverter(clients=world, dry_run=False, wait=False)

    first = reverter.run(plan, only=[target.chain_id], now=session.end_time)

    assert first.results[0].outcome is Outcome.SUBMITTED
    assert first.results[0].verification == "PENDING"
    assert "re-run to poll it" in first.results[0].reason
    assert world.rds_pending == {DB_INSTANCE: False}
    assert world.multi_az[DB_INSTANCE] is True          # not applied yet

    # Polling before it settles must not re-issue the call.
    writes = list(world.write_api_names())
    second = reverter.run(plan, only=[target.chain_id], now=session.end_time)
    assert second.results[0].outcome is Outcome.SUBMITTED
    assert world.write_api_names() == writes

    # Once RDS converges, the same command reports success.
    world.settle_rds(DB_INSTANCE)
    third = reverter.run(plan, only=[target.chain_id], now=session.end_time)
    assert third.results[0].outcome is Outcome.ALREADY_REVERTED
    assert third.results[0].verification == "VERIFIED"
    assert world.write_api_names() == writes


def test_a_net_no_op_chain_is_skipped(session, tmp_path):
    """A field the session changed and changed back has nothing to revert."""
    from conftest import as_wire_record
    from rewind.trail import StaticEventSource
    from rewind.trail import from_lookup_record

    restore = from_lookup_record(
        as_wire_record(
            {
                "EventId": "s0000009",
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
    )
    result = build_plan(
        source=StaticEventSource(list(session.events) + [restore]),
        query=session.query(),
        now=session.end_time,
    )
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    plan = load_plan(str(path))

    world = revertible_world(instance_types={INSTANCE_A: "t3.micro"})
    target = next(
        c for c in plan.chains
        if c.resource_id == INSTANCE_A and c.field_name == "instanceType"
    )
    run = Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=session.end_time
    )

    assert run.results[0].outcome is Outcome.SKIPPED
    assert run.results[0].precheck_verdict.value == "ALREADY_AT_ORIGINAL"
    assert "nothing to revert" in run.results[0].reason
    assert world.writes == []


def test_unfinished_collects_everything_needing_attention(session, tmp_path):
    plan = written_plan(session, tmp_path)
    world = revertible_world(instance_types={INSTANCE_A: "t3.2xlarge"})

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=session.end_time)

    kinds = {r.outcome for r in run.unfinished}
    assert kinds == {Outcome.SKIPPED}
    assert len(run.unfinished) == 3  # one conflict plus the two unprovable chains


# -- what counts as work left over ------------------------------------------


def test_out_of_scope_is_separated_from_unfinished(mixed, tmp_path):
    """The distinction, at the level it is computed."""
    from rewind.pipeline import Reverter
    from rewind.pipeline import plan as build_plan

    plan = build_plan(source=mixed.source(), query=mixed.query(), now=mixed.end_time)
    valueless = [c for c in plan.chains if not c.values_known]
    assert valueless, "the mixed fixture must contain a change with no recorded value"

    run = Reverter(clients=live_world_after_session(), dry_run=False).run(
        plan, now=mixed.end_time
    )

    scoped_out = {r.chain.chain_id for r in run.out_of_scope}
    assert scoped_out == {c.chain_id for c in valueless}
    assert not scoped_out & {r.chain.chain_id for r in run.unfinished}
    assert all(r.chain.values_known for r in run.unfinished)

    body = run.to_dict()
    assert body["outOfScope"] == len(valueless)
    assert body["unfinished"] == len(run.unfinished)


def test_the_report_never_calls_an_unrevertable_change_a_to_do(mixed, tmp_path):
    from rewind.pipeline import Reverter
    from rewind.pipeline import plan as build_plan
    from rewind.report import render_revert

    plan = build_plan(source=mixed.source(), query=mixed.query(), now=mixed.end_time)
    run = Reverter(clients=live_world_after_session(), dry_run=False).run(
        plan, now=mixed.end_time
    )

    text = render_revert(run)

    assert "nothing to restore" in text
    for result in run.out_of_scope:
        assert result.chain.field_name in text
    if not run.unfinished:
        assert "still need attention" not in text


def test_a_run_whose_only_leftovers_are_valueless_has_no_unfinished_work():
    """The exit-code case, isolated: nothing actionable left, so nothing to report."""
    from datetime import datetime, timedelta, timezone

    from rewind.domain import Query
    from rewind.pipeline import Reverter
    from rewind.pipeline import plan as build_plan
    from rewind.trail import StaticEventSource, from_lookup_record

    start = datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc)
    stop = from_lookup_record(
        {
            "EventId": "e1",
            "EventName": "StopInstances",
            "CloudTrailEvent": {
                "eventID": "e1",
                "eventTime": "2026-09-23T17:10:00Z",
                "eventName": "StopInstances",
                "eventSource": "ec2.amazonaws.com",
                "awsRegion": "us-west-1",
                "readOnly": False,
                "userIdentity": {"arn": "arn:aws:sts::1:assumed-role/R/agent"},
                "requestParameters": {"instancesSet": {"items": [{"instanceId": "i-0abc1234"}]}},
                "responseElements": None,
            },
        }
    )

    plan = build_plan(
        source=StaticEventSource([stop]),
        query=Query(
            identity="agent",
            start_time=start,
            end_time=start + timedelta(hours=1),
            region="us-west-1",
        ),
        now=start + timedelta(hours=1),
    )
    assert plan.chains and not any(c.values_known for c in plan.chains)

    run = Reverter(clients=live_world_after_session(), dry_run=False, wait=False).run(
        plan, now=start + timedelta(hours=1)
    )

    assert run.unfinished == [], "nothing here is actionable, so nothing is outstanding"
    assert len(run.out_of_scope) == len(plan.chains)
    assert run.to_dict()["unfinished"] == 0
