"""The generic layer: handling APIs nobody wrote a plugin for.

These are the tests that hold the tool to being a general CLI rather than a demo of four
fields. The `mixed` fixture contains seven changes, only one of which has a plugin.
"""

from __future__ import annotations

import pytest

from rewind.handlers.generic.inverse import classify_event, cli_command, strip_api_version, symmetric_inverse
from rewind.domain import Capability, Confidence, GENERIC_HANDLER
from rewind.handlers import PLUGINS, parse_event
from rewind.handlers.generic.parser import (
    changed_paths,
    flatten,
    is_mutating,
    resource_ids,
)
from rewind.pipeline import plan as build_plan, scan as run_scan

BUCKET = "rewind-demo-bucket"
DB = "rewind-demo-db"
FUNCTION = "rewind-demo-fn"
INSTANCE_C = "i-0ccc000000000000c"


def plan_mixed(mixed, **kwargs):
    return build_plan(
        source=mixed.source(), query=mixed.query(), now=mixed.end_time, **kwargs
    )


def by_field(result):
    return {(c.resource_id, c.field_name): c for c in result.chains}


# -- nothing is silently dropped --------------------------------------------


def test_every_change_is_reported_even_with_no_plugin(mixed):
    """The whole point: a session is not presupposed to contain only known operations."""
    result = plan_mixed(mixed)
    found = by_field(result)

    assert set(found) == {
        (DB, "multiAZ"),                              # plugin-backed
        (DB, "backupRetentionPeriod"),                # same call, no plugin
        (FUNCTION, "timeout"),                        # no plugin, old value in the window
        (FUNCTION, "memorySize"),                     # no plugin, no old value
        (BUCKET, "VersioningConfiguration.Status"),   # no plugin, whole-document API
        (INSTANCE_C, "StopInstances"),                # no value in requestParameters
        (INSTANCE_C, "disableApiTermination"),        # plugin-backed
        (INSTANCE_C, "sriovNetSupport.value"),        # same call, no plugin
    }
    assert result.stats["chains"] == 8
    assert result.stats["pluginBacked"] == 2


def test_a_plugin_claim_does_not_hide_its_neighbours(mixed):
    """One ModifyInstanceAttribute set two attributes; only one has a plugin.

    A plugin claims the request path it consumes, and the generic layer must pick up
    everything else in the same call. This is the exact change a plugin-only design dropped
    on the floor.
    """
    found = by_field(plan_mixed(mixed))
    covered = found[(INSTANCE_C, "disableApiTermination")]
    uncovered = found[(INSTANCE_C, "sriovNetSupport.value")]

    assert covered.handler == "SET_EC2_DISABLE_API_TERMINATION"
    assert uncovered.handler == GENERIC_HANDLER
    assert uncovered.net_after == "simple"
    # Same event, two chains, neither hiding the other.
    assert covered.changes[0].event_id == uncovered.changes[0].event_id


def test_a_plugin_field_and_a_generic_field_coexist_in_one_call(mixed):
    """One ModifyDBInstance set multiAZ (plugin) and backupRetentionPeriod (generic)."""
    found = by_field(plan_mixed(mixed))
    plugin_chain = found[(DB, "multiAZ")]
    generic_chain = found[(DB, "backupRetentionPeriod")]

    assert plugin_chain.handler == "SET_RDS_MULTI_AZ"
    assert plugin_chain.capability is Capability.AUTO
    assert generic_chain.handler == GENERIC_HANDLER
    # Same event, two chains, no double-reporting of the plugin's field.
    assert plugin_chain.changes[0].event_id == generic_chain.changes[0].event_id


def test_read_only_events_are_ignored(mixed):
    """The session contains a DescribeDBInstances; it changed nothing."""
    result = run_scan(mixed.source(), mixed.query())

    assert "DescribeDBInstances" not in {m.event_name for m in result.mutations}
    assert any(e.event_name == "DescribeDBInstances" for e in result.owned_events)


# -- capability tiers -------------------------------------------------------


