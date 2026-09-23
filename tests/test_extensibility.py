"""Adding support for a new operation: the extension path, exercised rather than claimed.

The long-term goal is to revert any set of changes. Today four fields have plugins, and the
rest are handled generically. So the property that actually matters is: **how much work is
one more field, and does it slot in without touching anything else?**

This file answers that by defining a complete, working plugin inline and registering it, so
the cost is visible in the diff rather than asserted in prose. The field it covers -
``backupRetentionPeriod`` - is one the `mixed` fixture currently reports at DISCOVERED, so
the test also pins the upgrade path: DISCOVERED -> AUTO, with nothing else changed.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import pytest

from rewind.pipeline.diff import Verdict, diff_plan
from rewind.trail import CloudTrailEvent, dig
from rewind.domain import Capability, Confidence, GENERIC_HANDLER
from rewind.handlers import Verification, PLUGINS, register_plugin
from rewind.handlers.base import BaseHandler
from rewind.handlers.protocols import MISMATCH, PENDING, VERIFIED, performed, step
from rewind.store.plan import load as load_plan
from rewind.pipeline import plan as build_plan
from rewind.pipeline.revert import Outcome, Reverter

DB = "rewind-demo-db"
FIELD = "backupRetentionPeriod"


# ---------------------------------------------------------------------------
# A complete plugin. This is the whole cost of supporting one more field.
# ---------------------------------------------------------------------------


class RdsBackupRetentionOperation(BaseHandler):
    """RDS backup retention period, as a worked example of the plugin contract."""

    name = "SET_RDS_BACKUP_RETENTION"
    resource_type = "AWS::RDS::DBInstance"
    field_name = FIELD
    mutating_event_names = frozenset({"ModifyDBInstance"})
    relevant_event_names = frozenset({"ModifyDBInstance", "CreateDBInstance"})
    asynchronous = True
    config_resource_type = "AWS::RDS::DBInstance"

    # -- 1. which changes are mine -----------------------------------------

    def claimed_paths(self, event: CloudTrailEvent) -> FrozenSet[Tuple[str, ...]]:
        return frozenset({(FIELD,)})

    def parse(self, event: CloudTrailEvent) -> List[Any]:
        if event.event_name not in self.mutating_event_names or not event.successful:
            return []
        value = event.request_parameters.get(FIELD)
        identifier = event.request_parameters.get("dBInstanceIdentifier")
        if value is None or not isinstance(identifier, str):
            return []
        return [self._mutation(event, identifier, str(int(value)))]

    # -- 2. where the old value comes from ---------------------------------

    def response_anchor(self, mutation) -> Optional[str]:
        """RDS echoes the pre-change value, so this field needs no history at all."""
        current = mutation.response_elements.get(FIELD)
        pending = dig(mutation.response_elements, "pendingModifiedValues", FIELD)
        if current is None:
            return None
        if str(int(current)) != mutation.after or pending is None:
            return str(int(current))
        return None

    def anchor_from_event(self, event: CloudTrailEvent, resource_id: str) -> Optional[str]:
        if event.request_parameters.get("dBInstanceIdentifier") != resource_id:
            return None
        value = event.request_parameters.get(FIELD)
        return None if value is None else str(int(value))

    def creation_event_names(self) -> FrozenSet[str]:
        return frozenset({"CreateDBInstance"})

    def anchor_from_creation(
        self, event: CloudTrailEvent, resource_id: str
    ) -> Optional[str]:
        return self.anchor_from_event(event, resource_id)

    def value_from_config(self, configuration: Dict[str, Any]) -> Optional[str]:
        value = configuration.get(FIELD)
        return None if value is None else str(int(value))

    # -- 3. reading live state ---------------------------------------------

    def _describe(self, clients: Any, resource_id: str) -> Dict[str, Any]:
        response = clients.client("rds").describe_db_instances(
            DBInstanceIdentifier=resource_id
        )
        for instance in response.get("DBInstances", []):
            return instance
        raise self.not_readable(resource_id, "db instance not found")

    def read_live_value(self, clients: Any, resource_id: str) -> str:
        instance = self._describe(clients, resource_id)
        pending = dig(instance, "PendingModifiedValues", "BackupRetentionPeriod")
        applied = instance.get("BackupRetentionPeriod")
        effective = applied if pending is None else pending
        if effective is None:
            raise self.not_readable(resource_id, "no backup retention reported")
        return str(int(effective))

    # -- 4. performing and verifying the revert ----------------------------

    def revert_plan(self, resource_id: str, target_value: str) -> Dict[str, Any]:
        return {
            "operation": self.name,
            "parameters": {
                "dBInstanceIdentifier": resource_id,
                "backupRetentionPeriod": int(target_value),
            },
            "asynchronous": True,
            "steps": [
                step(
                    "rds:ModifyDBInstance",
                    {
                        "DBInstanceIdentifier": resource_id,
                        "BackupRetentionPeriod": int(target_value),
                        "ApplyImmediately": True,
                    },
                )
            ],
            "verify": step(
                "rds:DescribeDBInstances",
                {"DBInstanceIdentifier": resource_id},
                expect=target_value,
            ),
        }

    def apply_revert(
        self, clients: Any, resource_id: str, target_value: str, wait: bool = True
    ) -> List[Dict[str, Any]]:
        params = {
            "DBInstanceIdentifier": resource_id,
            "BackupRetentionPeriod": int(target_value),
            "ApplyImmediately": True,
        }
        clients.client("rds").modify_db_instance(**params)
        return [performed("rds:ModifyDBInstance", params)]

    def verify_revert(
        self, clients: Any, resource_id: str, target_value: str
    ) -> Verification:
        """Returns the observed value with the verdict, so one read decides both."""
        instance = self._describe(clients, resource_id)
        pending = dig(instance, "PendingModifiedValues", "BackupRetentionPeriod")
        applied = instance.get("BackupRetentionPeriod")
        seen = None if applied is None else str(int(applied))
        if seen is not None and seen == target_value and pending is None:
            return Verification(VERIFIED, seen)
        if pending is not None and str(int(pending)) == target_value:
            return Verification(PENDING, seen)
        return Verification(MISMATCH, seen)


@pytest.fixture()
def with_new_plugin():
    """Register the plugin for the duration of one test, the way a real one would be."""
    plugin = register_plugin(RdsBackupRetentionOperation())
    try:
        yield plugin
    finally:
        PLUGINS.remove(plugin)


def plan_mixed(mixed):
    return build_plan(source=mixed.source(), query=mixed.query(), now=mixed.end_time)


def chain_for(result, field_name=FIELD):
    return next(c for c in result.chains if c.field_name == field_name)


# ---------------------------------------------------------------------------
# the upgrade path
# ---------------------------------------------------------------------------


def test_before_the_plugin_the_field_is_generic_and_unprovable(mixed):
    chain = chain_for(plan_mixed(mixed))

    assert chain.handler == GENERIC_HANDLER
    assert chain.capability is Capability.DISCOVERED
    assert chain.net_before is None


def test_registering_the_plugin_lifts_the_same_field_to_auto(mixed, with_new_plugin):
    """One file plus one registry line, and nothing else in the tool changes."""
    chain = chain_for(plan_mixed(mixed))

    assert chain.handler == "SET_RDS_BACKUP_RETENTION"
    assert chain.capability is Capability.AUTO
    # And the plugin's own responseElements knowledge gives it a proven old value.
    assert chain.net_before == "7"
    assert chain.net_after == "30"
    assert chain.confidence is Confidence.HIGH
    assert chain.anchor.source == "response-elements"


def test_the_new_plugin_does_not_disturb_the_other_chains(mixed, with_new_plugin):
    before = {
        (c.resource_id, c.field_name): (c.handler, c.capability, c.net_before)
        for c in plan_mixed(mixed).chains
    }
    # Re-planning with the plugin registered must change exactly one row.
    PLUGINS.remove(with_new_plugin)
    baseline = {
        (c.resource_id, c.field_name): (c.handler, c.capability, c.net_before)
        for c in plan_mixed(mixed).chains
    }
    PLUGINS.append(with_new_plugin)

    changed = {k for k in baseline if baseline[k] != before[k]}
    assert changed == {(DB, FIELD)}
    assert set(baseline) == set(before)  # no field appeared or vanished


def test_the_field_is_reported_exactly_once(mixed, with_new_plugin):
    """A plugin claiming a field must not leave the generic layer reporting it too."""
    keys = [(c.resource_id, c.field_name) for c in plan_mixed(mixed).chains]

    assert len(keys) == len(set(keys))
    assert keys.count((DB, FIELD)) == 1


def test_forgetting_claimed_paths_still_does_not_duplicate_the_field(mixed):
    """The safety net: a plain scalar field is deduped by chain key even without a claim."""

    class Forgetful(RdsBackupRetentionOperation):
        name = "SET_RDS_BACKUP_RETENTION_FORGETFUL"

        def claimed_paths(self, event):
            return frozenset()  # the mistake a plugin author will make

    plugin = register_plugin(Forgetful())
    try:
        keys = [(c.resource_id, c.field_name) for c in plan_mixed(mixed).chains]
        assert keys.count((DB, FIELD)) == 1
        assert chain_for(plan_mixed(mixed)).handler == plugin.name
    finally:
        PLUGINS.remove(plugin)


# ---------------------------------------------------------------------------
# the new field is fully functional, not just labelled AUTO
# ---------------------------------------------------------------------------


def rds_world(retention=30, pending=None, allow_writes=False):
    from conftest import FakeAws

    world = FakeAws(multi_az={DB: True}, allow_writes=allow_writes)
    original = world._read_describe_db_instances

    def read(kwargs):
        body = original(kwargs)
        instance = body["DBInstances"][0]
        instance["BackupRetentionPeriod"] = world.retention
        if world.retention_pending is not None:
            instance.setdefault("PendingModifiedValues", {})
            instance["PendingModifiedValues"]["BackupRetentionPeriod"] = (
                world.retention_pending
            )
        return body

    def write(kwargs):
        if "BackupRetentionPeriod" in kwargs:
            world.retention = kwargs["BackupRetentionPeriod"]
        if "MultiAZ" in kwargs:
            world.multi_az[kwargs["DBInstanceIdentifier"]] = kwargs["MultiAZ"]

    world.retention = retention
    world.retention_pending = pending
    world._read_describe_db_instances = read
    world._write_modify_db_instance = write
    return world


def test_diff_can_now_check_the_new_field(mixed, with_new_plugin, tmp_path):
    """Before the plugin this row was UNCHECKABLE: no Describe call existed for it."""
    import json

    result = plan_mixed(mixed)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    plan = load_plan(str(path))

    diff = diff_plan(plan, rds_world(retention=30), now=mixed.end_time)

    entry = next(e for e in diff.entries if e.chain.field_name == FIELD)
    assert entry.verdict is Verdict.REVERTIBLE
    assert entry.live_value == "30"
    # The field with no plugin still reports honestly that it cannot be checked.
    unchecked = next(e for e in diff.entries if e.chain.field_name == "memorySize")
    assert unchecked.verdict is Verdict.UNCHECKABLE


def test_revert_can_now_execute_the_new_field(mixed, with_new_plugin, tmp_path):
    import json

    result = plan_mixed(mixed)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    plan = load_plan(str(path))
    target = next(c for c in plan.chains if c.field_name == FIELD)
    world = rds_world(retention=30, allow_writes=True)

    run = Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=mixed.end_time
    )

    assert run.results[0].outcome is Outcome.REVERTED
    assert run.results[0].verification == VERIFIED
    assert world.retention == 7
    assert world.write_api_names() == ["modify_db_instance"]


def test_the_new_field_respects_the_dry_run_gate(mixed, with_new_plugin, tmp_path):
    import json

    result = plan_mixed(mixed)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    plan = load_plan(str(path))
    target = next(c for c in plan.chains if c.field_name == FIELD)
    world = rds_world(retention=30, allow_writes=False)  # any write raises

    run = Reverter(clients=world, dry_run=True).run(
        plan, only=[target.chain_id], now=mixed.end_time
    )

    assert run.results[0].outcome is Outcome.DRY_RUN
    assert world.retention == 30


def test_the_new_fields_async_verification_reports_submitted(mixed, with_new_plugin, tmp_path):
    """The plugin's own verify_revert is honoured, exactly as the shipped RDS one is."""
    import json

    result = plan_mixed(mixed)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    plan = load_plan(str(path))
    target = next(c for c in plan.chains if c.field_name == FIELD)
    world = rds_world(retention=30, allow_writes=True)

    # Make the write land as pending rather than applied, the way RDS really behaves.
    def pending_write(kwargs):
        world.retention_pending = kwargs["BackupRetentionPeriod"]

    world._write_modify_db_instance = pending_write

    run = Reverter(clients=world, dry_run=False, wait=False).run(
        plan, only=[target.chain_id], now=mixed.end_time
    )

    assert run.results[0].outcome is Outcome.SUBMITTED
    assert run.results[0].verification == PENDING
