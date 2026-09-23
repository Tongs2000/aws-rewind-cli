"""The parameterised EC2 instance attribute plugins.

One class covers six attributes through one event name. The two things that vary per
attribute - whether EC2 demands a stopped instance, and whether AWS Config records it - are
what these tests pin down, because getting either wrong is silent and expensive: a needless
production restart, or a config-history lookup for something that is never there.
"""

from __future__ import annotations

import json

import pytest

from conftest import live_world_after_session
from rewind.trail import StaticEventSource
from rewind.domain import parse_time
from rewind.domain import Capability, Confidence, Query
from rewind.handlers import PLUGINS, get_operation
from rewind.handlers.aws.ec2_instance_attribute import (
    ATTRIBUTES,
    Ec2InstanceAttributeOperation,
    api_name,
)
from rewind.store.plan import load as load_plan
from rewind.pipeline import plan as build_plan
from rewind.pipeline.revert import Outcome, Reverter

INSTANCE = "i-0aaa000000000000a"

NO_RESTART = "disableApiTermination"      # EC2 changes this on a running instance
NEEDS_RESTART = "ebsOptimized"            # EC2 rejects this unless the instance is stopped


def event(event_id, when, request_parameters, instance=INSTANCE):
    from conftest import as_wire_record
    from rewind.trail import from_lookup_record

    return from_lookup_record(
        as_wire_record(
            {
                "EventId": event_id,
                "EventName": "ModifyInstanceAttribute",
                "Resources": [
                    {"ResourceType": "AWS::EC2::Instance", "ResourceName": instance}
                ],
                "CloudTrailEvent": {
                    "eventID": event_id,
                    "eventTime": when,
                    "eventName": "ModifyInstanceAttribute",
                    "eventSource": "ec2.amazonaws.com",
                    "awsRegion": "us-west-1",
                    "readOnly": False,
                    "userIdentity": {
                        "arn": "arn:aws:sts::111122223333:assumed-role/R/perf-agent"
                    },
                    "requestParameters": request_parameters,
                    "responseElements": {"_return": True},
                },
            }
        )
    )


def attribute_plan(events):
    return build_plan(
        source=StaticEventSource(events),
        query=Query(
            identity="perf-agent",
            start_time=parse_time("2026-09-23T10:50:00Z"),
            end_time=parse_time("2026-09-23T11:10:00Z"),
            region="us-west-1",
        ),
        now=parse_time("2026-09-23T12:00:00Z"),
    )


def changed(attribute, before, after):
    """A plan where one attribute went from `before` to `after`."""
    return attribute_plan(
        [
            event("e1", "2026-09-20T09:00:00Z", {"instanceId": INSTANCE, attribute: {"value": before}}),
            event("s1", "2026-09-23T11:00:00Z", {"instanceId": INSTANCE, attribute: {"value": after}}),
        ]
    )


