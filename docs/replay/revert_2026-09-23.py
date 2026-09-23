import warnings, sys; warnings.filterwarnings('ignore')
sys.path[:0] = ["src", "tests"]
from conftest import FakeAws
from rewind.pipeline import Reverter
from rewind.report import render_revert
from rewind.store.plan import load
from rewind.handlers.aws.ec2_instance_attribute import ATTRIBUTES

LIVE = "i-0fff11112222fff06", "i-0aaa11112222aaa01"
GONE = "i-0ddd77778888ddd04", "i-0bbb33334444bbb02", "i-0eee99990000eee05", "i-0ccc55556666ccc03"


class LaggingAws(FakeAws):
    """The state at 21:00:56, with DescribeInstances lagging UnmonitorInstances by one read.

    The lag is what produced the contradictory row in the recorded run, so the replay keeps
    it: without it neither the bug nor the fix is exercised.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.lagging = {}

    def _write_unmonitor_instances(self, kwargs):
        for i in kwargs["InstanceIds"]:
            self.lagging[i] = self.monitoring.get(i)
            self.monitoring[i] = "disabled"

    def _read_describe_instances(self, kwargs):
        for i in list(kwargs.get("InstanceIds") or []):
            if i in self.lagging:
                landed, self.monitoring[i] = self.monitoring[i], self.lagging.pop(i)
                out = super()._read_describe_instances(kwargs)
                self.monitoring[i] = landed
                return out
        return super()._read_describe_instances(kwargs)


world = LaggingAws(
    instance_types={i: "t3.small" for i in LIVE + GONE},
    monitoring={i: "enabled" for i in LIVE + GONE},
    power={**{i: "running" for i in LIVE}, **{i: "terminated" for i in GONE}},
    concurrency={"rewind-demo-checkout-260923202643:live": 1},
    multi_az={"rewind-demo-payments-260923202643": True},
    allow_writes=True,
    rds_is_async=True,
)
for i in LIVE + GONE:
    for a in ATTRIBUTES:
        world.instance_attributes[(i, a.name)] = (
            "t3.small" if a.name == "instanceType" else (False if a.boolean else "stop")
        )

run = Reverter(clients=world, dry_run=False, wait=False).run(load("/tmp/p.json"))
print(render_revert(run))
print("\n### writes actually issued")
for name in world.write_api_names():
    print("  ", name)
