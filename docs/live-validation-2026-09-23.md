# Live validation, 2026-09-23

A full `scan → plan → diff → revert --confirm` run against a real AWS account
(`111122223333`, us-west-1), with four changes made by hand as the "agent":

| Resource | baseline | after the change |
|---|---|---|
| `i-0fff11112222fff06`, `i-0aaa11112222aaa01` | `t3.micro`, monitoring `disabled` | `t3.small`, monitoring `enabled` |
| `rewind-demo-checkout-260923202643:live` | no provisioned concurrency | `1` |
| `rewind-demo-payments-260923202643` | MultiAZ `false` | `true` |

## Result

```
scan   : 177 events, 228 tracked changes, 13 identities
plan   : 28 changes across 28 fields — AUTO=8, DISCOVERED=20
diff   : REVERTIBLE=8  UNCHECKABLE=20, drift none (8 of 28 compared)
dry run: DRY_RUN=8  SKIPPED=20
applied: REVERTED=4  SUBMITTED=1  SKIPPED=20  FAILED=3
```

All four anchor sources appeared in one window, which was the point of the exercise:

| Field | BEFORE → NOW | Anchor | Confidence |
|---|---|---|---|
| `multiAZ` | false → true | **`response-elements`** | **HIGH** |
| `instanceType` ×2 | t3.micro → t3.small | `creation-event` | MEDIUM |
| `monitoring` ×2 | disabled → enabled | `creation-event` | MEDIUM |
| `provisionedConcurrency` | **NONE** → 1 | `creation-event` | MEDIUM |

`multiAZ` anchoring from the change event's own `responseElements` is the only
retention-immune path in the tool, and it worked on real data:

```
anchor : false  (HIGH via response-elements)
reason : pre-change value read from the change event's own responseElements
```

`NONE` as a real value (distinct from an unproven anchor) also held up: the Lambda alias had
no provisioned concurrency config, and the plan said `NONE -> 1`, not `? -> 1`.

## What the noise looked like

`scan` with no `--identity`, 30-minute window:

```
IDENTITY                                        CHANGES  RESOURCES  PLUGIN-BACKED
----------------------------------------------  -------  ---------  -------------
Admin/alice-DevAccount                          28       10         8
CloudAWSSystemsManager…ole/i-0aaa11112222aaa01  51       1          -
CloudAWSSystemsManager…ole/i-0fff11112222fff06  51       1          -
…
```

228 changes, 13 identities, and the 8 that mattered were in one row. `RESOURCES = 1` turned
out to be a stronger signal than expected: an identity that only ever touches itself is
infrastructure, not the thing you are looking for.

## Three FAILED rows — two are defects

### 1. `monitoring` on `i-0aaa11112222aaa01`: FAILED, but `NOW` reads `disabled`

```
i-0aaa11112222aaa01  monitoring  enabled  disabled  disabled  FAILED
  the revert was issued but the field does not read 'disabled'
```

Self-contradictory: the verdict says the field is not `disabled` while the column beside it
says it is. Cause is in `Reverter._verify`, which reads live state **twice**:

```python
verification  = operation.verify_revert(...)      # first read  -> MISMATCH (still "enabled"/"disabling")
observed_after = operation.read_live_value(...)   # second read -> "disabled"
```

`UnmonitorInstances` settles in a second or two, so the two reads landed on either side of
the transition. The sibling instance, reverted moments earlier, passed. **The verdict and the
value shown must come from one read.**

### 2. `instanceType` on two *terminated* instances: `IncorrectInstanceState`

```
  the AWS call failed: ClientError: An error occurred (IncorrectInstanceState) when calling
  the StopInstances operation: This instance 'i-0ddd77778888ddd04' is not in a state from
  which it can be stopped.
```

Those two instances had been terminated before the run. The tool attempted the revert anyway,
and `diff` had reported them `REVERTIBLE` with `LIVE NOW = t3.small`.

Root cause: **`DescribeInstanceAttribute` still answers for a terminated instance**, so
`read_live_value` succeeds and `classify` sees a field that matches the session's outcome.
Nothing in the pre-check asks whether the resource still exists in a usable state — even
though `read_live_detail` already fetches `instanceState`, which would have said `terminated`.