def test_the_four_tiers_are_each_reached(mixed):
    result = plan_mixed(mixed)
    found = by_field(result)

    assert found[(DB, "multiAZ")].capability is Capability.AUTO
    assert found[(FUNCTION, "timeout")].capability is Capability.MANUAL
    assert found[(BUCKET, "VersioningConfiguration.Status")].capability is (
        Capability.RECONSTRUCTED
    )
    assert found[(FUNCTION, "memorySize")].capability is Capability.DISCOVERED
    assert result.stats["byCapability"] == {
        "DISCOVERED": 5,
        "RECONSTRUCTED": 1,
        "MANUAL": 1,
        "AUTO": 1,
    }


def test_generic_chaining_finds_an_old_value_with_no_api_knowledge(mixed):
    """An earlier UpdateFunctionConfiguration carries timeout=3. Nobody coded for that."""
    chain = by_field(plan_mixed(mixed))[(FUNCTION, "timeout")]

    assert chain.net_before == "3"
    assert chain.net_after == "30"
    assert chain.confidence is Confidence.HIGH
    assert chain.anchor.source == "cloudtrail-window"
    assert chain.handler == GENERIC_HANDLER


def test_a_value_not_present_in_the_parameters_stays_discovered(mixed):
    """StopInstances changes power state, which is nowhere in requestParameters."""
    chain = by_field(plan_mixed(mixed))[(INSTANCE_C, "StopInstances")]

    assert chain.net_after is None
    assert chain.net_before is None
    assert chain.capability is Capability.DISCOVERED
    assert chain.revert.get("manual") is None
    assert any("does not record the new value" in n for n in chain.notes)


def test_stop_and_start_do_not_merge_into_one_meaningless_chain(mixed):
    """Two valueless calls on one resource are separate rows, named by their event."""
    from conftest import as_wire_record
    from rewind.trail import StaticEventSource
    from rewind.trail import from_lookup_record

    start = from_lookup_record(
        as_wire_record(
            {
                "EventId": "m0000009",
                "EventName": "StartInstances",
                "CloudTrailEvent": {
                    "eventID": "m0000009-0000-4000-8000-00000000m009",
                    "eventTime": "2026-09-23T10:22:00Z",
                    "eventName": "StartInstances",
                    "eventSource": "ec2.amazonaws.com",
                    "awsRegion": "us-west-1",
                    "readOnly": False,
                    "userIdentity": {
                        "arn": "arn:aws:sts::111122223333:assumed-role/PerfAgentRole/perf-agent"
                    },
                    "requestParameters": {
                        "instancesSet": {"items": [{"instanceId": INSTANCE_C}]}
                    },
                    "responseElements": None,
                },
            }
        )
    )
    result = build_plan(
        source=StaticEventSource(list(mixed.events) + [start]),
        query=mixed.query(),
        now=mixed.end_time,
    )
    fields = {c.field_name for c in result.chains if c.resource_id == INSTANCE_C}

    assert "StopInstances" in fields and "StartInstances" in fields


# -- the generic inverse ----------------------------------------------------


def test_a_partial_update_api_gets_a_runnable_command(mixed):
    chain = by_field(plan_mixed(mixed))[(FUNCTION, "timeout")]

    assert chain.revert["executable"] is False       # never executed by the tool
    assert chain.revert["manual"] == (
        "aws lambda update-function-configuration --function-name rewind-demo-fn "
        "--timeout 3 --region us-west-1"
    )
    assert chain.revert["caveats"]
    assert any("will not call AWS for it" in n for n in chain.notes)


def test_a_whole_document_api_is_refused_not_guessed(mixed):
    """Calling PutBucketVersioning with one field would discard the rest."""
    chain = by_field(plan_mixed(mixed))[(BUCKET, "VersioningConfiguration.Status")]

    # The values are known - this is not an evidence problem.
    assert (chain.net_before, chain.net_after) == ("Suspended", "Enabled")
    assert chain.confidence is Confidence.HIGH
    assert chain.revert["manual"] is None
    assert "replaces a whole document" in chain.revert["reason"]
    assert chain.capability is Capability.RECONSTRUCTED


