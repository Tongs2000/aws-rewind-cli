"""CLI behaviour: scan, plan, window parsing, output formats, plan file round-trip."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import DB_INSTANCE, INSTANCE_A, INSTANCE_B, FakeCloudTrail
from rewind.cli import EXIT_OK, EXIT_USAGE, main
from rewind.trail import CloudTrailEventSource
from rewind.domain import PLAN_FORMAT_VERSION
from rewind.timeutil import WindowError, parse_duration, resolve_window

UTC = timezone.utc


def run(argv, session, capsys, now=None, source=None):
    """Invoke the CLI with an injected event source, so no AWS call is possible.

    ``--region`` is supplied automatically because the tool otherwise reads it from the
    ambient AWS configuration, which tests must not depend on.
    """
    from rewind.cli import context as cli_module

    if argv and argv[0] in ("scan", "plan") and "--region" not in argv:
        argv = list(argv) + ["--region", session.region]
    original = cli_module.source
    cli_module.source = lambda region, args: source or session.source()
    try:
        code = main(argv, now=now or session.end_time)
    finally:
        cli_module.source = original
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- window parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("30s", timedelta(seconds=30)), ("90m", timedelta(minutes=90)),
     ("2h", timedelta(hours=2)), ("3d", timedelta(days=3)), ("1w", timedelta(weeks=1))],
)
def test_duration_parsing(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "90", "m", "0h", "-2h", "90 minutes", "1y"])
def test_bad_durations_are_rejected(text):
    with pytest.raises(WindowError):
        parse_duration(text)


def test_since_and_explicit_window_are_mutually_exclusive():
    with pytest.raises(WindowError):
        resolve_window(since="1h", start="2026-09-22T17:00:00Z")


def test_end_must_be_after_start():
    with pytest.raises(WindowError):
        resolve_window(start="2026-09-22T18:00:00Z", end="2026-09-22T17:00:00Z")


def test_a_window_is_required():
    with pytest.raises(WindowError):
        resolve_window()


def test_since_resolves_relative_to_now():
    now = datetime(2026, 9, 22, 18, 0, 0, tzinfo=UTC)
    start, end = resolve_window(since="90m", now=now)

    assert end == now
    assert start == now - timedelta(minutes=90)


# -- scan -------------------------------------------------------------------


def test_scan_lists_only_the_requested_identitys_changes(session, capsys):
    code, out, _ = run(["scan", "--identity", "perf-agent", "--start",
                        session.start_time.isoformat(), "--end",
                        session.end_time.isoformat()], session, capsys)

    assert code == EXIT_OK
    # 8 = two resizes on A, one on B, monitoring on A and B (one call), two
    # Lambda changes, one RDS change.
    assert "8 tracked field change(s)" in out
    # The ci-deployer MonitorInstances inside the window is not ours.
    assert "i-0ccc000000000000c" not in out
    assert INSTANCE_A in out and INSTANCE_B in out
    # scan deliberately does not claim to know previous values.
    assert "-> t3.small" in out
    assert "rewind plan" in out


def test_scan_json_output_is_machine_readable(session, capsys):
    code, out, _ = run(["scan", "--identity", "perf-agent", "--since", "20m",
                        "--output", "json"], session, capsys)

    body = json.loads(out)
    assert code == EXIT_OK
    assert body["query"]["identity"] == "perf-agent"
    assert len(body["changes"]) == 8
    assert {c["field"] for c in body["changes"]} == {
        "instanceType", "monitoring", "provisionedConcurrency", "multiAZ"
    }
    assert all("setTo" in c and "eventId" in c for c in body["changes"])


def test_scan_reports_other_identities_when_nothing_matches(session, capsys):
    code, out, _ = run(["scan", "--identity", "nobody", "--since", "20m"], session, capsys)

    assert code == EXIT_OK
    assert "No tracked field changes" in out
    assert "Other identities" in out
    assert "perf-agent" in out


# -- plan -------------------------------------------------------------------


def test_plan_table_shows_before_now_and_confidence(session, capsys):
    code, out, _ = run(["plan", "--identity", "perf-agent", "--since", "20m"], session, capsys)

    assert code == EXIT_OK
    assert "t3.micro -> t3.small" in out          # chained through t3.large
    assert "NONE -> 5" in out                     # chained through 1
    assert "false -> true" in out
    assert "? -> t3.small" in out                 # unprovable anchor renders as ?
    assert "revertible : 4 of 6" in out
    assert "HIGH=2 MEDIUM=2 UNKNOWN=2" in out


def test_plan_never_prints_a_guessed_value(session, capsys):
    _, out, _ = run(["plan", "--identity", "perf-agent", "--since", "20m"], session, capsys)

    # The long-lived instance's row must not invent a plausible old type.
    rows = [line for line in out.splitlines() if INSTANCE_B in line]
    assert rows
    for row in rows:
        assert "?" in row
        assert "t3.micro" not in row
        assert "t3.nano" not in row


def test_plan_explain_shows_every_step_and_its_evidence(session, capsys):
    _, out, _ = run(["plan", "--identity", "perf-agent", "--since", "20m", "--explain"],
                    session, capsys)

    assert "Evidence" in out
    assert "step 1" in out and "step 2" in out
    assert session.event("h0000003").event_id in out   # anchor evidence
    assert session.event("s0000001").event_id in out   # first session step
    assert "t3.micro -> t3.large" in out               # the intermediate value is shown
    assert "revert to  : t3.micro" in out
    assert "not automatic" in out                      # the UNKNOWN chains


def test_plan_writes_a_reusable_document(session, capsys, tmp_path):
    out_file = tmp_path / "plan.json"
    code, out, _ = run(["plan", "--identity", "perf-agent", "--since", "20m",
                        "-o", str(out_file)], session, capsys)

    assert code == EXIT_OK
    assert "Plan written to" in out
    body = json.loads(out_file.read_text())
    assert body["rewindPlanVersion"] == PLAN_FORMAT_VERSION
    assert body["tool"]["name"] == "rewind"
    assert body["query"]["identity"] == "perf-agent"
    assert len(body["chains"]) == 6

    chain = next(c for c in body["chains"] if c["resourceId"] == INSTANCE_A
                 and c["field"] == "instanceType")
    assert chain["netBefore"] == "t3.micro"
    assert chain["netAfter"] == "t3.small"
    assert chain["changeCount"] == 2
    assert chain["revert"]["executable"] is True
    assert chain["revert"]["targetValue"] == "t3.micro"
    assert [s["api"] for s in chain["revert"]["steps"]] == [
        "ec2:StopInstances", "ec2:ModifyInstanceAttribute", "ec2:StartInstances"
    ]
    assert chain["anchor"]["confidence"] == "HIGH"
    assert len(chain["changes"]) == 2


def test_plan_document_is_json_serialisable_without_custom_encoders(session, capsys):
    _, out, _ = run(["plan", "--identity", "perf-agent", "--since", "20m",
                     "--output", "json"], session, capsys)

    body = json.loads(out)
    # Round-trips with the plain encoder: no datetime or enum leaked through.
    assert json.loads(json.dumps(body)) == body


def test_plan_warns_about_the_retention_limit_and_recent_windows(session, capsys):
    _, out, _ = run(["plan", "--identity", "perf-agent", "--since", "20m"], session,
                    capsys, now=session.end_time)

    assert "90-day" in out or "90 days" in out
    assert "eventually consistent" in out


def test_plan_warns_when_the_window_predates_retention(session, capsys):
    long_ago = session.end_time + timedelta(days=200)
    _, out, _ = run(["plan", "--identity", "perf-agent", "--start",
                     session.start_time.isoformat(), "--end",
                     session.end_time.isoformat()], session, capsys, now=long_ago)

    assert "past CloudTrail event history retention" in out


def test_empty_window_is_not_an_error(session, capsys):
    before = session.start_time - timedelta(days=30)
    code, out, _ = run(["plan", "--identity", "perf-agent", "--start", before.isoformat(),
                        "--end", (before + timedelta(minutes=1)).isoformat()],
                       session, capsys)

    assert code == EXIT_OK
    assert "No CloudTrail events were found" in out
    assert "Nothing to plan" in out


# -- usage ------------------------------------------------------------------


def test_no_command_prints_help(capsys):
    code = main([])
    assert code == EXIT_USAGE
    assert "usage" in capsys.readouterr().out.lower()


def test_a_bad_duration_exits_with_a_usage_code(session, capsys):
    code, _, err = run(["plan", "--identity", "perf-agent", "--since", "soon"], session, capsys)

    assert code == EXIT_USAGE
    assert "cannot parse duration" in err


def test_resolvers_command_explains_the_chain(session, capsys):
    code, out, _ = run(["resolvers"], session, capsys)

    assert code == EXIT_OK
    assert "priority order" in out
    assert "response-elements" in out
    assert "config-history" in out
    # The optional sources are listed as inactive, with the flag that switches them on.
    assert "--use-config" in out
    assert "--snapshot" in out
    assert "--set" in out


# -- the real CloudTrail paginator ------------------------------------------


def test_the_paginator_walks_pages_and_uses_only_the_event_name_index(session):
    """Guards the two query rules: one attribute per call, never ResourceName."""
    client = FakeCloudTrail(session.wire_records(), page_size=2)
    source = CloudTrailEventSource(client, page_size=2)

    window_events, truncated = source.window_events(session.start_time, session.end_time)
    named, _ = source.named_events(
        session.start_time - timedelta(days=30), session.end_time, ["ModifyInstanceAttribute"]
    )

    assert truncated is False
    assert len(window_events) == 8            # includes the ci-deployer call
    assert {e.event_name for e in named} == {"ModifyInstanceAttribute"}
    # Three before the session, three during it, including the failed one.
    assert len(named) == 6
    assert set(client.attribute_keys) == {"EventName"}
    assert client.queries[0].get("LookupAttributes") is None   # window query is unfiltered


def test_the_paginator_reports_truncation_at_the_page_cap(session):
    client = FakeCloudTrail(session.wire_records(), page_size=1)
    source = CloudTrailEventSource(client, max_pages=2, page_size=1)

    events, truncated = source.window_events(session.start_time, session.end_time)

    assert truncated is True
    assert len(events) == 2


# -- diff -------------------------------------------------------------------


def make_plan_file(session, capsys, tmp_path):
    out_file = tmp_path / "plan.json"
    run(["plan", "--identity", "perf-agent", "--start", session.start_time.isoformat(),
         "--end", session.end_time.isoformat(), "-o", str(out_file)], session, capsys)
    return str(out_file)


def run_diff(argv, session, capsys, world, now=None, source=None):
    """Invoke `rewind diff` with injected AWS clients, so no real call is possible."""
    from rewind.cli import context as cli_module

    original_clients, original_source = cli_module.clients, cli_module.source
    cli_module.clients = lambda region, args: world
    cli_module.source = lambda region, args: source or session.source()
    try:
        code = main(argv, now=now or session.end_time)
    finally:
        cli_module.clients, cli_module.source = original_clients, original_source
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_diff_reports_no_drift_on_an_untouched_account(session, capsys, tmp_path):
    from conftest import live_world_after_session

    plan_path = make_plan_file(session, capsys, tmp_path)
    code, out, _ = run_diff(["diff", plan_path], session, capsys, live_world_after_session())

    assert code == EXIT_OK
    assert "drift      : none" in out
    assert "REVERTIBLE=4" in out and "UNPROVEN=2" in out
    assert "4 field(s) are ready to revert" in out
    # The table shows the plan's view beside the live value.
    assert "t3.micro" in out and "t3.small" in out


def test_diff_flags_a_conflict_and_suggests_blame(session, capsys, tmp_path):
    from conftest import live_world_after_session

    world = live_world_after_session()
    world.instance_types[INSTANCE_A] = "t3.2xlarge"
    plan_path = make_plan_file(session, capsys, tmp_path)

    code, out, _ = run_diff(["diff", plan_path], session, capsys, world)

    assert code == EXIT_OK                      # no --exit-code, so still 0
    assert "CONFLICT   : 1 field(s)" in out
    assert "must not be overwritten" in out
    assert "run with --blame" in out
    assert "t3.2xlarge" in out


def test_diff_exit_code_flag_signals_conflicts_for_scripts(session, capsys, tmp_path):
    from conftest import live_world_after_session
    from rewind.cli import EXIT_CONFLICT

    world = live_world_after_session()
    plan_path = make_plan_file(session, capsys, tmp_path)

    clean, _, _ = run_diff(["diff", plan_path, "--exit-code"], session, capsys, world)
    # A value that is neither the session's outcome nor the pre-session value: real drift.
    world.instance_types[INSTANCE_A] = "t3.2xlarge"
    dirty, _, _ = run_diff(["diff", plan_path, "--exit-code"], session, capsys, world)

    assert clean == EXIT_OK
    assert dirty == EXIT_CONFLICT


def test_diff_json_output_is_machine_readable(session, capsys, tmp_path):
    from conftest import live_world_after_session

    plan_path = make_plan_file(session, capsys, tmp_path)
    code, out, _ = run_diff(["diff", plan_path, "--output", "json"], session, capsys,
                            live_world_after_session())

    body = json.loads(out)
    assert code == EXIT_OK
    assert body["driftFree"] is True
    assert body["summary"]["REVERTIBLE"] == 4
    assert len(body["entries"]) == 6
    entry = next(e for e in body["entries"] if e["resourceId"] == INSTANCE_A
                 and e["field"] == "instanceType")
    assert entry == {
        "chainId": entry["chainId"],
        "resourceType": "AWS::EC2::Instance",
        "resourceId": INSTANCE_A,
        "field": "instanceType",
        "handler": "SET_EC2_INSTANCE_TYPE",
        "capability": "AUTO",
        "verdict": "REVERTIBLE",
        "confidence": "HIGH",
        "planBefore": "t3.micro",
        "planAfter": "t3.small",
        "liveValue": "t3.small",
        "driftFree": True,
        "reason": entry["reason"],
        "liveDetail": {"instanceState": "running"},
    }
    assert json.loads(json.dumps(body)) == body


def test_diff_uses_the_region_recorded_in_the_plan(session, capsys, tmp_path):
    from conftest import live_world_after_session

    plan_path = make_plan_file(session, capsys, tmp_path)
    # No --region is passed, and no ambient AWS configuration exists in tests.
    code, out, _ = run_diff(["diff", plan_path], session, capsys, live_world_after_session())

    assert code == EXIT_OK
    assert "us-west-1" in out


def test_diff_on_a_bad_plan_file_exits_with_an_error_not_a_traceback(session, capsys, tmp_path):
    from conftest import live_world_after_session
    from rewind.cli import EXIT_ERROR

    bad = tmp_path / "bad.json"
    bad.write_text('{"rewindPlanVersion": 99}')

    code, out, err = run_diff(["diff", str(bad)], session, capsys, live_world_after_session())

    assert code == EXIT_ERROR
    assert "not supported by this build" in err
    assert "Traceback" not in err and out == ""


# -- revert -----------------------------------------------------------------


def revertible_world():
    from conftest import live_world_after_session

    world = live_world_after_session()
    world.allow_writes = True
    return world


def test_revert_is_a_dry_run_without_confirm(session, capsys, tmp_path):
    from conftest import live_world_after_session

    plan_path = make_plan_file(session, capsys, tmp_path)
    world = live_world_after_session()  # writes forbidden, so any write would raise

    code, out, _ = run_diff(["revert", plan_path], session, capsys, world)

    assert code == EXIT_OK
    assert "mode       : DRY RUN - nothing was called" in out
    assert "DRY_RUN=4" in out
    assert "would call ec2:StopInstances" in out
    assert "Re-run with --confirm to apply" in out
    assert world.writes == []
    assert world.instance_types[INSTANCE_A] == "t3.small"


def test_revert_with_confirm_applies_and_verifies(session, capsys, tmp_path):
    plan_path = make_plan_file(session, capsys, tmp_path)
    world = revertible_world()

    code, out, _ = run_diff(["revert", plan_path, "--confirm", "--no-wait"],
                            session, capsys, world)

    assert code == EXIT_OK
    assert "mode       : APPLIED" in out
    assert "REVERTED=4" in out and "SKIPPED=2" in out
    assert "called     ec2:ModifyInstanceAttribute" in out
    assert world.instance_types[INSTANCE_A] == "t3.micro"
    assert world.concurrency == {}


def test_revert_only_accepts_a_known_chain_id(session, capsys, tmp_path):
    plan_path = make_plan_file(session, capsys, tmp_path)
    world = revertible_world()

    code, _, err = run_diff(["revert", plan_path, "--only", "chn-nope", "--confirm"],
                            session, capsys, world)

    assert code == EXIT_USAGE
    assert "no such chain" in err
    assert world.writes == []


def test_revert_exit_code_flag_reports_unfinished_work(session, capsys, tmp_path):
    from rewind.cli import EXIT_CONFLICT

    plan_path = make_plan_file(session, capsys, tmp_path)
    world = revertible_world()

    # The two unprovable chains are always skipped, so there is always unfinished work.
    code, _, _ = run_diff(["revert", plan_path, "--confirm", "--no-wait", "--exit-code"],
                          session, capsys, world)
    assert code == EXIT_CONFLICT


def test_revert_writes_a_log_and_json(session, capsys, tmp_path):
    plan_path = make_plan_file(session, capsys, tmp_path)
    log_path = tmp_path / "revert-log.json"
    world = revertible_world()

    code, out, _ = run_diff(
        ["revert", plan_path, "--confirm", "--no-wait", "--output", "json",
         "--log", str(log_path)],
        session, capsys, world,
    )

    body = json.loads(out)
    assert code == EXIT_OK
    assert body["dryRun"] is False
    assert body["summary"]["REVERTED"] == 4
    assert len(body["executionOrder"]) == 6
    assert json.loads(log_path.read_text()) == body
    assert json.loads(json.dumps(body)) == body


def test_revert_surfaces_the_instance_restart_warning(session, capsys, tmp_path):
    from conftest import live_world_after_session

    plan_path = make_plan_file(session, capsys, tmp_path)
    _, out, _ = run_diff(["revert", plan_path], session, capsys, live_world_after_session())

    assert "stops and restarts the instance" in out


# -- phase 4: --set, snapshots, AWS Config -----------------------------------


def run_with_config(argv, session, capsys, config_client=None, world=None, now=None):
    """Invoke the CLI with the event source, AWS clients and Config client injected."""
    from rewind.cli import context as cli_module

    from conftest import live_world_after_session

    if argv and argv[0] in ("scan", "plan", "snapshot") and "--region" not in argv:
        argv = list(argv) + ["--region", session.region]
    originals = (cli_module.source, cli_module.clients, cli_module.config_client)
    cli_module.source = lambda region, args: session.source()
    cli_module.clients = lambda region, args: world or live_world_after_session()
    cli_module.config_client = lambda region, args: config_client
    try:
        code = main(argv, now=now or session.end_time)
    finally:
        cli_module.source, cli_module.clients, cli_module.config_client = originals
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_plan_set_fills_an_unknown_and_labels_it_asserted(session, capsys):
    code, out, _ = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m",
         "--set", "%s.instanceType=t3.nano" % INSTANCE_B],
        session, capsys,
    )

    assert code == EXIT_OK
    assert "ASSERTED=1" in out
    assert "t3.nano -> t3.small" in out
    assert "revertible : 5 of 6" in out
    # The row names the source, so nobody mistakes it for evidence.
    assert "operator-supplied" in out


def test_plan_rejects_a_malformed_set(session, capsys):
    code, _, err = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m", "--set", "nonsense"],
        session, capsys,
    )

    assert code == EXIT_USAGE
    assert "--set needs SELECTOR=VALUE" in err


def test_plan_warns_when_a_set_was_not_needed(session, capsys):
    code, out, _ = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m",
         "--set", "%s.instanceType=t9.wrong" % INSTANCE_A],
        session, capsys,
    )

    assert code == EXIT_OK
    assert "was ignored" in out
    assert "t3.micro -> t3.small" in out  # the proven value still won


def test_snapshot_writes_a_local_file(session, capsys, tmp_path):
    out_file = tmp_path / "snap.json"

    code, out, _ = run_with_config(
        ["snapshot", "--instance", INSTANCE_A, "--db-instance", DB_INSTANCE,
         "-o", str(out_file)],
        session, capsys,
    )

    assert code == EXIT_OK
    assert "Snapshot written to" in out
    body = json.loads(out_file.read_text())
    assert body["rewindSnapshotVersion"] == 1
    assert body["region"] == "us-west-1"
    recorded = {(e["resourceId"], e["field"]): e["value"] for e in body["entries"]}
    # One --instance now covers every EC2 field the tool supports.
    assert recorded[(INSTANCE_A, "instanceType")] == "t3.small"
    assert recorded[(INSTANCE_A, "monitoring")] == "enabled"
    assert recorded[(INSTANCE_A, "sourceDestCheck")] == "false"
    assert recorded[(DB_INSTANCE, "multiAZ")] == "true"
    assert "0 unreadable" in out


def test_snapshot_needs_at_least_one_resource(session, capsys):
    code, _, err = run_with_config(["snapshot"], session, capsys)

    assert code == EXIT_USAGE
    assert "name at least one resource" in err


def test_snapshot_validates_the_function_selector(session, capsys):
    code, _, err = run_with_config(
        ["snapshot", "--function", "just-a-name"], session, capsys
    )

    assert code == EXIT_USAGE
    assert "--function needs NAME:QUALIFIER" in err


def test_plan_uses_a_snapshot_taken_before_the_session(session, capsys, tmp_path):
    from datetime import timedelta

    from rewind.domain import iso

    snap_file = tmp_path / "snap.json"
    snap_file.write_text(
        json.dumps(
            {
                "rewindSnapshotVersion": 1,
                "takenAt": iso(session.start_time - timedelta(hours=1)),
                "region": "us-west-1",
                "entries": [
                    {
                        "resourceType": "AWS::EC2::Instance",
                        "resourceId": INSTANCE_B,
                        "field": "instanceType",
                        "operation": "SET_EC2_INSTANCE_TYPE",
                        "value": "t3.nano",
                    }
                ],
            }
        )
    )

    code, out, _ = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m", "--snapshot", str(snap_file)],
        session, capsys,
    )

    assert code == EXIT_OK
    assert "local-snapshot" in out
    assert "t3.nano -> t3.small" in out


def test_plan_refuses_a_snapshot_from_another_region(session, capsys, tmp_path):
    from rewind.domain import iso

    snap_file = tmp_path / "snap.json"
    snap_file.write_text(
        json.dumps(
            {
                "rewindSnapshotVersion": 1,
                "takenAt": iso(session.start_time),
                "region": "eu-west-1",
                "entries": [],
            }
        )
    )

    code, _, err = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m", "--snapshot", str(snap_file)],
        session, capsys,
    )

    assert code == EXIT_USAGE
    assert "taken in eu-west-1" in err


def test_plan_with_use_config_consults_aws_config(session, capsys):
    from datetime import timedelta

    from test_phase4_sources import FakeConfig, config_item

    client = FakeConfig(
        items={
            INSTANCE_B: [
                config_item(session.start_time - timedelta(days=200), {"instanceType": "t3.nano"})
            ]
        }
    )

    code, out, _ = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m", "--use-config"],
        session, capsys, config_client=client,
    )

    assert code == EXIT_OK
    assert "config-history" in out
    assert "t3.nano -> t3.small" in out
    assert client.queries, "AWS Config should have been queried"


def test_plan_without_use_config_never_touches_aws_config(session, capsys):
    from test_phase4_sources import FakeConfig

    client = FakeConfig()

    code, _, _ = run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m"], session, capsys,
        config_client=client,
    )

    assert code == EXIT_OK
    assert client.queries == []


def test_a_set_value_flows_through_to_revert(session, capsys, tmp_path):
    """The whole point: an asserted value makes the chain revertible end to end."""
    from conftest import live_world_after_session

    plan_file = tmp_path / "plan.json"
    run_with_config(
        ["plan", "--identity", "perf-agent", "--since", "20m",
         "--set", "%s.instanceType=t3.nano" % INSTANCE_B, "-o", str(plan_file)],
        session, capsys,
    )

    world = live_world_after_session()
    world.allow_writes = True
    code, out, _ = run_diff(
        ["revert", str(plan_file), "--confirm", "--no-wait"], session, capsys, world
    )

    assert code == EXIT_OK
    assert world.instance_types[INSTANCE_B] == "t3.nano"
    assert "REVERTED=5" in out


# -- scan without --identity ------------------------------------------------


def test_scan_without_an_identity_summarises_by_identity(session, capsys):
    """The unscoped default answers "who", not "what": one row per identity, counted.

    Listing every change cannot answer it. Live, a 35-minute window held 306 changes of which
    286 were SSM agent heartbeats - the twelve a human made were invisible. PLUGIN-BACKED is
    the column that sorts the signal to the top.
    """
    code, out, _ = run(["scan", "--since", "90m"], session, capsys)

    assert code == EXIT_OK
    assert "(all - no --identity given)" in out
    assert "PLUGIN-BACKED" in out
    assert "TIME" not in out, "the summary must not fall back to a row per change"
    assert "--detail" in out, "the way to the detail must be printed, not guessed"


def test_scan_detail_lists_every_change_with_its_identity(session, capsys):
    """The fixture holds a change by a different identity inside the window.

    A scoped scan correctly hides it; --detail must show it, with the identity as a column -
    otherwise the rows do not say whose they are.
    """
    code, out, _ = run(["scan", "--since", "90m", "--detail"], session, capsys)

    assert code == EXIT_OK
    assert "IDENTITY" in out and "TIME" in out
    assert "PLUGIN-BACKED" not in out

    _, scoped, _ = run(["scan", "--identity", "perf-agent", "--since", "90m"], session, capsys)
    assert "IDENTITY" not in scoped, "a scoped scan already names the identity in its header"
    assert out.count("\n") > scoped.count("\n"), "unfiltered detail must show more"


def test_the_summary_puts_the_actionable_identity_first(session, capsys):
    """Ordering is the whole feature: plugin-backed changes first, not most changes."""
    from rewind.pipeline import scan as run_scan

    result = run_scan(session.source(), session.query(""))
    rows = result.by_identity

    assert len(rows) > 1, "the fixture has changes by more than one identity"
    assert rows[0].plugin_backed > 0
    assert [r.plugin_backed for r in rows] == sorted(
        (r.plugin_backed for r in rows), reverse=True
    )
    assert sum(r.changes for r in rows) == len(result.mutations), "every change is counted once"
    assert all(r.resources > 0 for r in rows)


def test_scan_without_an_identity_lists_who_to_ask_about_next(session, capsys):
    _, out, _ = run(["scan", "--since", "90m", "--output", "json"], session, capsys)
    body = json.loads(out)

    assert body["query"]["identity"] is None, "an absent filter must not read as an identity"
    assert len(body["identities"]) > 1, "the fixture has changes by more than one identity"
    assert body["otherIdentities"] == [], "nothing is out of scope when nothing is filtered"


def test_plan_still_requires_an_identity(session, capsys):
    """Not a convenience: an unfiltered plan would describe reverts for service-linked roles."""
    with pytest.raises(SystemExit) as caught:
        run(["plan", "--since", "90m"], session, capsys)

    assert caught.value.code == EXIT_USAGE
    assert "--identity" in capsys.readouterr().err


def test_the_identity_list_says_how_many_it_is_not_showing(session, capsys):
    """Silent truncation is the bug this replaced: the table used to cut off at five."""
    from rewind.report.scan import IDENTITY_LIMIT, _identity_list

    labels = ["role/session-%02d" % n for n in range(IDENTITY_LIMIT + 4)]

    rendered = _identity_list(labels)

    assert "+4 more" in rendered
    assert labels[IDENTITY_LIMIT - 1] in rendered
    assert labels[IDENTITY_LIMIT] not in rendered
    assert _identity_list(labels[:IDENTITY_LIMIT]) == ", ".join(labels[:IDENTITY_LIMIT])


def test_an_arn_is_shortened_from_the_middle_so_rows_stay_distinguishable():
    """Six SSM roles differ only in their trailing instance id.

    Truncating from the right rendered all six identically - a table hiding exactly what it
    was printed to show. The account prefix carries no information and is dropped entirely.
    """
    from rewind.report.scan import _caller

    labels = [
        "arn:aws:sts::111122223333:assumed-role/"
        "CloudAWSSystemsManagerDefaultEC2InstanceManagementRole/%s" % instance
        for instance in ("i-0abc11112222aaaa1", "i-0def33334444bbbb2")
    ]

    rendered = [_caller(label, 46) for label in labels]

    assert len(set(rendered)) == 2, "the distinguishing tail must survive"
    assert all(len(r) <= 46 for r in rendered)
    assert all(r.endswith(("i-0abc11112222aaaa1", "i-0def33334444bbbb2")) for r in rendered)
    assert not any("111122223333" in r for r in rendered), "the account prefix is noise"
    assert _caller("arn:aws:sts::1:assumed-role/Admin/alice", 46) == "Admin/alice"
    assert _caller(None, 46) == "(unknown)"


def test_elide_keeps_both_ends_and_never_exceeds_the_width():
    from rewind.report.table import elide

    assert elide("short", 20) == "short"
    assert elide("abcdefghij", 5) == "ab…ij"
    assert len(elide("x" * 200, 12)) == 12
    assert elide("abcdef", 1) == "…"


def test_a_valueless_change_is_never_listed_as_work_outstanding(mixed, capsys, tmp_path):
    """Seen live: a revert where everything possible succeeded still exited 3.

    Six of the eight rows were StopInstances/StartInstances/RunInstances - calls whose value
    CloudTrail never records. No ``--set``, no manual command, nothing an operator can ever do
    changes them.

    This fixture does still exit 3, and correctly: its other skipped chains have known values
    and could be reverted with ``--set``. That difference is the whole point, so the test
    asserts the separation rather than the code.
    """
    from conftest import live_world_after_session
    from rewind.cli import EXIT_CONFLICT, EXIT_OK

    plan_path = str(tmp_path / "mixed.json")
    assert run(
        ["plan", "--identity", mixed.identity, "--since", "90m", "-o", plan_path],
        mixed,
        capsys,
    )[0] == EXIT_OK

    code, out, _ = run_diff(
        ["revert", plan_path, "--confirm", "--no-wait", "--exit-code"],
        mixed,
        capsys,
        live_world_after_session(),
    )

    assert code == EXIT_CONFLICT, "this fixture has fields --set could still rescue"

    attention, _, scope = out.partition("nothing an operator can do")
    assert scope, "valueless changes must get their own section"
    assert "StopInstances" in scope, "the valueless change belongs there"
    assert "StopInstances" not in attention.rsplit("still need attention", 1)[-1], (
        "it must not also be listed as a to-do"
    )