A deleted resource is not drift and not a conflict; it is a third thing, and it should be
skipped with that reason rather than attempted.

### 3. `multiAZ` SUBMITTED — correct

```
  AWS accepted the change but it has not settled yet; re-run to poll it
```

Multi-AZ conversion is asynchronous. `SUBMITTED` is the honest outcome, and a re-run polls it.

## What worked exactly as designed

- **dry run read live state**: the `WAS` column held real values, so the dry run reflected
  what would happen now rather than replaying the plan.
- **newest change first**, strictly ordered by last change time.
- **20 SKIPPED rows never got an AWS call.** The `capability is not AUTO` gate held.
- **`out_of_scope` separated from `unfinished`**: 19 valueless changes were listed as "nothing
  to restore" and only 5 as needing attention — the distinction added earlier the same day
  after `--exit-code` returned 3 on a clean run.
- **verification caught a real mismatch** rather than trusting that the write worked; the
  bug is in how the result was rendered, not in the checking.

## Known-gap rows this surfaced

20 of 28 fields were `DISCOVERED` pseudo-fields named after their event — `RunInstances`,
`StopInstances`, `StartInstances`, `TerminateInstances`, `CreateFunction20150331`,
`CreateAlias20150331`, `CreateDBInstance`. Nothing was hidden and nothing false was claimed,
but the modelling gap is now very visible: a power transition and an existence change are not
field changes. See **Known limitations 2** in the README.

---

# After the fixes

The three defects above are fixed. The original run is left exactly as it happened - a
validation record that is edited to look like a pass is worth nothing - so the corrected
behaviour is shown by **replay** instead.

`/tmp/p.json` still holds all 28 chains, and the live state at 21:00:56 was read and recorded
at the time, so the replay is that recorded plan against that recorded world:

| | State at 21:00:56 |
|---|---|
| `i-0fff11112222fff06`, `i-0aaa11112222aaa01` | `t3.small`, running, monitoring `enabled` |
| `i-0ddd77778888ddd04`, `i-0bbb33334444bbb02` and two more | **terminated** |
| `rewind-demo-checkout-260923202643:live` | provisioned concurrency `1` |
| `rewind-demo-payments-260923202643` | MultiAZ `true`, async |

The one thing a replay cannot inherit is a millisecond-scale race, so it is modelled
explicitly: `DescribeInstances` lags `UnmonitorInstances` by exactly one read, which is what
produced the contradictory row. Without that the fix would not be exercised. The script is
`docs/replay/revert_2026-09-23.py`.

## Outcome

```
before : REVERTED=4  SUBMITTED=1  SKIPPED=20  FAILED=3
after  : REVERTED=5  SUBMITTED=1  SKIPPED=22  FAILED=0
```