@pytest.mark.parametrize(
    "event_name,invertible",
    [
        ("ModifyDBInstance", True),
        ("UpdateFunctionConfiguration20150331v2", True),
        ("SetIdentityPoolRoles", True),
        ("PutBucketPolicy", False),
        ("CreateBucket", False),
        ("DeleteDBInstance", False),
        ("TerminateInstances", False),
        ("AttachRolePolicy", False),
        ("RunInstances", False),
        ("AssumeRole", False),
    ],
)
def test_which_apis_can_be_inverted_mechanically(event_name, invertible):
    assert classify_event(event_name)[0] is invertible
    if not invertible:
        assert classify_event(event_name)[1]


def test_no_inverse_is_offered_without_a_value_path():
    body = symmetric_inverse(
        event_source="ec2.amazonaws.com",
        event_name="StopInstances",
        resource_id=INSTANCE_C,
        identifier_params={"instanceId": INSTANCE_C},
        path=(),
        target_value="running",
    )
    assert body["manual"] is None
    assert body["executable"] is False


@pytest.mark.parametrize(
    "event_name,expected",
    [
        ("UpdateFunctionConfiguration20150331v2", "UpdateFunctionConfiguration"),
        ("CreateFunction20150331", "CreateFunction"),
        ("ModifyDBInstance", "ModifyDBInstance"),
    ],
)
def test_cloudtrail_api_version_suffixes_are_stripped(event_name, expected):
    assert strip_api_version(event_name) == expected


def test_generated_commands_use_real_cli_spelling():
    """Runs of capitals must not be split: modify-db-instance, not modify-d-b-instance."""
    command = cli_command(
        "rds.amazonaws.com",
        "ModifyDBInstance",
        {"dBInstanceIdentifier": DB},
        ("backupRetentionPeriod",),
        "7",
        "us-west-1",
    )
    assert command == (
        "aws rds modify-db-instance --db-instance-identifier rewind-demo-db "
        "--backup-retention-period 7 --region us-west-1"
    )


def test_values_are_shell_quoted():
    command = cli_command(
        "lambda.amazonaws.com", "UpdateFunctionConfiguration", {"functionName": "fn"},
        ("description",), "two words; rm -rf /",
    )
    assert "'two words; rm -rf /'" in command


# -- the mechanical parts in isolation --------------------------------------


def test_flatten_drops_list_indices_so_one_call_covers_many_resources():
    leaves = flatten(
        {"instancesSet": {"items": [{"instanceId": "i-1"}, {"instanceId": "i-2"}]}}
    )
    assert leaves == [
        (("instancesSet", "items", "instanceId"), "i-1"),
        (("instancesSet", "items", "instanceId"), "i-2"),
    ]


def test_resource_ids_prefer_the_cloudtrail_index(mixed):
    event = mixed.event("m0000003")
    assert event.resources
    assert resource_ids(event) == [DB]


def test_resource_ids_fall_back_to_identifier_shaped_parameters(mixed):
    """Lambda events carry an empty Resources index."""
    event = mixed.event("m0000002")
    assert event.resources == []
    assert resource_ids(event) == [FUNCTION]


def test_identifiers_and_call_mechanics_are_not_treated_as_changed_fields(mixed):
    """applyImmediately is call mechanics; dBInstanceIdentifier names the resource."""
    paths = dict(changed_paths(mixed.event("m0000003")))

    assert ("multiAZ",) in paths and ("backupRetentionPeriod",) in paths
    assert ("applyImmediately",) not in paths
    assert ("dBInstanceIdentifier",) not in paths


@pytest.mark.parametrize(
    "event_name,read_only,expected",
    [
        ("ModifyDBInstance", False, True),
        ("DescribeDBInstances", True, False),
        ("DescribeDBInstances", None, False),     # falls back to the verb
        ("ModifyDBInstance", None, True),
        ("ListBuckets", None, False),
        ("GetObject", None, False),
    ],
)
def test_mutating_detection_prefers_cloudtrails_own_flag(mixed, event_name, read_only, expected):
    import dataclasses

    event = dataclasses.replace(
        mixed.event("m0000003"),
        event_name=event_name,
        raw=dict(mixed.event("m0000003").raw, eventName=event_name, readOnly=read_only)
        if read_only is not None
        else {k: v for k, v in mixed.event("m0000003").raw.items() if k != "readOnly"},
    )
    assert is_mutating(event) is expected