def written(result, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return load_plan(str(path))


def revert_world():
    world = live_world_after_session()
    world.allow_writes = True
    return world


# -- registration and naming ------------------------------------------------


def test_every_declared_attribute_is_registered():
    registered = {p.field_name for p in PLUGINS if isinstance(p, Ec2InstanceAttributeOperation)}
    assert registered == {a.name for a in ATTRIBUTES}


def test_operation_names_are_derived_not_hand_written():
    assert get_operation("SET_EC2_DISABLE_API_TERMINATION") is not None
    assert get_operation("SET_EC2_EBS_OPTIMIZED") is not None
    assert get_operation("SET_EC2_INSTANCE_INITIATED_SHUTDOWN_BEHAVIOR") is not None


@pytest.mark.parametrize(
    "cloudtrail_name,boto3_name",
    [
        ("disableApiTermination", "DisableApiTermination"),
        ("ebsOptimized", "EbsOptimized"),
        ("sourceDestCheck", "SourceDestCheck"),
        ("enaSupport", "EnaSupport"),
        ("instanceInitiatedShutdownBehavior", "InstanceInitiatedShutdownBehavior"),
    ],
)
def test_the_whole_cloudtrail_to_boto3_mapping_is_one_character(cloudtrail_name, boto3_name):
    assert api_name(cloudtrail_name) == boto3_name


def test_instance_type_is_one_of_these_attributes():
    """It used to have its own module; the reasons given did not survive checking.

    Same event, same request shape, same ``DescribeInstanceAttribute`` response shape, and
    the same data-loss warning every ``requires_stopped`` attribute gets. The operation name
    is unchanged, so plans written before the merge still load.
    """
    attribute = next(a for a in ATTRIBUTES if a.name == "instanceType")
    assert (attribute.boolean, attribute.requires_stopped, attribute.in_config) == (
        False,
        True,
        True,
    )
    assert get_operation("SET_EC2_INSTANCE_TYPE") is not None


def test_no_plugin_asks_the_lookback_for_events_it_cannot_use():
    """A name in ``relevant_event_names`` costs one LookupEvents call on every plan.

    The old instanceType plugin listed Start/Stop/TerminateInstances and then ignored every
    one of them in ``anchor_from_event``, so each plan paid for three queries whose results
    no resolver could consume.
    """
    from rewind.handlers import PLUGINS

    for plugin in PLUGINS:
        unusable = plugin.relevant_event_names - plugin.mutating_event_names
        assert unusable <= plugin.creation_event_names(), (
            "%s asks for %s but can anchor from neither a mutation nor a creation"
            % (plugin.name, sorted(unusable - plugin.creation_event_names()))
        )


# -- discovery and chaining -------------------------------------------------


def test_a_boolean_attribute_is_chained_and_reaches_auto():
    chain = next(c for c in changed(NO_RESTART, False, True).chains)

    assert chain.field_name == NO_RESTART
    assert chain.handler == "SET_EC2_DISABLE_API_TERMINATION"
    assert (chain.net_before, chain.net_after) == ("false", "true")
    assert chain.confidence is Confidence.HIGH
    assert chain.capability is Capability.AUTO


def test_a_string_attribute_is_chained_too():
    chain = next(c for c in changed("instanceInitiatedShutdownBehavior", "stop", "terminate").chains)

    assert (chain.net_before, chain.net_after) == ("stop", "terminate")
    assert chain.capability is Capability.AUTO


def test_the_plugin_claims_the_wrapped_path_so_nothing_is_double_reported():
    fields = [c.field_name for c in changed(NO_RESTART, False, True).chains]

    assert fields == [NO_RESTART]
    assert "%s.value" % NO_RESTART not in fields


def test_a_launch_time_value_anchors_from_the_creation_event():
    """RunInstances sets some of these unwrapped, so creation-event anchoring applies."""
    from conftest import as_wire_record
    from rewind.trail import from_lookup_record

    launch = from_lookup_record(
        as_wire_record(
            {
                "EventId": "r1",
                "EventName": "RunInstances",
                "Resources": [],
                "CloudTrailEvent": {
                    "eventID": "r1",
                    "eventTime": "2026-09-15T09:00:00Z",
                    "eventName": "RunInstances",
                    "eventSource": "ec2.amazonaws.com",
                    "awsRegion": "us-west-1",
                    "readOnly": False,
                    "userIdentity": {"arn": "arn:aws:sts::111122223333:assumed-role/R/boot"},
                    "requestParameters": {"ebsOptimized": False, "instanceType": "t3.micro"},
                    "responseElements": {
                        "instancesSet": {"items": [{"instanceId": INSTANCE}]}
                    },
                },
            }
        )
    )
    result = attribute_plan(
        [launch, event("s1", "2026-09-23T11:00:00Z", {"instanceId": INSTANCE, NEEDS_RESTART: {"value": True}})]
    )
    chain = next(c for c in result.chains if c.field_name == NEEDS_RESTART)

    assert chain.net_before == "false"
    assert chain.confidence is Confidence.MEDIUM
    assert chain.anchor.source == "creation-event"


# -- the stop/start distinction, which is the whole point of the declaration --


def test_an_attribute_ec2_allows_on_a_running_instance_is_one_call(tmp_path):
    plan = written(changed(NO_RESTART, False, True), tmp_path)
    world = revert_world()
    world.instance_attributes[(INSTANCE, NO_RESTART)] = True

    run = Reverter(clients=world, dry_run=False, wait=True).run(plan, now=None)

    assert run.results[0].outcome is Outcome.REVERTED
    assert world.write_api_names() == ["modify_instance_attribute"]
    assert world.waiters == []                       # no restart, so no waiters
    assert world.power[INSTANCE] == "running"        # untouched
    assert world.instance_attributes[(INSTANCE, NO_RESTART)] is False


def test_an_attribute_ec2_requires_stopping_for_does_the_full_dance(tmp_path):
    plan = written(changed(NEEDS_RESTART, False, True), tmp_path)
    world = revert_world()
    world.instance_attributes[(INSTANCE, NEEDS_RESTART)] = True

    run = Reverter(clients=world, dry_run=False, wait=True).run(plan, now=None)

    assert run.results[0].outcome is Outcome.REVERTED
    assert world.write_api_names() == [
        "stop_instances", "modify_instance_attribute", "start_instances"
    ]
    assert world.waiters == ["instance_stopped", "instance_running"]
    assert world.power[INSTANCE] == "running"        # restored to what it was
    assert world.instance_attributes[(INSTANCE, NEEDS_RESTART)] is False


def test_a_stopped_instance_is_left_stopped(tmp_path):
    plan = written(changed(NEEDS_RESTART, False, True), tmp_path)
    world = revert_world()
    world.power[INSTANCE] = "stopped"
    world.instance_attributes[(INSTANCE, NEEDS_RESTART)] = True

    Reverter(clients=world, dry_run=False, wait=False).run(plan, now=None)

    assert world.write_api_names() == ["modify_instance_attribute"]
    assert world.power[INSTANCE] == "stopped"


def test_only_the_restricted_attributes_carry_a_restart_warning():
    no_restart = get_operation("SET_EC2_DISABLE_API_TERMINATION")
    needs_restart = get_operation("SET_EC2_EBS_OPTIMIZED")

    assert "warning" not in no_restart.revert_plan(INSTANCE, "false")
    plan = needs_restart.revert_plan(INSTANCE, "false")
    assert "stops and restarts the instance" in plan["warning"]
    assert [s["api"] for s in plan["steps"]] == [
        "ec2:StopInstances", "ec2:ModifyInstanceAttribute", "ec2:StartInstances"
    ]


def test_the_planned_steps_match_what_is_issued(tmp_path):
    """The plan is what a human approved, so apply_revert must not diverge from it."""
    for attribute in (NO_RESTART, NEEDS_RESTART):
        plan = written(changed(attribute, False, True), tmp_path)
        world = revert_world()
        world.instance_attributes[(INSTANCE, attribute)] = True
        run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=None)

        planned = [s["api"] for s in plan.chains[0].revert["steps"]]
        issued = [c["api"] for c in run.results[0].calls]
        assert set(issued) <= set(planned), attribute
        assert issued


