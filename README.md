# AWS Rewind

A local CLI that answers three questions about an AWS account:

1. **What did this identity change?**
2. **What was each of those settings before?**
3. **What exactly would it take to put them back?**

It reads CloudTrail event history and makes read-only Describe calls. It creates no AWS
resources, needs no database, runs nothing in your account, and adds **$0** to the bill.

**It does not presuppose what a session contains.** Any mutating call CloudTrail records is
discovered, and for any declarative API the old value is reconstructed with no per-API code
at all. Plugins exist, and four ship as a demo — but a plugin is what raises one field from
"here is the command, you run it" to "the tool can do it for you". It is not what makes a
change visible.

Two guiding rules:

- **never invent a previous value.** When the old value cannot be proven the tool says so,
  shows the change anyway, and lets you supply the value yourself.
- **never hide a change.** A field with no plugin is reported at a lower capability tier,
  never omitted. Silent omission is worse than an honest "I can see this but cannot undo
  it".

> `scan`, `plan`, `diff`, `revert`, `snapshot`, `operations` and `resolvers` are
> implemented. Everything is read-only **except** `revert --confirm`, which is the single
> mutating code path in the tool — and it only ever touches fields a plugin vouches for.

---

## Install

```bash
pipx install .          # or: uvx --from . rewind --help
rewind --help
```

Credentials come from the ambient AWS configuration, exactly like the AWS CLI. Required
permissions:

| Command | Needs |
|---|---|
| `scan`, `plan` | `cloudtrail:LookupEvents` |
| `plan --use-config` | the above, plus `config:DescribeConfigurationRecorderStatus`, `config:GetResourceConfigHistory`, `config:ListDiscoveredResources` |
| `snapshot` | the Describe/Get reads below, for the resources you name |
| `diff`, `revert` (dry run) | the above, plus `ec2:DescribeInstances`, `ec2:DescribeInstanceAttribute`, `lambda:GetProvisionedConcurrencyConfig`, `rds:DescribeDBInstances` |
| `revert --confirm` | the above, plus the specific write for each field you revert: `ec2:ModifyInstanceAttribute`, `ec2:StopInstances`, `ec2:StartInstances`, `ec2:MonitorInstances`, `ec2:UnmonitorInstances`, `lambda:PutProvisionedConcurrencyConfig`, `lambda:DeleteProvisionedConcurrencyConfig`, `rds:ModifyDBInstance` |

A read-only role is enough for everything except `revert --confirm` — which is a good way
to try the tool out: run it with read-only credentials and the dry run tells you exactly
what it would do.

Runs from a clone with no install too:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/rewind --help
# or without installing at all:
PYTHONPATH=src python3 -m rewind --help
```

---

## The idea

CloudTrail records the value each call **set**, never the value it replaced. The naive
approach searches backwards for "the last event that set this field" once per change —
and every one of those searches can fall off the end of CloudTrail's 90-day retention.

A session's changes to one field actually form a chain:

```
before₁ ──change 1──▶ after₁ ═ before₂ ──change 2──▶ after₂ ═ … ═ now
          └── CloudTrail recorded every "after" directly ──┘
```

So each step's `before` is just the previous step's `after`. Only **one** value per
`(resource, field)` is genuinely unknown: the one from before the *first* change. The
tool calls that the **anchor**, and resolves it once per chain rather than once per
change. A session that resized an instance five times has one unknown, not five.

It also means the revert target is the pre-session value, never an intermediate one:

```
i-0aaa000000000000a  instanceType  t3.micro -> t3.small  2 steps  →  revert to t3.micro
                                   (via t3.large, which is not the target)
```

---

## Capability tiers

Every row the tool prints says how far it got with that field. This is the mechanism that
lets it be general without overstating what it can do.

| Tier | What is known | What `rewind revert` does |
|---|---|---|
| `AUTO` | a plugin covers this field | reads live state, checks for conflicts, executes, verifies |
| `MANUAL` | old and new values known, and the API can be re-called with the old value | prints an `aws` command for you to check and run |
| `RECONSTRUCTED` | old and new values known, but no safe inverse exists for that API | nothing — explains why |
| `DISCOVERED` | the change happened, but the new value is not in `requestParameters` | nothing — the change is still listed |

In a real account the lower tiers dominate, and by a wide margin. A 35-minute window on a
live test account held **306 tracked changes**; 286 of them were SSM agent heartbeats from
six instances, and exactly **4 were revertible**:

```
IDENTITY                                        CHANGES  RESOURCES  PLUGIN-BACKED  EVENTS
----------------------------------------------  -------  ---------  -------------  -------------------------
Admin/alice-DevAccount                          16       4          4              ModifyInstanceAttribute, …
CloudAWSSystemsManager…ole/i-0abc11112222aaaa1  69       1          -              UpdateInstanceInformation
CloudTelemetryInstanceRole/i-0fed55556666cccc3  60       1          -              UpdateInstanceInformation
aws:ec2-instance/i-0def33334444bbbb2            1        1          -              RegisterManagedInstance
```

That is why `scan` summarises by identity when you do not name one, and why `PLUGIN-BACKED`
sorts the table: the signal is a rounding error on the noise. Scoped to the one identity
that mattered, the plan was eight fields, two of them `AUTO`:

```
RESOURCE             FIELD           BEFORE -> NOW         STEPS  CONFIDENCE  CAPABILITY  ANCHOR
-------------------  --------------  --------------------  -----  ----------  ----------  -----------------
i-0abc11112222aaaa1  instanceType    t3.micro -> t3.small  1      HIGH        AUTO        cloudtrail-window
i-0def33334444bbbb2  instanceType    t3.micro -> t3.small  1      HIGH        AUTO        cloudtrail-window
i-0def33334444bbbb2  StartInstances  ? -> ?                1      UNKNOWN     DISCOVERED  none
i-0abc11112222aaaa1  StopInstances   ? -> ?                1      UNKNOWN     DISCOVERED  none
i-0cba77778888dddd4  RunInstances    ? -> ?                1      UNKNOWN     DISCOVERED  none
```

The six `DISCOVERED` rows are honest but coarse: four are the stop/start that resizing an
instance *requires*, and two are instance creations. See **Known limitations** — they are a
missing change *type*, not a missing plugin.

### How a field with no plugin is understood

Nothing clever — CloudTrail already carries enough:

- **is it a change?** `readOnly: false`. (A verb heuristic covers events too old to have
  the flag.)
- **which resource?** CloudTrail's `Resources` index when the service populated it,
  otherwise request parameters that look like identifiers (`*Id`, `*Identifier`, `*Name`,
  `*Arn`, `*Qualifier`, or an `arn:`-shaped value).
- **which field, set to what?** every other scalar leaf of `requestParameters`, addressed
  by its path — so `VersioningConfiguration.Status` is a field name the tool never had to
  be told about. Call mechanics (`clientToken`, `dryRun`, `applyImmediately`, …) are
  excluded.
- **what was it before?** the same path on the same resource in an earlier event. **The
  chain trick needs no API knowledge at all** — which is why `timeout 3 -> 30` above is
  HIGH confidence with nobody having written a line of Lambda-specific code.

Everything inferred is reported, so a wrong guess is visible rather than silent. The known
cost: a heuristic will occasionally call something a field that is really a launch argument
(`minCount` on `RunInstances`). Those land at `DISCOVERED` or get their inverse refused, so
the failure mode is noise, not a bad revert.

### Why a generic inverse is never executed

For a **declarative, partial-update** API the inverse is mechanical — re-call it with the
old value:

```
ModifyDBInstance(dBInstanceIdentifier=db, multiAZ=true)
  → aws rds modify-db-instance --db-instance-identifier db --multi-az false