```
plan       : /tmp/p.json
mode       : APPLIED
region     : us-west-1
started    : 2026-09-23T21:33:33.130585+00:00
fields     : 28  (newest change reverted first)
outcomes   : REVERTED=5  SUBMITTED=1  SKIPPED=22

RESOURCE                      FIELD                     WAS       TARGET    NOW       OUTCOME
----------------------------  ------------------------  --------  --------  --------  ---------
i-0fff11112222fff06           monitoring                enabled   disabled  disabled  REVERTED
i-0aaa11112222aaa01           monitoring                enabled   disabled  disabled  REVERTED
rewind-demo-payments-260923…  multiAZ                   true      false     true      SUBMITTED
rewind-demo-payments-260923…  allowMajorVersionUpgrade  ?         ?         ?         SKIPPED
rewind-demo-checkout-260923…  provisionedConcurrency    1         NONE      NONE      REVERTED
i-0aaa11112222aaa01           StartInstances            ?         ?         ?         SKIPPED
i-0fff11112222fff06           StartInstances            ?         ?         ?         SKIPPED
i-0aaa11112222aaa01           instanceType              t3.small  t3.micro  t3.micro  REVERTED
i-0fff11112222fff06           instanceType              t3.small  t3.micro  t3.micro  REVERTED
i-0aaa11112222aaa01           StopInstances             ?         ?         ?         SKIPPED
i-0fff11112222fff06           StopInstances             ?         ?         ?         SKIPPED
i-0eee99990000eee05           TerminateInstances        ?         ?         ?         SKIPPED
i-0ccc55556666ccc03           TerminateInstances        ?         ?         ?         SKIPPED
i-0ddd77778888ddd04           TerminateInstances        ?         ?         ?         SKIPPED
i-0bbb33334444bbb02           TerminateInstances        ?         ?         ?         SKIPPED
i-0ddd77778888ddd04           StartInstances            ?         ?         ?         SKIPPED
i-0bbb33334444bbb02           StartInstances            ?         ?         ?         SKIPPED
i-0ddd77778888ddd04           instanceType              ?         t3.micro  ?         SKIPPED
i-0bbb33334444bbb02           instanceType              ?         t3.micro  ?         SKIPPED
i-0ddd77778888ddd04           StopInstances             ?         ?         ?         SKIPPED
i-0bbb33334444bbb02           StopInstances             ?         ?         ?         SKIPPED
i-0ddd77778888ddd04           RunInstances              ?         ?         ?         SKIPPED
i-0bbb33334444bbb02           RunInstances              ?         ?         ?         SKIPPED
i-0ccc55556666ccc03           RunInstances              ?         ?         ?         SKIPPED
i-0eee99990000eee05           RunInstances              ?         ?         ?         SKIPPED
rewind-demo-payments-260923…  CreateDBInstance          ?         ?         ?         SKIPPED
arn:aws:lambda:us-west-1:18…  CreateAlias20150331       ?         ?         ?         SKIPPED
rewind-demo-checkout-260923…  CreateFunction20150331    ?         ?         ?         SKIPPED

Details
=======

chn-665d0e86880e  i-0fff11112222fff06.monitoring  [REVERTED]
  restored 'disabled' and confirmed it
  called     ec2:UnmonitorInstances

chn-18af3316d78f  i-0aaa11112222aaa01.monitoring  [REVERTED]
  restored 'disabled' and confirmed it
  called     ec2:UnmonitorInstances

chn-61f9005b0834  rewind-demo-payments-260923202643.multiAZ  [SUBMITTED]
  AWS accepted the change but it has not settled yet; re-run to poll it
  called     rds:ModifyDBInstance

chn-1df9abe06b3a  rewind-demo-payments-260923202643.allowMajorVersionUpgrade  [SKIPPED]
  the previous value of allowMajorVersionUpgrade is not proven

chn-b6b1765a76ce  rewind-demo-checkout-260923202643:live.provisionedConcurrency  [REVERTED]
  restored 'NONE' and confirmed it
  called     lambda:DeleteProvisionedConcurrencyConfig

chn-76b5a43277a6  i-0aaa11112222aaa01.StartInstances  [SKIPPED]
  the previous value of StartInstances is not proven

chn-57b94f12cd14  i-0fff11112222fff06.StartInstances  [SKIPPED]
  the previous value of StartInstances is not proven

chn-91174b46698c  i-0aaa11112222aaa01.instanceType  [REVERTED]
  restored 't3.micro' and confirmed it
  called     ec2:StopInstances
  called     ec2:ModifyInstanceAttribute
  called     ec2:StartInstances
  warning    instanceType can only be changed while the instance is stopped, so reverting it stops and restarts the instance; instance-store data is lost and public IPv4 addresses can change

chn-dc12a435024b  i-0fff11112222fff06.instanceType  [REVERTED]
  restored 't3.micro' and confirmed it
  called     ec2:StopInstances
  called     ec2:ModifyInstanceAttribute
  called     ec2:StartInstances
  warning    instanceType can only be changed while the instance is stopped, so reverting it stops and restarts the instance; instance-store data is lost and public IPv4 addresses can change

chn-3b99ba2cde8e  i-0aaa11112222aaa01.StopInstances  [SKIPPED]
  the previous value of StopInstances is not proven

chn-0f97fd72eb71  i-0fff11112222fff06.StopInstances  [SKIPPED]
  the previous value of StopInstances is not proven

chn-a8d5011602ac  i-0eee99990000eee05.TerminateInstances  [SKIPPED]
  the previous value of TerminateInstances is not proven

chn-26b5a190fddb  i-0ccc55556666ccc03.TerminateInstances  [SKIPPED]
  the previous value of TerminateInstances is not proven

chn-5f5f7d327d8f  i-0ddd77778888ddd04.TerminateInstances  [SKIPPED]
  the previous value of TerminateInstances is not proven

chn-3ffded560190  i-0bbb33334444bbb02.TerminateInstances  [SKIPPED]
  the previous value of TerminateInstances is not proven

chn-d7bc27a5a9af  i-0ddd77778888ddd04.StartInstances  [SKIPPED]
  the previous value of StartInstances is not proven

chn-4da4eb4f2ff6  i-0bbb33334444bbb02.StartInstances  [SKIPPED]
  the previous value of StartInstances is not proven

chn-f1f79259729d  i-0ddd77778888ddd04.instanceType  [SKIPPED]
  cannot read i-0ddd77778888ddd04: the instance is terminated, so its remembered value cannot be restored

chn-527d2dc2a7b2  i-0bbb33334444bbb02.instanceType  [SKIPPED]
  cannot read i-0bbb33334444bbb02: the instance is terminated, so its remembered value cannot be restored

chn-b3a0d78a0d04  i-0ddd77778888ddd04.StopInstances  [SKIPPED]
  the previous value of StopInstances is not proven

chn-a7dd42ba96c6  i-0bbb33334444bbb02.StopInstances  [SKIPPED]
  the previous value of StopInstances is not proven

chn-b9812a206a7e  i-0ddd77778888ddd04.RunInstances  [SKIPPED]
  the previous value of RunInstances is not proven

chn-fa6234443885  i-0bbb33334444bbb02.RunInstances  [SKIPPED]
  the previous value of RunInstances is not proven

chn-3e0a497d95e5  i-0ccc55556666ccc03.RunInstances  [SKIPPED]
  the previous value of RunInstances is not proven

chn-ec47c5a04acc  i-0eee99990000eee05.RunInstances  [SKIPPED]
  the previous value of RunInstances is not proven

chn-893e1059451b  rewind-demo-payments-260923202643.CreateDBInstance  [SKIPPED]
  the previous value of CreateDBInstance is not proven

chn-34fff7022179  arn:aws:lambda:us-west-1:111122223333:function:rewind-demo-checkout-260923202643:live.CreateAlias20150331  [SKIPPED]
  the previous value of CreateAlias20150331 is not proven

chn-1b8318c6a57f  rewind-demo-checkout-260923202643.CreateFunction20150331  [SKIPPED]
  the previous value of CreateFunction20150331 is not proven

2 field(s) still need attention:
  rewind-demo-payments-260923202643 multiAZ                SUBMITTED
  rewind-demo-payments-260923202643 allowMajorVersionUpgrade SKIPPED

21 change(s) reported but not revertible, and nothing an operator can do about it:
  i-0aaa11112222aaa01          StartInstances         CloudTrail records no value to restore
  i-0fff11112222fff06          StartInstances         CloudTrail records no value to restore
  i-0aaa11112222aaa01          StopInstances          CloudTrail records no value to restore
  i-0fff11112222fff06          StopInstances          CloudTrail records no value to restore
  i-0eee99990000eee05          TerminateInstances     CloudTrail records no value to restore
  i-0ccc55556666ccc03          TerminateInstances     CloudTrail records no value to restore
  i-0ddd77778888ddd04          TerminateInstances     CloudTrail records no value to restore
  i-0bbb33334444bbb02          TerminateInstances     CloudTrail records no value to restore
  i-0ddd77778888ddd04          StartInstances         CloudTrail records no value to restore
  i-0bbb33334444bbb02          StartInstances         CloudTrail records no value to restore
  i-0ddd77778888ddd04          instanceType           the resource no longer exists
  i-0bbb33334444bbb02          instanceType           the resource no longer exists
  i-0ddd77778888ddd04          StopInstances          CloudTrail records no value to restore
  i-0bbb33334444bbb02          StopInstances          CloudTrail records no value to restore
  i-0ddd77778888ddd04          RunInstances           CloudTrail records no value to restore
  i-0bbb33334444bbb02          RunInstances           CloudTrail records no value to restore
  i-0ccc55556666ccc03          RunInstances           CloudTrail records no value to restore
  i-0eee99990000eee05          RunInstances           CloudTrail records no value to restore
  rewind-demo-payments-260923202643 CreateDBInstance       CloudTrail records no value to restore
  arn:aws:lambda:us-west-1:111122223333:function:rewind-demo-checkout-260923202643:live CreateAlias20150331    CloudTrail records no value to restore
  rewind-demo-checkout-260923202643 CreateFunction20150331 CloudTrail records no value to restore

### writes actually issued
   unmonitor_instances
   unmonitor_instances
   modify_db_instance
   delete_provisioned_concurrency_config
   stop_instances
   modify_instance_attribute
   start_instances
   stop_instances
   modify_instance_attribute
   start_instances
```