def test_plugins_declare_what_they_consume(mixed):
    """Every plugin claim must be a real path in the events it handles, or it is dead code."""
    for plugin in PLUGINS:
        for event in mixed.events:
            claimed = plugin.claimed_paths(event)
            if not plugin.parse(event):
                continue
            available = {path for path, _ in flatten(event.request_parameters)}
            assert claimed <= available or not claimed, (plugin.name, claimed)


def test_a_failed_event_produces_no_generic_mutation(mixed):
    import dataclasses

    failed = dataclasses.replace(mixed.event("m0000003"), error_code="AccessDenied")
    assert parse_event(failed) == []


# -- nested and list-valued parameters --------------------------------------
#
# EC2 nests every attribute as {"attributeName": {"value": X}} and expresses sets as
# {"xSet": {"items": [...]}}. Both shapes broke the first version of the generic layer.


def modify_attribute_event(event_id, when, request_parameters, instance="i-0abc"):
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
    from rewind.trail import StaticEventSource
    from rewind.domain import parse_time
    from rewind.domain import Query

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


def test_a_wrapped_ec2_attribute_is_chained_and_gets_a_valid_command():
    """The flag must come from the attribute name, not from the `value` wrapper.

    Emitting `--value simple` produced a command that looks right and fails, which is worse
    than emitting nothing. Uses an attribute with no plugin, so this exercises the generic
    path rather than a plugin's own revert_plan.
    """
    result = attribute_plan(
        [
            modify_attribute_event(
                "e1", "2026-09-20T09:00:00Z",
                {"instanceId": "i-0abc", "sriovNetSupport": {"value": "off"}},
            ),
            modify_attribute_event(
                "s1", "2026-09-23T11:00:00Z",
                {"instanceId": "i-0abc", "sriovNetSupport": {"value": "simple"}},
            ),
        ]
    )
    chain = next(c for c in result.chains if "sriovNetSupport" in c.field_name)

    assert (chain.net_before, chain.net_after) == ("off", "simple")
    assert chain.confidence is Confidence.HIGH
    assert chain.capability is Capability.MANUAL
    assert chain.revert["manual"] == (
        "aws ec2 modify-instance-attribute --instance-id i-0abc "
        "--sriov-net-support off --region us-west-1"
    )
    assert "--value" not in chain.revert["manual"]


def test_a_boolean_becomes_a_flag_pair_not_an_argument():
    """`--multi-az false` is rejected by the CLI; `--no-multi-az` is what it accepts."""
    assert cli_command(
        "rds.amazonaws.com", "ModifyDBInstance", {"dBInstanceIdentifier": DB},
        ("multiAZ",), "false",
    ) == "aws rds modify-db-instance --db-instance-identifier rewind-demo-db --no-multi-az"
    assert cli_command(
        "rds.amazonaws.com", "ModifyDBInstance", {"dBInstanceIdentifier": DB},
        ("multiAZ",), "true",
    ) == "aws rds modify-db-instance --db-instance-identifier rewind-demo-db --multi-az"


def test_every_value_of_a_list_field_is_kept():
    """Two security groups must not silently become one."""
    result = attribute_plan(
        [
            modify_attribute_event(
                "e1", "2026-09-20T09:00:00Z",
                {"instanceId": "i-0abc", "groupSet": {"items": [{"groupId": "sg-aaaa"}]}},
            ),
            modify_attribute_event(
                "s1", "2026-09-23T11:00:00Z",
                {
                    "instanceId": "i-0abc",
                    "groupSet": {"items": [{"groupId": "sg-1111"}, {"groupId": "sg-2222"}]},
                },
            ),
        ]
    )
    chain = next(c for c in result.chains if "groupId" in c.field_name)

    assert chain.net_after == "sg-1111,sg-2222"
    assert chain.net_before == "sg-aaaa"
    assert chain.confidence is Confidence.HIGH


def test_an_identifier_shaped_value_that_is_not_the_resource_is_still_a_field():
    """`groupSet.items.groupId` ends in "Id" but is the value being set, not the resource.

    The test that matters is whether the value *is* one of the resource ids, not whether the
    key looks like an identifier. Getting this wrong made changing security groups register
    as an unparseable change.
    """
    event = modify_attribute_event(
        "s1", "2026-09-23T11:00:00Z",
        {"instanceId": "i-0abc", "groupSet": {"items": [{"groupId": "sg-1111"}]}},
    )

    assert resource_ids(event) == ["i-0abc"]
    paths = dict(changed_paths(event))
    assert ("groupSet", "items", "groupId") in paths
    assert ("instanceId",) not in paths        # this one does name the resource