```

Four families break that, and each is refused with a reason rather than guessed at:

1. **whole-document replacement.** `PutBucketPolicy`, `PutBucketVersioning` — calling one
   with a single field discards everything else. This is the dangerous one.
2. **state encoded in the verb.** `MonitorInstances` / `UnmonitorInstances` — the old value
   is the *other* event name.
3. **preconditions.** An EC2 resize needs the instance stopped first.
4. **creation and deletion.** The inverse of `DeleteX` is `CreateX` with every argument the
   original had, which CloudTrail does not record.

So the generic tier writes the command out and stops. `revert --confirm` touches `AUTO`
only. Run `rewind operations` to see which fields that is.

---

## Where an anchor comes from

Six sources, tried in priority order; the first to *prove* a value wins. None requires
infrastructure you do not already have.

| # | Resolver | Confidence | Cost | Covers |
|---|---|---|---|---|
| 1 | `response-elements` — the changing call's own response carried the old value | HIGH | free | RDS `ModifyDBInstance`. **Immune to the 90-day limit.** |
| 2 | `config-history` — AWS Config `GetResourceConfigHistory` | HIGH | **zero extra** where the account already records | EC2 instance type and monitoring, RDS Multi-AZ, far past 90 days. Opt in with `--use-config` |
| 3 | `local-snapshot` — a snapshot written before the change | HIGH | free, local | anything, if you took one. Opt in with `--snapshot` |
| 4 | `cloudtrail-window` — the latest earlier successful event that set this field | HIGH | free | anything changed in the last 90 days |
| 5 | `creation-event` — the resource's creation event, when inside the window | MEDIUM | free | resources created recently |
| 6 | `operator-supplied` — a value you pass with `--set` | **ASSERTED** | free | the last resort. Never overrides evidence |

`rewind resolvers` prints the chain and which sources are live. Inactive ones are listed
with the flag that switches them on rather than hidden, so you can always tell "no old
value exists" apart from "the tool did not look there":

```
Anchor resolvers, in priority order. The first one to *prove* a value wins.