## What each fix changed

**1. `monitoring` on both instances: FAILED → REVERTED.**

Two fixes had to land for this. Returning the observed value with the verdict made the row
self-consistent, but on its own it turned `MISMATCH` + `NOW=disabled` into `MISMATCH` +
`NOW=enabled` - honest, and still a false alarm, because the revert had worked.

So a field whose reads are known to lag its writes (`read_may_lag`) gets **one retry**, not a
reclassification. A read that lagged resolves in milliseconds; a write that silently did
nothing - a mis-scoped IAM policy is the usual cause - never does. Downgrading every miss to
"still pending" would have hidden exactly the case an operator most needs to hear about, and
there is a test holding that line.

**2. The two terminated instances: FAILED → SKIPPED, with no write issued.**

```
i-0ddd77778888ddd04   instanceType   the resource no longer exists
```

Before, `StopInstances` was called and returned `IncorrectInstanceState`. The `writes actually
issued` list at the end of the replay is the proof: ten calls, none of them against a
terminated instance.

**3. "still need attention": 5 rows → 2.**

A terminated instance is not a to-do; nothing closes it. It joins the "nothing an operator can
do about it" list with its own reason, alongside the valueless changes. The two rows that
remain are the two that are genuinely actionable: `multiAZ` is `SUBMITTED` and a re-run polls
it, and `allowMajorVersionUpgrade` could be rescued with `--set`.