def test_a_list_field_is_reported_but_no_command_is_guessed():
    """CLI spelling for list parameters varies per API, so it is refused, not invented."""
    result = attribute_plan(
        [
            modify_attribute_event(
                "e1", "2026-09-20T09:00:00Z",
                {"instanceId": "i-0abc", "groupSet": {"items": [{"groupId": "sg-aaaa"}]}},
            ),
            modify_attribute_event(
                "s1", "2026-09-23T11:00:00Z",
                {
                    "instanceId": "i-0abc",
                    "groupSet": {"items": [{"groupId": "sg-1111"}, {"groupId": "sg-2222"}]},
                },
            ),
        ]
    )
    chain = next(c for c in result.chains if "groupId" in c.field_name)

    assert chain.capability is Capability.RECONSTRUCTED
    assert chain.revert["manual"] is None
    assert chain.revert["targetValue"] == "sg-aaaa"
    assert "nested" in chain.revert["reason"]


@pytest.mark.parametrize(
    "path,expected",
    [
        (("timeout",), "timeout"),
        (("disableApiTermination", "value"), "disableApiTermination"),
        (("instanceType", "value"), "instanceType"),
        (("VersioningConfiguration", "Status"), None),      # genuinely nested
        (("groupSet", "items", "groupId"), None),           # genuinely nested
        ((), None),
    ],
)
def test_which_flag_a_path_maps_to(path, expected):
    from rewind.handlers.generic.inverse import flag_segment

    assert flag_segment(path) == expected


def test_nested_paths_are_refused_rather_than_mis_flagged():
    from rewind.handlers.generic.inverse import command_is_derivable

    assert command_is_derivable(("timeout",), "3") is None
    assert command_is_derivable(("disableApiTermination", "value"), "true") is None
    assert "nested" in command_is_derivable(("VersioningConfiguration", "Status"), "Enabled")
    assert "list" in command_is_derivable(("groups",), "sg-1,sg-2")


# -- which resource a call acted on -----------------------------------------
#
# Every shape below is a real CloudTrail payload seen while running the CLI against a live
# account. Each one produced a wrong answer that no fixture test had caught.


def event(name, source, request=None, response=None, resources=None):
    from rewind.trail import from_lookup_record

    return from_lookup_record(
        {
            "EventId": "00000000-0000-4000-8000-000000000000",
            "EventName": name,
            "Resources": resources or [],
            "CloudTrailEvent": {
                "eventID": "00000000-0000-4000-8000-000000000000",
                "eventTime": "2026-09-23T17:00:00Z",
                "eventName": name,
                "eventSource": source,
                "awsRegion": "us-west-1",
                "readOnly": False,
                "userIdentity": {"arn": "arn:aws:sts::111122223333:assumed-role/R/s"},
                "requestParameters": request or {},
                "responseElements": response,
            },
        }
    )


def test_a_parameter_that_merely_ends_in_name_is_not_the_resource():
    """SSM sends platformName "Amazon Linux" and agentName "amazon-ssm-agent".

    Both end in a noun-ish suffix, so a flat suffix test made all three of these the
    resource - and then reported every field three times, twice against something that is
    not a resource at all. The instance wins because the *API names its own subject*.
    """
    ssm = event(
        "UpdateInstanceInformation",
        "ssm.amazonaws.com",
        {
            "instanceId": "i-0fed55556666cccc3",
            "platformName": "Amazon Linux",
            "agentName": "amazon-ssm-agent",
            "agentVersion": "3.3.4624.0",
            "platformType": "Linux",
        },
    )

    assert resource_ids(ssm) == ["i-0fed55556666cccc3"]

    mutations = parse_event(ssm)
    assert {m.resource_id for m in mutations} == {"i-0fed55556666cccc3"}
    assert "Amazon Linux" not in {m.resource_id for m in mutations}