# -- the rest of the machinery comes for free -------------------------------


def test_dry_run_still_calls_nothing(tmp_path):
    plan = written(changed(NEEDS_RESTART, False, True), tmp_path)
    world = live_world_after_session()       # writes forbidden
    world.instance_attributes[(INSTANCE, NEEDS_RESTART)] = True

    run = Reverter(clients=world, dry_run=True).run(plan, now=None)

    assert run.results[0].outcome is Outcome.DRY_RUN
    assert world.writes == []


def test_drift_is_detected(tmp_path):
    from rewind.pipeline.diff import Verdict, diff_plan

    plan = written(changed(NO_RESTART, False, True), tmp_path)
    world = live_world_after_session()
    world.instance_attributes[(INSTANCE, NO_RESTART)] = False   # somebody already changed it

    diff = diff_plan(plan, world, now=None)

    assert diff.entries[0].verdict is Verdict.ALREADY_REVERTED


def test_a_conflict_is_not_overwritten(tmp_path):
    plan = written(changed("instanceInitiatedShutdownBehavior", "stop", "terminate"), tmp_path)
    world = revert_world()
    world.instance_attributes[(INSTANCE, "instanceInitiatedShutdownBehavior")] = "hibernate"

    run = Reverter(clients=world, dry_run=False, wait=False).run(plan, now=None)

    assert run.results[0].outcome is Outcome.SKIPPED
    assert run.results[0].precheck_verdict.value == "CONFLICT"
    assert world.writes == []


@pytest.mark.parametrize("attribute", [a for a in ATTRIBUTES if a.in_config])
def test_config_recorded_attributes_can_use_config_history(attribute):
    operation = next(
        p for p in PLUGINS
        if isinstance(p, Ec2InstanceAttributeOperation) and p.field_name == attribute.name
    )
    assert operation.config_resource_type == "AWS::EC2::Instance"
    expected = "false" if attribute.boolean else "stop"
    raw = False if attribute.boolean else "stop"
    assert operation.value_from_config({attribute.name: raw}) == expected


@pytest.mark.parametrize("attribute", [a for a in ATTRIBUTES if not a.in_config])
def test_attribute_only_fields_decline_config_history(attribute):
    """DescribeInstances does not return these, so a Config item never carries them."""
    operation = next(
        p for p in PLUGINS
        if isinstance(p, Ec2InstanceAttributeOperation) and p.field_name == attribute.name
    )
    assert operation.config_resource_type is None
    assert operation.value_from_config({attribute.name: False}) is None
