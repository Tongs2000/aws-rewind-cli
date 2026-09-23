"""`rewind undo`: the whole sequence in one command, with the review step intact.

The risk in a one-shot command is that convenience quietly becomes a different safety model.
These tests hold it to the same one as `revert`: nothing is called without `--confirm`, a
conflicted field is refused per field rather than aborting the run, and the plan lands on disk
either way so the run stays auditable.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    DB_INSTANCE,
    FUNCTION_ALIAS,
    INSTANCE_A,
    WriteAttempted,
    live_world_after_session,
)
from rewind.cli import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE, main
from rewind.pipeline import undo
from rewind.pipeline.diff import Verdict
from rewind.pipeline.revert import Outcome
from rewind.report import render_undo


def run_undo(session, world, **kwargs):
    return undo(
        source=session.source(),
        clients=world,
        query=session.query(),
        now=session.end_time,
        **kwargs
    )


# -- the safety model is the same one revert has ------------------------------


def test_a_dry_run_calls_nothing(session):
    """The fake client raises on any write unless a test opts in, so this cannot pass vacuously."""
    world = live_world_after_session()

    run = run_undo(session, world)

    assert run.dry_run is True
    assert run.changed_anything is False
    assert world.writes == [], "a dry run must not write"
    assert world.calls, "but it must have read something"
    assert all(
        r.outcome in (Outcome.DRY_RUN, Outcome.SKIPPED) for r in run.revert.results
    )


def test_confirm_applies_and_the_diff_still_ran_first(session):
    world = live_world_after_session()
    world.allow_writes = True

    run = run_undo(session, world, confirm=True, wait=False)

    assert run.dry_run is False
    assert run.changed_anything is True
    assert world.write_api_names(), "something must actually have been called"
    # The diff is not optional in a confirmed run: it is the only thing that can say
    # "somebody else has been here" before the first write.
    assert run.diff.entries, "diff must run even when confirming"
    assert run.diff.checked_at is not None


def test_the_write_guard_really_raises_for_undo(session):
    """Proves the assertion above is not vacuous."""
    world = live_world_after_session()

    with pytest.raises(WriteAttempted):
        world.client("ec2").unmonitor_instances(InstanceIds=[INSTANCE_A])


# -- a conflict is refused per field, not by aborting -------------------------


def test_one_conflicted_field_does_not_stop_the_others(session):
    """Aborting everything because one field drifted would leave an incident half-handled."""
    world = live_world_after_session()
    world.allow_writes = True
    world.instance_types[INSTANCE_A] = "t3.2xlarge"  # somebody else resized it

    run = run_undo(session, world, confirm=True, wait=False)

    conflicted = [e.chain.chain_id for e in run.diff.conflicts]
    assert conflicted, "the fixture must produce a conflict for this to mean anything"

    by_chain = {r.chain.chain_id: r for r in run.revert.results}
    for chain_id in conflicted:
        assert by_chain[chain_id].outcome is Outcome.SKIPPED
        assert by_chain[chain_id].precheck_verdict is Verdict.CONFLICT
    assert world.instance_types[INSTANCE_A] == "t3.2xlarge", "the conflict was not overwritten"

    # and the fields nobody touched were still reverted
    assert world.multi_az[DB_INSTANCE] is False
    assert FUNCTION_ALIAS not in world.concurrency


# -- output -------------------------------------------------------------------


def test_the_summary_shows_all_three_stages_and_what_the_diff_said(session):
    run = run_undo(session, live_world_after_session())
    run.plan_path = "/tmp/plan.json"

    text = render_undo(run)

    assert "1 plan" in text and "2 diff" in text and "3 revert" in text
    assert "DIFF SAID" in text, "the diff verdict belongs beside the outcome"
    assert "/tmp/plan.json" in text
    assert "DRY RUN - nothing was called" in text
    assert "None" not in text


def test_detail_prints_each_stage_in_full(session):
    run = run_undo(session, live_world_after_session())

    summary = render_undo(run)
    full = render_undo(run, detail=True)

    assert len(full) > len(summary) * 2
    for heading in ("PLAN", "DIFF", "REVERT"):
        assert heading in full
    assert "Evidence" in full, "--detail implies the plan's evidence block"


# -- the CLI wiring -----------------------------------------------------------


def patched(argv, session, capsys, world, now=None):
    from rewind.cli import context as cli_module

    original = cli_module.clients, cli_module.source
    cli_module.clients = lambda region, args: world
    cli_module.source = lambda region, args: session.source()
    try:
        code = main(argv, now=now or session.end_time)
    finally:
        cli_module.clients, cli_module.source = original
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_undo_writes_the_plan_even_in_a_dry_run(session, capsys, tmp_path):
    """A run nobody can re-check afterwards is not an audit trail."""
    plan_path = tmp_path / "plan.json"
    code, out, _ = patched(
        ["undo", "--identity", session.identity, "--since", "90m",
         "--region", session.region, "-o", str(plan_path)],
        session,
        capsys,
        live_world_after_session(),
    )

    assert code == EXIT_OK
    assert plan_path.exists(), "the plan must land on disk without --confirm"
    body = json.loads(plan_path.read_text())
    assert body["chains"], "and it must be a real plan, loadable by diff and revert"
    assert str(plan_path) in out


def test_undo_prints_the_temporary_plan_path_when_none_was_given(session, capsys):
    code, out, _ = patched(
        ["undo", "--identity", session.identity, "--since", "90m", "--region", session.region],
        session,
        capsys,
        live_world_after_session(),
    )

    assert code == EXIT_OK
    path = next(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("plan  "))
    assert path.endswith(".json") and "rewind-plan-" in path
    assert json.loads(open(path).read())["chains"], "the printed path must be usable"


def test_undo_exit_code_flags_a_conflict_for_scripts(session, capsys, tmp_path):
    world = live_world_after_session()
    world.instance_types[INSTANCE_A] = "t3.2xlarge"

    quiet, _, _ = patched(
        ["undo", "--identity", session.identity, "--since", "90m", "--region", session.region],
        session, capsys, world,
    )
    assert quiet == EXIT_OK, "no --exit-code, so a conflict is not a process failure"

    loud, _, _ = patched(
        ["undo", "--identity", session.identity, "--since", "90m",
         "--region", session.region, "--exit-code"],
        session, capsys, world,
    )
    assert loud == EXIT_CONFLICT


def test_undo_log_holds_all_three_stages(session, capsys, tmp_path):
    log = tmp_path / "undo.json"
    patched(
        ["undo", "--identity", session.identity, "--since", "90m",
         "--region", session.region, "--log", str(log)],
        session, capsys, live_world_after_session(),
    )

    body = json.loads(log.read_text())
    assert set(body) >= {"plan", "diff", "revert", "dryRun", "planPath"}
    assert body["dryRun"] is True
    assert body["revert"]["summary"]["DRY_RUN"] > 0


def test_undo_requires_an_identity(session, capsys):
    """Same boundary as plan: an unfiltered undo would revert service-linked roles' changes."""
    with pytest.raises(SystemExit) as caught:
        patched(["undo", "--since", "90m"], session, capsys, live_world_after_session())

    assert caught.value.code == EXIT_USAGE