def test_one_call_does_not_spread_a_field_across_unrelated_resources():
    """RDS lists the DB, its parameter groups, its subnet group, its SGs *and* its VPC.

    Keeping all of them made the tool announce that a VPC's allowMajorVersionUpgrade had
    changed. The request names what it acted on; the index does not rank its own entries.
    """
    rds = event(
        "ModifyDBInstance",
        "rds.amazonaws.com",
        {"dBInstanceIdentifier": "rewind-demo-payments", "allowMajorVersionUpgrade": False},
        resources=[
            {"ResourceName": "rewind-demo-payments", "ResourceType": "AWS::RDS::DBInstance"},
            {"ResourceName": "default.postgres18", "ResourceType": "AWS::RDS::DBParameterGroup"},
            {"ResourceName": "sg-027845b02c5dfeef4", "ResourceType": "AWS::EC2::SecurityGroup"},
            {"ResourceName": "vpc-04cc5141df5edb45f", "ResourceType": "AWS::EC2::VPC"},
            {"ResourceName": "subnet-0187942c7e37144e3", "ResourceType": "AWS::EC2::Subnet"},
        ],
    )

    assert resource_ids(rds) == ["rewind-demo-payments"]

    changed = {(m.resource_id, m.field_name) for m in parse_event(rds)}
    assert changed == {("rewind-demo-payments", "allowMajorVersionUpgrade")}
    assert not [r for r, _ in changed if r.startswith(("vpc-", "sg-", "subnet-"))]


def test_a_creation_is_attributed_to_what_it_created_not_to_its_arguments():
    """RunInstances names an AMI, a subnet and security groups; it creates neither."""
    run = event(
        "RunInstances",
        "ec2.amazonaws.com",
        {
            "imageId": "ami-0f3cd23dec6f5801c",
            "subnetId": "subnet-0187942c7e37144e3",
            "instanceType": "t3.micro",
            "instancesSet": {"items": [{"minCount": 1, "maxCount": 1}]},
        },
        {
            "instancesSet": {
                "items": [
                    {"instanceId": "i-0abc11112222aaaa1", "instanceType": "t3.micro"},
                    {"instanceId": "i-0def33334444bbbb2", "instanceType": "t3.micro"},
                ]
            },
            "groupSet": {"items": [{"groupId": "sg-027845b02c5dfeef4"}]},
        },
    )

    assert resource_ids(run) == ["i-0abc11112222aaaa1", "i-0def33334444bbbb2"]

    mutations = parse_event(run)
    # One row per created instance, under the event name. Launch arguments are not fields:
    # "minCount was set to 1" is not state anybody can revert.
    assert {m.resource_id for m in mutations} == {
        "i-0abc11112222aaaa1",
        "i-0def33334444bbbb2",
    }
    assert {m.field_name for m in mutations} == {"RunInstances"}
    assert not [m for m in mutations if "Count" in m.field_name]


def test_several_ids_at_one_parameter_path_are_still_several_resources():
    """The fan-out that must survive: one call, two instances of the same kind.

    This is the line between the two behaviours. Ids sharing a parameter path are genuinely
    several targets; ids at different paths are different kinds of thing.
    """
    monitor = event(
        "MonitorInstances",
        "ec2.amazonaws.com",
        {
            "instancesSet": {
                "items": [
                    {"instanceId": "i-0aaa000000000000a"},
                    {"instanceId": "i-0bbb000000000000b"},
                ]
            }
        },
    )

    assert resource_ids(monitor) == ["i-0aaa000000000000a", "i-0bbb000000000000b"]


