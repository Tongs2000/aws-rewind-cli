"""Test helpers: sanitized fixture loading and a fake CloudTrail client.

No credentials and no network are ever needed. The fake CloudTrail client also asserts
the two query rules the tool relies on: at most one lookup attribute per call, and never
the ``ResourceName`` index.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rewind.trail import StaticEventSource  # noqa: E402
from rewind.domain import parse_time  # noqa: E402
from rewind.trail import from_lookup_record  # noqa: E402
from rewind.domain import Query  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cloudtrail"

INSTANCE_A = "i-0aaa000000000000a"
INSTANCE_B = "i-0bbb000000000000b"
FUNCTION_ALIAS = "rewind-demo-fn:live"
DB_INSTANCE = "rewind-demo-db"


def as_wire_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """CloudTrail returns CloudTrailEvent as a JSON string; fixtures nest an object."""
    wire = dict(record)
    if isinstance(wire.get("CloudTrailEvent"), dict):
        wire["CloudTrailEvent"] = json.dumps(wire["CloudTrailEvent"])
    return wire


class Fixture:
    def __init__(self, path: Path) -> None:
        self.raw = json.loads(path.read_text())
        self.records = [as_wire_record(r) for r in self.raw["events"]]
        self.events = [from_lookup_record(r) for r in self.records]

    @property
    def identity(self) -> str:
        return self.raw["identity"]

    @property
    def start_time(self):
        return parse_time(self.raw["window"]["startTime"])

    @property
    def end_time(self):
        return parse_time(self.raw["window"]["endTime"])

    @property
    def region(self) -> str:
        return self.raw["window"].get("region", "us-west-1")

    def query(self, identity: Optional[str] = None) -> Query:
        """``None`` means "the fixture's own identity"; ``""`` means "do not filter"."""
        return Query(
            identity=self.identity if identity is None else identity,
            start_time=self.start_time,
            end_time=self.end_time,
            region=self.region,
        )

    def event(self, fragment: str):
        """Look an event up by any fragment of its id, e.g. ``fixture.event("h0000003")``."""
        for event in self.events:
            if fragment in event.event_id:
                return event
        raise KeyError(fragment)

    def source(self, truncated: bool = False) -> StaticEventSource:
        return StaticEventSource(self.events, truncated=truncated)

    def without(self, *fragments: str) -> StaticEventSource:
        """A source with the named events removed, for negative cases."""
        kept = [e for e in self.events if not any(f in e.event_id for f in fragments)]
        return StaticEventSource(kept)

    def wire_records(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self.records)


def load_fixture(name: str) -> Fixture:
    return Fixture(FIXTURES / name)


@pytest.fixture()
def session() -> Fixture:
    return load_fixture("agent_session.json")


@pytest.fixture()
def mixed() -> Fixture:
    """A session whose changes mostly have no plugin - the generality case."""
    return load_fixture("mixed_session.json")


class FakeCloudTrail:
    """Stands in for a boto3 cloudtrail client, with pagination."""

    def __init__(self, records: List[Dict[str, Any]], page_size: int = 50) -> None:
        self.records = records
        self.page_size = page_size
        self.queries: List[Dict[str, Any]] = []

    def lookup_events(self, **kwargs: Any) -> Dict[str, Any]:
        self.queries.append(kwargs)
        attributes = kwargs.get("LookupAttributes") or []
        assert len(attributes) <= 1, "LookupEvents accepts at most one lookup attribute"
        for attribute in attributes:
            assert attribute["AttributeKey"] != "ResourceName", (
                "the tool must not depend on the CloudTrail ResourceName index"
            )
        wanted_name = attributes[0]["AttributeValue"] if attributes else None

        start, end = kwargs["StartTime"], kwargs["EndTime"]
        matched = []
        for record in self.records:
            event = from_lookup_record(record)
            if wanted_name is not None and event.event_name != wanted_name:
                continue
            if not (start <= event.event_time <= end):
                continue
            matched.append(record)

        offset = int(kwargs.get("NextToken") or 0)
        page = matched[offset : offset + self.page_size]
        response: Dict[str, Any] = {"Events": page}
        if offset + self.page_size < len(matched):
            response["NextToken"] = str(offset + self.page_size)
        return response

    @property
    def attribute_keys(self) -> List[str]:
        keys = []
        for query in self.queries:
            for attribute in query.get("LookupAttributes") or []:
                keys.append(attribute["AttributeKey"])
        return keys


# ---------------------------------------------------------------------------
# Fake AWS, for the read-only live-state calls `diff` makes.
#
# Every mutating API raises. `diff` must never call one, so a regression that
# introduces a write fails loudly instead of quietly succeeding.
# ---------------------------------------------------------------------------

WRITE_APIS = frozenset(
    {
        "monitor_instances",
        "unmonitor_instances",
        "stop_instances",
        "start_instances",
        "modify_instance_attribute",
        "put_provisioned_concurrency_config",
        "delete_provisioned_concurrency_config",
        "modify_db_instance",
    }
)


class WriteAttempted(AssertionError):
    """Raised when a read-only code path calls a mutating API."""


class ProvisionedConcurrencyConfigNotFoundException(Exception):
    """Name matches the boto3 exception class the Lambda operation looks for."""


class FakeWaiter:
    def __init__(self, world: "FakeAws", name: str) -> None:
        self.world = world
        self.name = name

    def wait(self, **kwargs: Any) -> None:
        self.world.waiters.append(self.name)


class FakeClient:
    def __init__(self, service: str, world: "FakeAws") -> None:
        self.service = service
        self.world = world

    def __getattr__(self, name: str):
        def call(**kwargs: Any) -> Dict[str, Any]:
            world = self.world
            world.calls.append((self.service, name, kwargs))
            if name in WRITE_APIS:
                if not world.allow_writes:
                    raise WriteAttempted(
                        "%s:%s was called, but this code path must be read-only"
                        % (self.service, name)
                    )
                writer = getattr(world, "_write_" + name, None)
                if writer is None:
                    raise KeyError("no fake for write API %s:%s" % (self.service, name))
                if world.fail_write == name:
                    world.fail_write = None
                    raise RuntimeError("ServiceException: %s is unavailable" % name)
                writer(kwargs)
                # Recorded only once the call took effect, so `writes` never contains a
                # mutation that raised.
                world.writes.append((self.service, name, kwargs))
                return {}
            reader = getattr(world, "_read_" + name, None)
            if reader is None:
                raise KeyError("no fake for %s:%s" % (self.service, name))
            return reader(kwargs)

        return call

    def get_waiter(self, name: str) -> FakeWaiter:
        return FakeWaiter(self.world, name)


class FakeAws:
    """Read-only stand-in for :class:`rewind.aws.AwsClients`."""

    def __init__(
        self,
        instance_types: Optional[Dict[str, str]] = None,
        monitoring: Optional[Dict[str, str]] = None,
        power: Optional[Dict[str, str]] = None,
        concurrency: Optional[Dict[str, int]] = None,
        multi_az: Optional[Dict[str, bool]] = None,
        rds_pending: Optional[Dict[str, bool]] = None,
        region: str = "us-west-1",
        allow_writes: bool = False,
        rds_is_async: bool = False,
    ) -> None:
        self.region = region
        self.allow_writes = allow_writes
        self.rds_is_async = rds_is_async
        self.instance_types = dict(instance_types or {})
        self.monitoring = dict(monitoring or {})
        self.power = dict(power or {})
        self.concurrency = dict(concurrency or {})
        self.multi_az = dict(multi_az or {})
        self.rds_pending = dict(rds_pending or {})
        #: (instanceId, attributeName) -> value, for ModifyInstanceAttribute attributes
        self.instance_attributes: Dict[Any, Any] = {}
        self.calls: List[Any] = []
        self.writes: List[Any] = []
        self.waiters: List[str] = []
        #: set to a write API name to make that one call raise once
        self.fail_write: Optional[str] = None

    def client(self, service: str) -> FakeClient:
        return FakeClient(service, self)

    def api_names(self) -> List[str]:
        return ["%s:%s" % (service, name) for service, name, _ in self.calls]

    def write_api_names(self) -> List[str]:
        return [name for _, name, _ in self.writes]

    # -- reads --------------------------------------------------------------

    def _read_describe_instance_attribute(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = kwargs["InstanceId"]
        if instance_id not in self.instance_types:
            raise KeyError("InvalidInstanceID.NotFound: %s" % instance_id)
        attribute = kwargs.get("Attribute", "instanceType")
        if attribute == "instanceType":
            return {"InstanceType": {"Value": self.instance_types[instance_id]}}
        from rewind.handlers.aws.ec2_instance_attribute import api_name

        value = self.instance_attributes.get((instance_id, attribute))
        if value is None:
            raise KeyError("InvalidParameterValue: %s" % attribute)
        return {api_name(attribute): {"Value": value}}

    def _read_describe_instances(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        instances = []
        for instance_id in kwargs.get("InstanceIds", []):
            if instance_id not in self.monitoring and instance_id not in self.instance_types:
                continue  # unknown instance: reported as absent, like EC2 would
            instances.append(
                {
                    "InstanceId": instance_id,
                    "InstanceType": self.instance_types.get(instance_id, "t3.micro"),
                    "Monitoring": {"State": self.monitoring.get(instance_id, "disabled")},
                    "State": {"Name": self.power.get(instance_id, "running")},
                }
            )
        return {"Reservations": [{"Instances": instances}]}

    def _read_get_provisioned_concurrency_config(
        self, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        key = "%s:%s" % (kwargs["FunctionName"], kwargs["Qualifier"])
        if key not in self.concurrency:
            raise ProvisionedConcurrencyConfigNotFoundException(key)
        return {
            "RequestedProvisionedConcurrentExecutions": self.concurrency[key],
            "Status": "READY",
        }

    def _read_describe_db_instances(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        identifier = kwargs["DBInstanceIdentifier"]
        if identifier not in self.multi_az:
            raise KeyError("DBInstanceNotFound: %s" % identifier)
        body: Dict[str, Any] = {
            "DBInstanceIdentifier": identifier,
            "MultiAZ": self.multi_az[identifier],
            "DBInstanceStatus": "modifying" if identifier in self.rds_pending else "available",
        }
        if identifier in self.rds_pending:
            body["PendingModifiedValues"] = {"MultiAZ": self.rds_pending[identifier]}
        return {"DBInstances": [body]}


    # -- writes, only reachable when allow_writes=True -----------------------

    def _write_stop_instances(self, kwargs: Dict[str, Any]) -> None:
        for instance_id in kwargs["InstanceIds"]:
            self.power[instance_id] = "stopped"

    def _write_start_instances(self, kwargs: Dict[str, Any]) -> None:
        for instance_id in kwargs["InstanceIds"]:
            self.power[instance_id] = "running"

    def _write_modify_instance_attribute(self, kwargs: Dict[str, Any]) -> None:
        instance_id = kwargs["InstanceId"]
        if "InstanceType" in kwargs:
            self.instance_types[instance_id] = kwargs["InstanceType"]["Value"]
            return
        from rewind.handlers.aws.ec2_instance_attribute import ATTRIBUTES, api_name

        for attribute in ATTRIBUTES:
            key = api_name(attribute.name)
            if key in kwargs:
                self.instance_attributes[(instance_id, attribute.name)] = kwargs[key]["Value"]

    def _write_monitor_instances(self, kwargs: Dict[str, Any]) -> None:
        for instance_id in kwargs["InstanceIds"]:
            self.monitoring[instance_id] = "enabled"

    def _write_unmonitor_instances(self, kwargs: Dict[str, Any]) -> None:
        for instance_id in kwargs["InstanceIds"]:
            self.monitoring[instance_id] = "disabled"

    def _write_put_provisioned_concurrency_config(self, kwargs: Dict[str, Any]) -> None:
        key = "%s:%s" % (kwargs["FunctionName"], kwargs["Qualifier"])
        self.concurrency[key] = kwargs["ProvisionedConcurrentExecutions"]

    def _write_delete_provisioned_concurrency_config(self, kwargs: Dict[str, Any]) -> None:
        self.concurrency.pop("%s:%s" % (kwargs["FunctionName"], kwargs["Qualifier"]), None)

    def _write_modify_db_instance(self, kwargs: Dict[str, Any]) -> None:
        identifier = kwargs["DBInstanceIdentifier"]
        if self.rds_is_async:
            # Like the real thing: the applied value does not move until it converges.
            self.rds_pending[identifier] = kwargs["MultiAZ"]
        else:
            self.multi_az[identifier] = kwargs["MultiAZ"]

    def settle_rds(self, identifier: str) -> None:
        """Simulate RDS finishing an asynchronous modification."""
        if identifier in self.rds_pending:
            self.multi_az[identifier] = self.rds_pending.pop(identifier)

def live_world_after_session() -> FakeAws:
    """Live state exactly as the fixture's session left it."""
    world = FakeAws(
        instance_types={INSTANCE_A: "t3.small", INSTANCE_B: "t3.small"},
        monitoring={INSTANCE_A: "enabled", INSTANCE_B: "enabled"},
        power={INSTANCE_A: "running", INSTANCE_B: "running"},
        concurrency={FUNCTION_ALIAS: 5},
        multi_az={DB_INSTANCE: True},
    )
    # The EC2 attribute plugins each read their own attribute, so give the instances a
    # settled value for every one of them.
    from rewind.handlers.aws.ec2_instance_attribute import ATTRIBUTES

    for instance_id in (INSTANCE_A, INSTANCE_B):
        for attribute in ATTRIBUTES:
            world.instance_attributes[(instance_id, attribute.name)] = (
                False if attribute.boolean else "stop"
            )
    return world