RESOLVER           ACTIVE  EVIDENCE
-----------------  ------  --------------------------------------------------------------
response-elements  yes     the changing call's own responseElements carried the pre-chan…
config-history     no      AWS Config configuration history, when the account records it
local-snapshot     no      a local snapshot file written before the change (optional, no…
cloudtrail-window  yes     the latest earlier successful event in the window that set th…
creation-event     yes     the resource's creation event, when it is inside the window
operator-supplied  no      a value the operator passed with --set (asserted, not proven)

config-history: not enabled (pass --use-config)
local-snapshot: no snapshot file was supplied (--snapshot)
operator-supplied: no --set values were supplied
```

Confidence means:

- **HIGH** — the change event's own response carried the pre-change value, an earlier
  successful event explicitly set this field, or a recorded observation (an AWS Config
  item, a local snapshot) captured it.
- **MEDIUM** — the value came from the resource's creation event, or from an inference
  that is only sound because the history is provably complete.
- **ASSERTED** — you supplied it with `--set`. The tool did not prove it, and the plan
  says so. An audit trail that blurs "we established this" with "somebody told us" is
  worse than none.
- **UNKNOWN** — not proven and not supplied. Shown, never reverted.

### What is deliberately *not* treated as evidence

- a **failed** call (`errorCode` present) — an API that returned an error changed nothing,
  even when it is the most recent event touching the field;
- a call that changed a **different attribute** — `ModifyInstanceAttribute` covers many
  fields, so the parser keys on `instanceType.value` being present, not on the event name;
- `StopInstances` / `StartInstances` — power transitions never change an instance type;
- a **service default** — if `RunInstances` does not record `monitoring`, the EC2 default
  is not proof, so the anchor stays UNKNOWN;
- **absence of events after a truncated lookup** — if a query was cut short, "we saw no
  intervening event" means nothing, so inferences that depend on completeness are dropped.

---

## Usage

```bash
# 0. the whole sequence in one command.  Dry run unless --confirm is passed.
rewind undo --identity perf-agent --since 90m
rewind undo --identity perf-agent --since 90m --confirm

# or step by step, which is the same thing:

# 1. what changed?  (CloudTrail only, no previous values claimed)
rewind scan --identity perf-agent --since 90m --region us-west-1

# 2. what was it before, and how would it be put back?
rewind plan --identity perf-agent --since 90m -o plan.json

# 3. show the evidence behind every resolved value
rewind plan --identity perf-agent --since 90m --explain

# 4. has anything drifted since the plan was made?
rewind diff plan.json
rewind diff plan.json --blame        # name who caused each conflict
rewind diff plan.json --exit-code    # exit 3 when anything conflicts, for CI

# 5. fill a gap the tool cannot prove (three independent ways)
rewind plan --identity perf-agent --since 90m --use-config     # AWS Config, if recorded
rewind plan --identity perf-agent --since 90m --snapshot s.json
rewind plan --identity perf-agent --since 90m \
            --set i-0bbb000000000000b.instanceType=t3.nano

# take a snapshot *before* letting an agent loose (optional, local, free)
rewind snapshot --instance i-0aaa000000000000a --db-instance my-db -o snapshot.json

# 6. put it back.  Dry run unless --confirm is passed.
rewind revert plan.json                       # shows the exact calls, makes none
rewind revert plan.json --confirm             # applies, newest change first
rewind revert plan.json --confirm --only chn-c22f4d4af202
rewind revert plan.json --confirm --log revert.json

# what has a plugin, and what happens to everything else?
rewind operations

# where does the tool look for old values?
rewind resolvers
```

Windows: `--since 90m|2h|3d|1w`, or `--start`/`--end` with ISO-8601 instants.
Output: `--output table` (default) or `--output json` for scripting.

### `undo` — all of it, in one command

`plan`, `diff` and `revert` back to back. **Dry run unless `--confirm` is passed**, exactly as
`revert` alone behaves: the review step is preserved by the default, not by refusing to
compose the steps.

```
identity   : perf-agent
window     : 2026-09-22T17:20:00+00:00 -> 2026-09-22T17:30:00+00:00
region     : us-west-1
mode       : DRY RUN - nothing was called

1 plan     : 8 change(s) across 6 field(s); 4 can be reverted automatically
2 diff     : no drift in the 6 field(s) that could be compared
3 revert   : DRY_RUN=4  SKIPPED=2
plan       : /tmp/rewind-plan-ab12cd.json

RESOURCE             FIELD                   WAS       TARGET    NOW  DIFF SAID   OUTCOME
-------------------  ----------------------  --------  --------  ---  ----------  -------
rewind-demo-db       multiAZ                 true      false     ?    REVERTIBLE  DRY_RUN
rewind-demo-fn:live  provisionedConcurrency  5         NONE      ?    REVERTIBLE  DRY_RUN
i-0aaa000000000000a  monitoring              enabled   disabled  ?    REVERTIBLE  DRY_RUN
i-0aaa000000000000a  instanceType            t3.small  t3.micro  ?    REVERTIBLE  DRY_RUN

2 field(s) still need a decision:
  i-0bbb000000000000b   monitoring     the previous value of monitoring is not proven
  i-0bbb000000000000b   instanceType   the previous value of instanceType is not proven

Nothing was called. Re-run with --confirm to apply; live state is re-read immediately
before each field is touched.
```

Three stages, one line each, and the table is the *revert's* view with the diff's verdict
beside it - because "ready" and "somebody else touched this" are different reasons to leave a
field alone. `--detail` prints each stage's own report in full instead.

Two things it does that running three commands by hand tends to skip:

- **the plan is written even in a dry run**, to `--out` or to a temporary file whose path is
  printed. A run nobody can re-check afterwards is not an audit trail, and an asynchronous
  field needs the document to be polled by a later `revert`.
- **the diff runs before any write, including when confirming.** It is the only thing that can
  tell "ready to revert" from "somebody else has been here since".

A conflict does **not** abort the run. `revert` already refuses a conflicted field one at a
time, having re-read it immediately beforehand, and aborting everything because one field
drifted would leave the rest of an incident unhandled. `--exit-code` returns 3 when anything
conflicts or still needs a decision.

`--identity` is required here, as it is for `plan`: an unfiltered undo would collect the
changes made by service-linked roles and revert them.

### `scan`

```
identity : perf-agent
window   : 2026-09-22T17:20:00+00:00 -> 2026-09-22T17:30:00+00:00
region   : us-west-1
events   : 8 in window, 7 by this identity
changes  : 8 tracked field change(s)

TIME                 EVENT                            RESOURCE             FIELD                   SET TO       HANDLED BY
-------------------  -------------------------------  -------------------  ----------------------  -----------  ----------
2026-09-22 17:21:00  ModifyInstanceAttribute          i-0aaa000000000000a  instanceType            -> t3.large  plugin
2026-09-22 17:22:30  ModifyInstanceAttribute          i-0aaa000000000000a  instanceType            -> t3.small  plugin
2026-09-22 17:23:00  ModifyInstanceAttribute          i-0bbb000000000000b  instanceType            -> t3.small  plugin
2026-09-22 17:24:00  MonitorInstances                 i-0aaa000000000000a  monitoring              -> enabled   plugin
2026-09-22 17:24:00  MonitorInstances                 i-0bbb000000000000b  monitoring              -> enabled   plugin
2026-09-22 17:25:00  PutProvisionedConcurrencyConfig  rewind-demo-fn:live  provisionedConcurrency  -> 1         plugin
2026-09-22 17:26:00  PutProvisionedConcurrencyConfig  rewind-demo-fn:live  provisionedConcurrency  -> 5         plugin
2026-09-22 17:27:00  ModifyDBInstance                 rewind-demo-db       multiAZ                 -> true      plugin

Only the value each call *set* is shown; run `rewind plan` to resolve what each field held beforehand.
```

#### Finding out *who*, when you do not know yet

`--identity` is **optional on `scan`**, because "which identity did this?" is the question an
operator has *before* they can answer it. Omit it and you get one row per identity:

```
identity : (all - no --identity given)
events   : 305 in window (all in scope)
changes  : 341 tracked field change(s)
identities: 11 made a tracked change

IDENTITY                                        CHANGES  RESOURCES  PLUGIN-BACKED  EVENTS
----------------------------------------------  -------  ---------  -------------  -------------------------
Admin/alice-DevAccount                          16       4          4              ModifyInstanceAttribute, …
CloudAWSSystemsManager…ole/i-0abc11112222aaaa1  69       1          -              UpdateInstanceInformation
CloudTelemetryInstanceRole/i-0fed55556666cccc3  60       1          -              UpdateInstanceInformation
aws:ec2-instance/i-0def33334444bbbb2            1        1          -              RegisterManagedInstance
```

Three deliberate choices in that table, all of them learned from real accounts:

- **counted, not listed.** That window held 341 changes, 325 of them SSM agent heartbeats.
  A row per change cannot answer "who"; the twelve a human made are invisible in it. Pass
  `--detail` for every change, with the identity as a column.
- **sorted by `PLUGIN-BACKED`, not by volume.** It counts the changes `rewind revert` could
  actually execute, so the identity that matters is first even though another made four
  times as many changes.
- **ARNs are elided from the middle.** Six SSM roles differ only in their trailing instance
  id; truncating from the right rendered all six identically — a table hiding exactly what
  it was printed to show. The `arn:aws:sts::<account>:assumed-role/` prefix is dropped
  outright, since every row in one account shares it.

`plan` keeps `--identity` **required**, and that is a safety boundary rather than an
inconsistency: an unfiltered plan would collect the changes made by `AWSServiceRoleForRDS`,
every audit role and every SSM agent in the account, and describe a revert for each one.

### `plan`

```
changes    : 8 change(s) across 6 field(s)
revertible : 4 of 6  (unprovable: 2, already back to original: 0)
confidence : HIGH=2 MEDIUM=2 UNKNOWN=2
warning    : Anchor evidence older than 90 days cannot be read from CloudTrail event
             history. Fields last changed before then will be UNKNOWN; enable AWS
             Config recording, or pass the value with --set.

RESOURCE             FIELD                   BEFORE -> NOW         STEPS  CONFIDENCE  REVERT  ANCHOR
-------------------  ----------------------  --------------------  -----  ----------  ------  -----------------
i-0aaa000000000000a  instanceType            t3.micro -> t3.small  2      HIGH        yes     cloudtrail-window
i-0bbb000000000000b  instanceType            ? -> t3.small         1      UNKNOWN     no      none
i-0aaa000000000000a  monitoring              disabled -> enabled   1      MEDIUM      yes     creation-event
i-0bbb000000000000b  monitoring              ? -> enabled          1      UNKNOWN     no      none
rewind-demo-fn:live  provisionedConcurrency  NONE -> 5             2      MEDIUM      yes     creation-event
rewind-demo-db       multiAZ                 false -> true         1      HIGH        yes     response-elements
```

`?` is an unproven anchor. It is never filled with a plausible-looking guess.

### `plan --explain`

```
chn-c22f4d4af202  i-0aaa000000000000a.instanceType
  anchor     : t3.micro  (HIGH via cloudtrail-window)
  reason     : previous value set by ModifyInstanceAttribute at 2026-09-16T11:00:00+00:00
  evidence   : h0000003-0000-4000-8000-00000000h003
  step 1     : 17:21:00  ModifyInstanceAttribute: t3.micro -> t3.large  [s0000001-…]
  step 2     : 17:22:30  ModifyInstanceAttribute: t3.large -> t3.small  [s0000002-…]
  revert to  : t3.micro  via ec2:StopInstances, ec2:ModifyInstanceAttribute, ec2:StartInstances
  note       : 2 changes to this field in the session; the value to restore is the one
               from before the first change

chn-610ea650a65d  i-0bbb000000000000b.instanceType
  anchor     : ?  (UNKNOWN via none)
  reason     : no resolver could prove the previous value … (response-elements: the
               ModifyInstanceAttribute response does not carry a pre-change instanceType;
               config-history: not enabled (pass --use-config); cloudtrail-window: no earlier
               successful event in the window sets instanceType on i-0bbb…; creation-event:
               the creation of i-0bbb… is not visible in the window)
  step 1     : 17:23:00  ModifyInstanceAttribute: ? -> t3.small  [s0000003-…]
  revert     : not automatic - the previous value of instanceType is not proven
  note       : not revertible automatically: the previous value is not proven. Supply it
               explicitly with --set to revert anyway.
```

An UNKNOWN still tells you which sources were consulted and why each one missed. That is
the difference between a dead end and an actionable next step.

---

### `diff`

`plan` tells you what CloudTrail says. `diff` tells you whether that is still true.

```
plan       : <stdin>
generated  : 2026-09-22T17:30:00+00:00  by identity perf-agent in us-west-1
checked    : 2026-09-22T17:30:00+00:00
fields     : 6
verdicts   : REVERTIBLE=1  UNPROVEN=2  CONFLICT=2  UNREADABLE=1
CONFLICT   : 2 field(s) were changed outside this plan and must not be overwritten

RESOURCE             FIELD                   WAS       SESSION SET  LIVE NOW    VERDICT
-------------------  ----------------------  --------  -----------  ----------  ----------
i-0aaa000000000000a  instanceType            t3.micro  t3.small     t3.2xlarge  CONFLICT
i-0bbb000000000000b  instanceType            ?         t3.small     t3.small    UNPROVEN
i-0aaa000000000000a  monitoring              disabled  enabled      enabled     REVERTIBLE
i-0bbb000000000000b  monitoring              ?         enabled      enabled     UNPROVEN
rewind-demo-fn:live  provisionedConcurrency  NONE      5            25          CONFLICT
rewind-demo-db       multiAZ                 false     true         ?           UNREADABLE

Details
=======

chn-03d8733342b7  i-0aaa000000000000a.instanceType  [CONFLICT]
  the session left this field at 't3.small' but it now reads 't3.2xlarge'; something
  outside this plan changed it
  live instanceState: running
  run with --blame to look for the CloudTrail event that changed it

… one entry per blocking or attributed field

1 field(s) are ready to revert. Next: `rewind revert plan.json` for a dry run, then
add --confirm.
```

#### The two questions `diff` keeps separate

This is the design point, and it is why `diff` is useful even where `plan` had to give up:

| | old value known | old value UNKNOWN |
|---|---|---|
| **nothing else touched it** | `REVERTIBLE` | `UNPROVEN` |
| **something else touched it** | `CONFLICT` | `CONFLICT` |

Drift detection compares live state with the chain's `netAfter`. That needs **no history
at all** — so the tool can always tell you whether a resource has been meddled with, even
when it cannot tell you what the value used to be. An `UNPROVEN` row is still useful
information: nobody else has touched it, so supplying the value by hand is safe.

#### Verdicts

| Verdict | Meaning |
|---|---|
| `REVERTIBLE` | live state matches the session's outcome and the old value is proven — safe to revert |
| `UNPROVEN` | live state matches, but the old value is not proven; supply it explicitly |
| `ALREADY_REVERTED` | somebody already put this field back to its pre-session value |
| `ALREADY_AT_ORIGINAL` | the session's net effect was zero; there was never anything to undo |
| `CONFLICT` | live state is neither the session's outcome nor the original — **never overwritten** |
| `UNREADABLE` | the field's current value could not be read (resource deleted, no permission, …) |

An unreadable resource is reported and the diff continues; one deleted database does not
stop the other five fields from being checked.

#### `--blame`

A `CONFLICT` immediately raises "who?". `--blame` searches CloudTrail from the plan's
`generatedAt` to now for events touching the conflicted fields, and names the identity.
The query is only issued **if a conflict was actually found**, so a clean diff costs
nothing extra.

When `--blame` finds nothing, the tool says so plainly rather than repeating the hint —
the change may predate the plan, sit outside event history, or not be recorded at all.

#### RDS convergence is not drift

A Multi-AZ change takes minutes, and while it converges RDS reports the **old** value with
the new one under `PendingModifiedValues`. Comparing the applied value would make that
window look exactly like somebody else's change, so `diff` compares the *effective*
(pending-aware) value and shows the raw detail beside it:

```
  live appliedMultiAZ: false
  live pendingMultiAZ: true
  live note: a Multi-AZ modification is still being applied
```

#### Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including "conflicts found", unless `--exit-code` is passed) |
| 1 | an error — a bad plan file, say |
| 2 | a usage problem — an unparseable window |
| 3 | `--exit-code` was passed and at least one field conflicts |

Default is 0 on conflicts, matching `git diff`; pass `--exit-code` to branch on drift in a
script.

---

### `revert`

The only mutating command, gated three ways.

**1. Dry run by default.** No `--confirm`, no calls:

```
plan       : <stdin>
mode       : DRY RUN - nothing was called
region     : us-west-1
started    : 2026-09-22T17:30:00+00:00
fields     : 6  (newest change reverted first)
outcomes   : DRY_RUN=4  SKIPPED=2

RESOURCE             FIELD                   WAS       TARGET    NOW  OUTCOME
-------------------  ----------------------  --------  --------  ---  -------
rewind-demo-db       multiAZ                 true      false     ?    DRY_RUN
rewind-demo-fn:live  provisionedConcurrency  5         NONE      ?    DRY_RUN
i-0bbb000000000000b  monitoring              ?         ?         ?    SKIPPED
i-0aaa000000000000a  monitoring              enabled   disabled  ?    DRY_RUN
i-0bbb000000000000b  instanceType            ?         ?         ?    SKIPPED
i-0aaa000000000000a  instanceType            t3.small  t3.micro  ?    DRY_RUN

Details
=======

chn-03d8733342b7  i-0aaa000000000000a.instanceType  [DRY_RUN]
  would restore 't3.micro'; nothing was called (pass --confirm to apply)
  would call ec2:StopInstances  # EC2 rejects a change to instanceType on a running instance
  would call ec2:ModifyInstanceAttribute
  would call ec2:StartInstances  # only if the instance is running when the revert starts
  warning    instanceType can only be changed while the instance is stopped, so reverting
             it stops and restarts the instance; instance-store data is lost and public
             IPv4 addresses can change

… one entry per field, in execution order

Re-run with --confirm to apply. Live state is re-checked immediately before each field
is touched, so a conflict that appears in the meantime still stops that field.
```

**2. A fresh check immediately before each field is touched** — not the plan's view, not
the diff's view. An EC2 resize waits on stop and start waiters, so minutes can pass
between one field and the next; a check done once up front would be stale by the time the
tool reached the last one. A conflict that appears *during* the run still stops that
field:

```
i-0aaa000000000000a  instanceType  t3.2xlarge  t3.micro  ?  SKIPPED
  the session left this field at 't3.small' but it now reads 't3.2xlarge';
  something outside this plan changed it
```

**3. Only `REVERTIBLE` is touched.** A conflict, an unproven old value or an unreadable
resource is reported and left alone. One failure does not stop the run — the remaining
fields are still attempted, and the summary lists what still needs attention.

#### Outcomes

| Outcome | Meaning |
|---|---|
| `DRY_RUN` | would be reverted; nothing was called |
| `REVERTED` | applied, and a Describe/Get confirmed the field is back |
| `SUBMITTED` | AWS accepted it but it has not settled yet (RDS Multi-AZ) — re-run to poll |
| `ALREADY_REVERTED` | the field was already at its pre-session value before we did anything |
| `SKIPPED` | deliberately not touched — conflict, unproven, unreadable, or nothing to do |
| `FAILED` | a call raised, or verification did not observe the expected value |

#### Order

Newest change first, by each chain's **last** change — that is the change being undone.
Ties break on chain id, so two fields changed by a single API call are always reverted in
the same order.

#### Idempotency, for free

Re-running stores no state and needs none. The fresh pre-check re-reads live state, finds
the field already at its pre-session value, and reports `ALREADY_REVERTED` without
calling anything. The same mechanism polls an asynchronous RDS change to completion:

```
$ rewind revert plan.json --confirm --only chn-6ae117be5d1c
rewind-demo-db  multiAZ  true  false  ?  SUBMITTED
  AWS accepted the change but it has not settled yet; re-run to poll it

$ rewind revert plan.json --confirm --only chn-6ae117be5d1c    # a few minutes later
rewind-demo-db  multiAZ  false  false  false  ALREADY_REVERTED
```

`SUBMITTED` is not a hedge. `read_live_value` reports the value a resource is *converging
towards* (so the convergence window is not mistaken for drift), while verification asks
the stricter question — has it actually settled? Claiming `REVERTED` while RDS is still
applying the change would be a lie.

#### What a revert does and does not restore

It restores **the field the chain owns**, and nothing else. Reverting an instance type
stops the instance, resizes it, and then leaves the power state as it *found* it —
running before, running after. The pre-session power state is not part of this chain, so
the tool does not touch it.

#### Exit codes

`--exit-code` returns 3 when work is **outstanding** — something failed, is still pending, or
was skipped but could still be rescued with `--set` or by hand.

A change CloudTrail recorded no value for is *not* outstanding. Nothing can ever be done
about it, so it is listed separately and does not affect the exit code:

```
1 change(s) reported but not revertible - CloudTrail records no value for them, so
there is nothing to restore:
  i-0ccc000000000000c   StopInstances
```

This distinction was found the hard way: a live run reverted everything it could and still
exited 3, because six unactionable rows were counted as work left over — telling a CI job
that a clean run had failed.

---

### Closing the 90-day gap

CloudTrail event history holds 90 days. A resource that ran untouched for six months and
was changed once therefore has no in-window evidence — and that is exactly the incident you
most want to undo. Three independent answers, none of which costs anything extra.

#### `--use-config` — AWS Config

The real fix. Config keeps configuration items far longer than CloudTrail's event history,
and where an account already records them for compliance the data is **already paid for**.

```
rewind plan --identity perf-agent --since 90m --use-config

RESOURCE             FIELD         BEFORE -> NOW        STEPS  CONFIDENCE  REVERT  ANCHOR
-------------------  ------------  -------------------  -----  ----------  ------  --------------
i-0bbb000000000000b  instanceType  t3.nano -> t3.small  1      HIGH        yes     config-history
```

Probed, never required: the resolver checks for a running configuration recorder and skips
itself with a reason if there is none. Two AWS-specific details are handled rather than
glossed over:

- Config identifies an RDS instance by its `DbiResourceId` (`db-ABC…`), **not** by the
  `DBInstanceIdentifier` that everything else uses, so the resolver looks the id up with
  `ListDiscoveredResources` and caches it;
- Config does **not** record Lambda provisioned concurrency. That field gets an explicit
  "AWS Config does not record provisionedConcurrency" instead of a silent miss.

#### `--snapshot` — a local file, taken beforehand

```
rewind snapshot --instance i-0aaa000000000000a --db-instance my-db -o snapshot.json

taken   : 2026-09-22T17:30:00+00:00
region  : us-west-1
fields  : 3 recorded, 0 unreadable

RESOURCE             FIELD         VALUE     ERROR
-------------------  ------------  --------  -----
i-0aaa000000000000a  instanceType  t3.small
i-0aaa000000000000a  monitoring    enabled
rewind-demo-db       multiAZ       true
```

A plain local JSON file. No AWS resource is created, nothing is charged, and it is yours
to keep, commit or throw away. A resource whose field cannot be read is recorded **with its
error** rather than omitted, so the file says what was attempted.

A snapshot is not automatically trusted. It establishes the anchor only when both hold:

1. it was taken **before** the session's first change to that field — a snapshot taken
   afterwards records the *new* value, which would be exactly the wrong answer;
2. CloudTrail shows nothing moved the field in between.

That second check is shared with the Config resolver, so the standard cannot drift between
them.

#### `--set` — you supply the value

The last resort, and the cheapest. Everything else already works: the conflict check, the
ordering, the verification.

```bash
rewind plan --identity perf-agent --since 90m \
            --set i-0bbb000000000000b.instanceType=t3.nano
# or, using the chain id from a previous plan:
rewind plan --identity perf-agent --since 90m --set chn-610ea650a65d=t3.nano
```

```
RESOURCE             FIELD         BEFORE -> NOW        CONFIDENCE  REVERT  ANCHOR
-------------------  ------------  -------------------  ----------  ------  -----------------
i-0bbb000000000000b  instanceType  t3.nano -> t3.small  ASSERTED    yes     operator-supplied
```

Two deliberate constraints:

**It can only fill an UNKNOWN.** The resolver sits last in the chain, so a `--set` never
overrides evidence. If you set a value the tool could already prove, the evidence wins and
you are told:

```
warning : --set i-0aaa000000000000a.instanceType=t9.wrong was ignored: the previous value
          is already proven to be 't3.micro'. The evidence wins; nothing was overridden.
```

**A selector that matches nothing is reported**, because a typo in a resource id must not
pass quietly:

```
warning : these --set selectors matched no changed field in this window: i-0typo.instanceType.
          Check the resource id and field name, or the chain id from a previous plan.
```

#### Why `--set` lives on `plan`, not on `revert`

The value becomes part of the plan document, so it is reviewable before anything is
touched, and `diff` and `revert` need no special case. The plan file stays the single
source of truth, and the audit trail records *that* a human asserted the value, which
selector they used, and when.

---

## The plan file

`plan.json` **is** the tool's state. No database, nothing stored in your account: keep it,
diff it, commit it, attach it to a ticket. It is what `diff` and `revert` consume.

The envelope is `rewindPlanVersion`, `tool`, `generatedAt`, `query`, `stats`, `warnings` and
`chains`. Every derived field — `confidence`, `capability`, `changeCount`, the change times —
is **recomputed on load** rather than read back, so a hand-edited file cannot make a chain
disagree with itself. They are still written, because a human reading the JSON wants them.

One chain, in full:

```json
{
  "chainId": "chn-03d8733342b7",
  "resourceType": "AWS::EC2::Instance",
  "resourceId": "i-0aaa000000000000a",
  "field": "instanceType",
  "fieldPath": ["instanceType"],
  "netBefore": "t3.micro",
  "netAfter": "t3.small",
  "confidence": "HIGH",
  "capability": "AUTO",
  "handler": "SET_EC2_INSTANCE_TYPE",
  "eventSource": "ec2.amazonaws.com",
  "eventName": "ModifyInstanceAttribute",
  "executable": true,
  "changeCount": 2,
  "firstChangeAt": "2026-09-22T17:21:00Z",
  "lastChangeAt": "2026-09-22T17:22:30Z",
  "anchor": {
    "value": "t3.micro",
    "confidence": "HIGH",
    "source": "cloudtrail-window",
    "evidenceEventIds": ["h0000003-0000-4000-8000-00000000h003"],
    "note": "previous value set by ModifyInstanceAttribute at 2026-09-16T11:00:00Z"
  },
  "revert": {
    "operation": "SET_EC2_INSTANCE_TYPE",
    "parameters": {"instanceId": "i-0aaa000000000000a", "instanceType": "t3.micro"},
    "steps": [
      {
        "api": "ec2:StopInstances",
        "params": {"InstanceIds": ["i-0aaa000000000000a"]},
        "condition": "EC2 rejects a change to instanceType on a running instance",
        "waitFor": "instance_stopped"
      },
      "… then ModifyInstanceAttribute, then StartInstances"
    ],
    "verify": {
      "api": "ec2:DescribeInstanceAttribute",
      "params": {"InstanceId": "i-0aaa000000000000a", "Attribute": "instanceType"},
      "expect": "t3.micro"
    },
    "warning": "instanceType can only be changed while the instance is stopped, so …",
    "executable": true,
    "targetValue": "t3.micro"
  },
  "changes": [
    {
      "sequence": 1,
      "eventId": "s0000001-0000-4000-8000-00000000s001",
      "eventTime": "2026-09-22T17:21:00Z",
      "eventName": "ModifyInstanceAttribute",
      "identity": "arn:aws:sts::111122223333:assumed-role/PerfAgentRole/perf-agent",
      "before": "t3.micro",
      "after": "t3.large"
    },
    "… one entry per change, each with before and after"
  ],
  "evidenceEventIds": [
    "h0000003-0000-4000-8000-00000000h003",
    "s0000001-0000-4000-8000-00000000s001",
    "s0000002-0000-4000-8000-00000000s002"
  ],
  "notes": [
    "2 changes to this field in the session; the value to restore is the one from before the first change"
  ]
}
```

---

## Supported fields

| Operation | Field | Mutating events | Best anchor available |
|---|---|---|---|
| `SET_EC2_DETAILED_MONITORING` | `monitoring` | `MonitorInstances`, `UnmonitorInstances` | earlier Monitor/Unmonitor (HIGH) → `RunInstances` (MEDIUM) |
| `SET_LAMBDA_PROVISIONED_CONCURRENCY` | `provisionedConcurrency` | `Put`/`DeleteProvisionedConcurrencyConfig` | earlier Put/Delete (HIGH) → `CreateFunction` ⇒ `NONE` (MEDIUM) |
| `SET_RDS_MULTI_AZ` | `multiAZ` | `ModifyDBInstance` carrying `multiAZ` | **the change's own `responseElements`** (HIGH, no history needed) |

Plus seven `ModifyInstanceAttribute` attributes, which share one plugin because EC2 exposes
them through one API, one `DescribeInstanceAttribute` shape and one `Modify` call. Anchoring
is the same for all of them: an earlier `ModifyInstanceAttribute` for that attribute (HIGH),
else `RunInstances` (MEDIUM).

| Field | Reverting it needs the instance stopped | In AWS Config |
|---|---|---|
| `instanceType` | **yes** | yes |
| `disableApiTermination` | no | no |
| `disableApiStop` | no | no |
| `sourceDestCheck` | no | yes |
| `instanceInitiatedShutdownBehavior` | no | no |
| `ebsOptimized` | **yes** | yes |
| `enaSupport` | **yes** | yes |

Ten fields reach AUTO in total. That number is not the tool's reach — it is how many fields
skip the review step. Everything else CloudTrail records still appears, at a lower tier.

Notes that matter in practice:

- **A multi-instance call becomes several chains.** One `MonitorInstances` covering two
  instances produces two chains, because their previous states may differ and a revert
  has to restore each one separately.
- **Lambda's `Resources` index is usually empty** for these events, so the resource is
  identified from `requestParameters`. An ARN and a bare function name resolve to the
  same chain.
- **`NONE` is a real value**, distinct from an unproven anchor. It is only inferred when
  the function's creation is visible *and* the window was not truncated.

### Adding a field

Nothing needs to be added for a change to be *seen* — that is the generic layer's job. A
plugin is what moves one field from MANUAL to AUTO, and it is bought in three sizes:

| You write | You get | Tier |
|---|---|---|
| `parse` | the change named your way instead of by its raw parameter path | RECONSTRUCTED / MANUAL |
| `+ anchor_from_event` / `anchor_from_creation` / `response_anchor` | better evidence for what it used to be | same tier, higher confidence |
| `+ read_live_value`, `revert_plan`, `apply_revert` | conflict checks and execution | **AUTO** |

Subclass `handlers.BaseHandler`, put the module in `handlers/aws/`, and add it to
`PLUGINS` in `handlers/registry.py`. Scanning, chaining, anchoring, diffing, reverting and
every report pick it up with no further registration.

The tier comes from the methods themselves: `capability_of` asks
`isinstance(handler, Actuator)`, and `Actuator` is a `runtime_checkable` protocol of exactly
those three methods. Writing them *is* the declaration — there is no flag to set, and a
half-finished plugin cannot claim AUTO by accident. `BaseHandler` deliberately does **not**
stub them, which is why `handlers/base.py` supplies defaults for two of the five Actuator
methods and none of the other three.

---

## Architecture

Eight layers. A module may import from a lower layer or its own; never upward. This is not
a convention — `tests/test_architecture.py` parses every module's AST and fails the build
on a violation, on an unplaced module, and on any function-local internal import used to
sneak around a cycle.

```
7  cli/        parser.py declares the interface, context.py is the only place
               arguments become AWS objects, commands.py joins them up
6  report/     table.py + one module per command; never decides anything
5  pipeline/   scan, plan, diff, revert, snapshot - one module per verb
4  resolvers/  the six anchor sources, tried in priority order
3  handlers/   protocols.py (the three roles), generic/ (works on anything),
               aws/ (one plugin per field)
2  trail/  store/  timeutil.py
1  domain/     the vocabulary: Chain, Change, Anchor, Confidence, Capability
0  errors.py  aws.py
```

Two rules do most of the work:

- **`domain/` imports nothing internal at all.** A separate test asserts this. It is what
  keeps `Chain` a value you can serialise, re-read and compare without dragging AWS, the
  plugins or the resolvers along.
- **The tiers are the interfaces.** `handlers/protocols.py` is three small protocols
  (`Parser`, `Historian`, `Actuator`) rather than one big abstract base class, so a plugin
  implements only what it can honestly do, and what it implements determines what the tool
  claims it can do.

---

## How CloudTrail is queried

Two shapes, chosen to avoid a dependency the tool cannot rely on:

- the **session window** is read with *no* `LookupAttributes` at all, and the identity is
  filtered in code. Windows are short, so reading everything is cheap, and it cannot miss
  a change just because CloudTrail did not index the resource;
- the **anchor lookback** uses one `EventName` attribute per API. `LookupEvents` accepts
  a single attribute per call, and `EventName` is an index that does not depend on
  resource tagging or on the service populating `Resources`.

The `ResourceName` index is never used — a test asserts this.

Only CloudTrail **Event history** is read: free, already on, no trail to configure,
90 days. The lookback is not filtered by identity, because whoever last set a field
established the value to restore, and that is often somebody else.

---

## Tests

`425 passed` in ~0.9s. No credentials, no network, sanitized fixtures only.

```bash
.venv/bin/python -m pytest
```

| File | Tests | The load-bearing ones |
|---|---|---|
| `test_architecture.py` | 125 | every module placed in a layer, **no import pointing upwards** (one case per module), `domain/` importing nothing internal, no function-local internal imports |
| `test_cli.py` | 63 | window parsing, `scan` with no `--identity` summarising by identity, `plan` still requiring one, **`?` never becoming a guess**, plan round-trip, exit codes, never using the `ResourceName` index, a valueless change never counted as work outstanding |
| `test_generic.py` | 56 | **every change reported with no plugin**, generic chaining with no API knowledge, which APIs can be inverted mechanically, **the resource a call acted on** (a `platformName` that is not a resource, one field never spread across a VPC and an SG, a creation attributed to what it created) |
| `test_phase4_sources.py` | 38 | `--set` parsing, ASSERTED labelling, **`--set` never overriding evidence**, a snapshot invalidated by an intervening change, Config closing the 90-day gap and bounded by the change time |
| `test_ec2_attributes.py` | 28 | seven attributes sharing one plugin, `--no-x` never `--x false`, which need the instance stopped, **no plugin asking the lookback for events it cannot anchor from** |
| `test_diff.py` | 27 | drift detection with an UNPROVEN anchor, **only-reads plus the guard proving it**, **never claiming "no drift" when nothing could be read**, credentials told apart from a bad resource |
| `test_revert.py` | 20 | dry run calling nothing, newest-first ordering, **planned calls matching issued calls**, a conflict appearing mid-run, per-field pre-checking, a silently ineffective write caught by verification, re-run idempotency |
| `test_anchors.py` | 14 | latest-same-field wins, failed calls ignored, `responseElements` outranking everything, `NONE` only from a visible creation, the retention gap producing an honest UNKNOWN |
| `test_chain.py` | 9 | repeated changes collapsing into one chain, **an intermediate value never becoming the revert target**, net-no-op, RDS anchoring with no history at all |
| `test_undo.py` | 11 | **`undo` calls nothing without `--confirm`** (with the write guard proving it), the diff running even when confirming, one conflicted field not stopping the others, the plan landing on disk in a dry run, `--exit-code` |
| `test_extensibility.py` | 9 | a runtime-registered plugin found rather than silently treated as generic, an Actuator with a missing method not claiming AUTO |

Every mutating API in the fake AWS client **raises unless a test opts in**, so a test
asserting "nothing was written" cannot pass vacuously; two tests prove that guard works.

Two fixtures. `agent_session.json` puts the hard cases in one window: a field changed twice,
a resource whose history is outside retention, a **failed** resize more recent than the real
one, Stop/Start noise, a same-API change by a **different identity**, and an RDS change that
anchors itself. `mixed_session.json` is the generality case — seven changes, **one** with a
plugin.

### What the fixtures did not catch

Running against a live account found five defects that 373 passing tests had not, which is
worth recording because the pattern was the same each time — a shape fixtures never produce:

| Defect | Consequence |
|---|---|
| a valueless chain crashed the `diff` table | the command failed outright |
| `plan --explain` printed `-> None` instead of `?` | a Python repr in operator output |
| a multi-change note claimed a value to restore on a chain with no values | the next line contradicted it |
| `diff` said "drift: none" when *nothing* could be read | JSON said `driftFree: false`; the table a human reads said the opposite |
| every SKIPPED row counted as work outstanding | `--exit-code` returned 3 after a fully successful revert |

Each fix came with a test that was verified to fail against the old code, and two of them
are class-level guards rather than instance fixes: one greps every renderer for a nullable
value reaching the page without `cell()`, the other asserts no plugin asks the lookback for
event names it cannot anchor from.

---

## Status

All four phases are complete, and the tool has been **run end to end against a live AWS
account**: `scan` → `plan` → `diff` → `revert --confirm`, with the reverted instance types
independently confirmed in CloudTrail and `DescribeInstances`.

Not started:

- **the third change type** (see limitation 2) — power transitions and existence.
- **`--access-key-id`** to scope a session precisely instead of by time window. CloudTrail
  records the access key of an `AssumeRole` session, which is exact where a window is a guess.
- **more `AUTO` fields**, each one a module plus a registry line.
- **multi-region and multi-account** in one run; a `--set` file for a large incident.

---

## Assumptions

1. **Identity matching** tries `userIdentity.userName`, `arn`, `principalId`, the session
   issuer, and the role-session suffix of an ARN. An exact match wins; otherwise a
   case-insensitive substring match is accepted, so `perf-agent` matches
   `arn:aws:sts::…:assumed-role/PerfAgentRole/perf-agent`.
2. **Identity filtering applies to changes, not to evidence.** The lookback is unfiltered on
   purpose: whoever last set a field established the value to restore, and that is often
   somebody else.
3. **One chain per (resource, field).** A `ModifyDBInstance` changing MultiAZ *and*
   `allowMajorVersionUpgrade` yields two chains — the plugin takes one, the generic layer the
   other. A field is never spread across more than one resource.
4. **Values are compared as display strings** (`"true"`, `"disabled"`, `"NONE"`), so a
   CloudTrail value and a Describe value compare unambiguously. Typed values appear only
   inside `revert.parameters`, where boto3 needs them.
5. **`--lookback-days` is capped at 90**, CloudTrail's event-history retention.
6. **Chain ids are stable** — `sha256(resourceType|resourceId|field)` — so plan files can be
   compared and `--only` ids survive a re-plan.
7. **Nothing is hardcoded** in `src/`: no account id, resource id, region or timestamp.

## Known limitations

1. **The 90-day wall is real and every mitigation is conditional.** `--use-config` needs the
   account to be recording, `--snapshot` needs you to have taken one, `--set` needs you to
   know the value. With none of those, a field last changed over 90 days ago stays UNKNOWN.
   RDS is the exception: it anchors from its own response and is immune.
2. **Only field changes are modelled; there are really three kinds.** A power transition and
   a creation both appear as a pseudo-field named after the event (`StopInstances`,
   `RunInstances`) at `DISCOVERED` with `? -> ?`. Nothing is hidden and nothing false is
   claimed, but two things follow: a stop **and** a start net to zero and should collapse to
   `ALREADY_AT_ORIGINAL` instead of showing as two open rows, and a creation's revert is a
   *termination*, which needs a destructive gate before it could ever be offered. The
   `UNCHECKABLE` wording misleads too — it blames a missing plugin when the truth is that
   there is no field.
3. **Generic resource and field detection is heuristic.** Three tiers of evidence pick the
   resource a call acted on and only the strongest is used. Live data forced this: a
   35-minute window dropped from 1166 "changes" to 306 once one `RunInstances` stopped
   becoming 224 rows and `platformName: "Amazon Linux"` stopped counting as a resource. It is
   still a heuristic; everything inferred is printed, and the failure mode is noise at a low
   tier rather than a bad revert.
4. **`diff` cannot check a field with no plugin.** No Describe call reads it, so those rows
   come back `UNCHECKABLE` — the change is recorded, but not whether anyone touched it since.
5. **An `ASSERTED` value is exactly as good as the person who typed it.** The tool verifies
   the revert landed; it cannot verify that `t3.nano` was really the old size.
6. **CloudTrail does not record everything.** Parameters can be omitted or redacted,
   `responseElements` can be null, some services emit only data-plane events, and a service
   default is never treated as evidence. There is no general workaround.
7. **Eventual consistency.** Delivery can lag ~15 minutes; a window ending inside that gets a
   warning and should be re-run.
8. **A plan is not live state.** `scan` and `plan` make no live calls, and a `diff` is a
   point-in-time read with no lock. That is why `revert` re-reads immediately before each
   field rather than trusting either.
9. **`revert` is not transactional and reverts one field per chain.** If the fifth field
   fails the first four stay reverted; the run reports exactly what happened and re-running is
   safe. Neighbouring state is left alone — an instance ends in whatever power state the
   revert found it in.
10. **An EC2 resize is disruptive.** It stops and restarts the instance: instance-store data
    is lost and public IPv4 addresses can change. The dry run prints this before you confirm.
11. **Credentials expire mid-session**, and then every live read fails at once. `diff` tells
    that apart from a per-resource failure and refuses to report drift:

    ```
    drift      : UNKNOWN - not one field could be read, so nothing was compared
    CREDENTIALS: 2 read(s) failed on your credentials, not on the resource.
    ```

12. **Single region, single account per run.** `--blame` likewise only sees 90 days of event
    history, so a conflict with no blame record is not evidence that nobody did it.