def test_repeated_identical_calls_collapse_to_a_no_op_rather_than_a_revert():
    """An SSM agent heartbeats every few minutes, setting the same values each time.

    Seen live: 44 raw changes over 7 fields, every one landing at "already back to original"
    with nothing revertible. That is the correct answer, and the chain produces it - there is
    no filter anywhere saying "ignore SSM".
    """
    from datetime import datetime, timedelta, timezone

    from rewind.domain import Query
    from rewind.trail import StaticEventSource
    import dataclasses

    base = event(
        "UpdateInstanceInformation",
        "ssm.amazonaws.com",
        {
            "instanceId": "i-0fed55556666cccc3",
            "agentVersion": "3.3.4624.0",
            "platformVersion": "2023",
        },
    )
    start = datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc)

    def beat(minutes, tag):
        return dataclasses.replace(
            base, event_id="beat-%s" % tag, event_time=start + timedelta(minutes=minutes)
        )

    # One heartbeat *before* the window is what makes the anchor provable, and it is what a
    # real account always has: the agent was already running before anybody started looking.
    beats = [beat(-10, "anchor")] + [beat(5 * n, str(n)) for n in range(1, 6)]

    result = build_plan(
        source=StaticEventSource(beats),
        query=Query(
            identity="R",
            start_time=start,
            end_time=start + timedelta(hours=1),
            region="us-west-1",
        ),
        now=start + timedelta(hours=1),
    )

    assert result.stats["changes"] == 10, "five in-window heartbeats x two fields"
    assert result.stats["chains"] == 2, "collapsed to one chain per field"
    assert result.stats["netNoOp"] == 2
    assert result.stats["revertible"] == 0
    for chain in result.chains:
        assert chain.net_before == chain.net_after
        assert chain.executable is False
        assert any("net effect is zero" in note for note in chain.notes)


def test_the_api_names_its_own_subject_when_several_parameters_could_be_the_resource():
    """One call can carry three identifier-shaped names. Document order is not an answer.

    ``ModifyDBInstance`` moving a parameter group *and* Multi-AZ names three things that all
    end in an identifier suffix. Without ranking, "the first one in the payload" decides,
    which is arbitrary - and here it is the parameter group, not the database. The API is not
    shy about naming its subject, so ``dBInstanceIdentifier`` wins over ``...GroupName``.
    """
    rds = event(
        "ModifyDBInstance",
        "rds.amazonaws.com",
        {
            # deliberately first in the payload
            "dBParameterGroupName": "default.postgres18",
            "dBSubnetGroupName": "demo-subnet-group",
            "dBInstanceIdentifier": "rewind-demo-payments",
            "multiAZ": True,
        },
    )

    assert resource_ids(rds) == ["rewind-demo-payments"]

    owners = {m.resource_id for m in parse_event(rds)}
    assert owners == {"rewind-demo-payments"}


# -- what a valueless change is allowed to claim -----------------------------


def valueless_chain(mixed):
    result = plan_mixed(mixed)
    return next(c for c in result.chains if c.net_after is None)


def test_a_valueless_step_never_renders_as_none(mixed):
    """Found by reading real output: "StopInstances: ? -> None".

    ``cell()`` exists so an unproven value is "?" and never a Python repr. The evidence block
    applied it to ``before`` and not to ``after`` - the same omission that had already been
    fixed once in the diff table, which is why the guard test below covers every renderer.
    """
    from rewind.report.plan import render_plan

    text = render_plan(plan_mixed(mixed), explain=True)

    assert "None" not in text
    assert "? -> ?" in text


def test_no_renderer_interpolates_a_nullable_value_without_cell():
    """The class of bug, not the instance. Two renderers had it; a grep is the only guard.

    Any of these attributes can be None, and every one of them must reach the page through
    ``cell()`` so it becomes "?" rather than "None".
    """
    import pathlib
    import re

    nullable = re.compile(
        r"(?<!cell\()\b\w+\.(after|before|net_after|net_before|live_value|target_value"
        r"|observed_before|observed_after)\b"
    )
    offenders = []
    for path in sorted(pathlib.Path("src/rewind/report").glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "value_known" in code or "cell(" in code:
                continue
            if nullable.search(code):
                offenders.append("%s:%d %s" % (path.name, number, line.strip()))
    assert not offenders, "wrap these in cell(): " + "; ".join(offenders)


def test_repeated_valueless_calls_do_not_claim_a_value_to_restore(mixed):
    """The note said "the value to restore is the one from before the first change".

    Directly below it, another note said the value cannot be established at all. One of the
    two had to go, and it was not the honest one.
    """
    chain = valueless_chain(mixed)

    assert chain.net_before is None and chain.net_after is None
    assert not any("value to restore" in note for note in chain.notes)
    assert any("cannot be reconstructed" in note for note in chain.notes)