## Still not fixed

The 21 `DISCOVERED` pseudo-fields. `RunInstances`, `StopInstances` and `CreateDBInstance` are
not fields, and calling them `UNCHECKABLE` blames a missing plugin for a missing change *type*.
See **Known limitations 2**.

---

# Closing the loop: the same diff, after the revert

The verification step was the one command the original run never completed - credentials
expired twice before it could be reached. Run against the *same* plan file once they were
refreshed:

```
plan       : /tmp/p.json
generated  : 2026-09-23T20:59:57+00:00  by identity alice-DevAccount in us-west-1
checked    : 2026-09-23T21:37:17.502215+00:00
fields     : 28
verdicts   : ALREADY_REVERTED=6  UNREADABLE=2  UNCHECKABLE=20
drift      : none - 6 of 28 field(s) compared, all still as the session left them

RESOURCE                      FIELD                     WAS       SESSION SET  LIVE NOW  VERDICT
----------------------------  ------------------------  --------  -----------  --------  ----------------
rewind-demo-checkout-260923…  CreateFunction20150331    ?         ?            ?         UNCHECKABLE
arn:aws:lambda:us-west-1:18…  CreateAlias20150331       ?         ?            ?         UNCHECKABLE
rewind-demo-payments-260923…  CreateDBInstance          ?         ?            ?         UNCHECKABLE
i-0eee99990000eee05           RunInstances              ?         ?            ?         UNCHECKABLE
i-0ccc55556666ccc03           RunInstances              ?         ?            ?         UNCHECKABLE
i-0bbb33334444bbb02           RunInstances              ?         ?            ?         UNCHECKABLE
i-0ddd77778888ddd04           RunInstances              ?         ?            ?         UNCHECKABLE
i-0bbb33334444bbb02           StopInstances             ?         ?            ?         UNCHECKABLE
i-0ddd77778888ddd04           StopInstances             ?         ?            ?         UNCHECKABLE
i-0bbb33334444bbb02           instanceType              t3.micro  t3.small     ?         UNREADABLE
i-0ddd77778888ddd04           instanceType              t3.micro  t3.small     ?         UNREADABLE
i-0bbb33334444bbb02           StartInstances            ?         ?            ?         UNCHECKABLE
i-0ddd77778888ddd04           StartInstances            ?         ?            ?         UNCHECKABLE
i-0bbb33334444bbb02           TerminateInstances        ?         ?            ?         UNCHECKABLE
i-0ddd77778888ddd04           TerminateInstances        ?         ?            ?         UNCHECKABLE
i-0ccc55556666ccc03           TerminateInstances        ?         ?            ?         UNCHECKABLE
i-0eee99990000eee05           TerminateInstances        ?         ?            ?         UNCHECKABLE
i-0aaa11112222aaa01           StopInstances             ?         ?            ?         UNCHECKABLE
i-0fff11112222fff06           StopInstances             ?         ?            ?         UNCHECKABLE
i-0fff11112222fff06           instanceType              t3.micro  t3.small     t3.micro  ALREADY_REVERTED
i-0aaa11112222aaa01           instanceType              t3.micro  t3.small     t3.micro  ALREADY_REVERTED
i-0aaa11112222aaa01           StartInstances            ?         ?            ?         UNCHECKABLE
i-0fff11112222fff06           StartInstances            ?         ?            ?         UNCHECKABLE
rewind-demo-checkout-260923…  provisionedConcurrency    NONE      1            NONE      ALREADY_REVERTED
rewind-demo-payments-260923…  allowMajorVersionUpgrade  ?         false        ?         UNCHECKABLE
rewind-demo-payments-260923…  multiAZ                   false     true         false     ALREADY_REVERTED
i-0aaa11112222aaa01           monitoring                disabled  enabled      disabled  ALREADY_REVERTED
i-0fff11112222fff06           monitoring                disabled  enabled      disabled  ALREADY_REVERTED

Details
=======
chn-527d2dc2a7b2  i-0bbb33334444bbb02.instanceType  [UNREADABLE]
  cannot read i-0bbb33334444bbb02: the instance is terminated, so its remembered value cannot be restored
  cannot read i-0ddd77778888ddd04: the instance is terminated, so its remembered value cannot be restored

… one entry per blocking field
```

