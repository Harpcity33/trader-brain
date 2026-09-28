# Titan full-live architecture

## Production lifecycle

```text
existing Massive producer (SHADOW, read-only)
                    │
                    ▼
query-only local adapter ── sequence/freshness/completed-bar checks
                    │
                    ▼
bounded active set ── IBKR contract/tradability + deterministic geometry/quality
                    │
                    ▼
expiring signed plan ── exact quote/spread/depth/session evidence
                    │
                    ▼
fresh whole-broker snapshot ── account-wide deterministic risk + cash reserve
                    │
                    ▼
durable plan + risk reservation + intent (single SQLite transaction)
                    │
                    ▼
supported broker review/place boundary ── stable client reference
                    │
          timeout/ambiguous result
              ┌─────┴─────┐
              ▼           ▼
        known result    UNKNOWN ── reserve exposure, notify, no retry
              │           │
              └─────┬─────┘
                    ▼
strictly newer broker reconciliation + idempotent fill deltas
                    │
                    ▼
per-fill protection obligation ── submitted is not working
                    │
                    ▼
verified broker-held protection / cancel-before-close / safe close
                    │
                    ▼
confirmed-event outbox ── retry/dedupe/redaction ── EOD evidence
```

The account reconciliation and protection path runs before discovery on every
tick. Data/research loss blocks affected entries but does not suspend position
management. Authentication, storage, broker, lifecycle, and notification
failures become durable incidents. Manual or unowned broker activity is never
absorbed as strategy-owned exposure.

## Authority barriers

An order mutation requires all of these simultaneously:

1. a reviewed content-addressed release and matching durable runtime identity;
2. the sole kernel writer lock and matching database writer lease;
3. signed configuration with live entries and the local mutation interlock
   explicitly enabled;
4. a consumed, unexpired, one-use owner activation record bound to release,
   policy, account, and schema;
5. fresh complete whole-broker evidence, no unknown exposure, and verified
   risk/session/capacity gates;
6. a release-bound IBKR provider-authority receipt proving that the exact
   daemon place/cancel contract is supported without manual Transmit or an API
   precaution bypass; and
7. an exact current local preflight plus final account/market revalidation at
   the transport boundary.

The current checked-in release intentionally fails several of those barriers.
There is no force flag, environment-variable live override, database edit
procedure, confirmation auto-click, or fallback broker credential scraper.

## Safety state

The SQLite writer uses WAL plus full synchronous durability. It persists:

- content-hashed plans and account-wide risk reservations;
- stable intent/client IDs before submission;
- monotonic broker order/fill revisions;
- separate required, submitted, working, failed, and satisfied protection;
- irreversible daily loss/profit-crossing and closeout latches;
- manual/unknown exposure, incidents, notification attempts, and latency;
- an append-only event chain and release/config/policy/runtime binding.

Only authoritative, strictly newer broker evidence can resolve an ambiguous
submission, establish working protection, prove cancellation, or prove
flatness. Independent exit capacity prevents two sell paths from reserving the
same shares.

## Existing lanes and boundaries

- The old `robinhood-momentum-engine` is paused and is never a writer for the
  IBKR account. A Codex heartbeat may supervise the new runtime, but the local
  release-bound daemon is the sole autonomous account writer.
- Premarket is analysis-only. Its 30-minute results carry no execution
  authority and must be refreshed after the regular open.
- Research and the Massive shadow producer have zero broker authority.
- Aggressive-paper evidence and ledgers remain isolated from live authority.
- Options remain a separately authorized attended lane and are not enabled by
  this equity release.
- Notification delivery is owned by a separately supervised worker. Delivery
  failure pauses new entries but cannot interrupt reconciliation, protection,
  exits, or mandatory closeout.

## Deployment states

`BUILT` means source/release tests pass. `INSTALLED_PAUSED` means a verified
release is present with authority off and a disabled plist staged only inside
its isolated subtree. `RUNNING_RECONCILE_ONLY` requires an observed process and
fresh reconciliation. `ACTIVE` requires consumed owner activation plus a
subsequent clean service-side reconciliation. These states are never inferred
from filenames, configuration labels, or scheduler text.
