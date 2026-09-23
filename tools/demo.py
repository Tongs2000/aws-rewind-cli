"""Offline demo: the fixture, the whole sequence, no credentials and no network.

The AWS seam is `rewind.cli.context`; setting an attribute on `rewind.cli` itself silently
does nothing, which is how this target came to be reaching the real API while advertising
that it did not.
"""

import sys

sys.path[:0] = ["src", "tests"]

from conftest import load_fixture, live_world_after_session  # noqa: E402
from rewind.cli import context, main  # noqa: E402

fixture = load_fixture("agent_session.json")
context.source = lambda region, args: fixture.source()
context.clients = lambda region, args: live_world_after_session()

raise SystemExit(
    main(
        [
            "undo",
            "--identity",
            "perf-agent",
            "--start",
            fixture.start_time.isoformat(),
            "--end",
            fixture.end_time.isoformat(),
            "--region",
            "us-west-1",
        ],
        now=fixture.end_time,
    )
)