Every one of the six AUTO fields reads `ALREADY_REVERTED`, and the account agrees:

```
i-0aaa11112222aaa01   t3.micro  running  disabled
i-0fff11112222fff06   t3.micro  running  disabled
rewind-demo-payments-260923202643   MultiAZ=False  available
rewind-demo-checkout-260923202643   ProvisionedConcurrencyConfigs: []
```

Two things this last step proved that nothing earlier had:

**`multiAZ` went `SUBMITTED` → `ALREADY_REVERTED` on a re-run.** Multi-AZ conversion is
asynchronous, so the revert could only report "accepted, not settled". Polling it by simply
re-running is the whole design for asynchronous fields, and it worked on real data.

**`ResourceGone` fires in `diff`, not just in `revert`.** The two terminated instances now
read:

```
i-0bbb33334444bbb02  instanceType  t3.micro  t3.small  ?  UNREADABLE
  cannot read i-0bbb33334444bbb02: the instance is terminated, so its remembered value
  cannot be restored
```

Before the fix those rows were `REVERTIBLE` with `LIVE NOW = t3.small`, because
`DescribeInstanceAttribute` reports the type an instance had when it died. The problem is now
caught one phase earlier - at review time rather than at `IncorrectInstanceState`.

## The run, end to end

| Step | Result |
|---|---|
| `scan` (no `--identity`) | 228 changes, 13 identities, the 8 that mattered in one row |
| `plan` | 28 fields, AUTO=8, all four anchor sources present |
| `diff` | REVERTIBLE=8, drift none |
| `revert` (dry run) | DRY_RUN=8, SKIPPED=20, zero writes |
| `revert --confirm` | 6 writes, matched one-for-one against CloudTrail |
| `diff` again | **ALREADY_REVERTED=6** |

Five defects were found by this exercise that 373 passing fixture tests had not: see
**What the fixtures did not catch** and **After the fixes**. All are fixed, each with a test
verified to fail against the old code.
