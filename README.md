# Trader Brain / Titan Momentum

## Kiro completion handoff — September 18, 2026

Start with [KIRO_HANDOFF.md](KIRO_HANDOFF.md) for the current owner-approved
session-P&L model, implemented repairs, remaining acceptance criteria, offline
test commands, and local-only deployment prerequisites. The installed account
is **PAUSED**; the new session model is **not production-ready or activated**.
The historical component overview below does not override that current status.
This private repository is a source handoff, not a transfer of credentials,
broker reports, runtime databases, SDK binaries, or trading authority.

This repository contains the September 8 full-live production implementation
and the earlier research, risk, scoring, paper, and attended-option components.
The new runtime is a persistent, single-writer equity lifecycle:

`market evidence -> independent validation -> account-wide risk -> durable intent -> broker boundary -> fill reconciliation -> verified protection -> non-overlapping exit -> durable notification -> EOD evidence`

The implementation is fail-closed. It does not treat a shadow plan, an
`ACTIVE` scheduler label, a submitted stop, a cancel acknowledgement, or a
local notification write as proof of execution, protection, cancellation, or
user delivery. Every possible unknown submission stays reserved and blocks new
risk until strictly newer authoritative broker evidence resolves it.

## Full-live components

- `src/titan_brain/live/state.py`: durable SQLite state, append-only hash-chain,
  reservations, intents, broker evidence, fill/protection obligations, latches,
  incidents, outbox, and latency samples.
- `src/titan_brain/live/service.py`: fixed account-first lifecycle ordering and
  persistent runner.
- `src/titan_brain/live/execution.py` and `lifecycle_actions.py`: durable entry,
  protection, exit, cancel, and reconciliation boundaries.
- `src/titan_brain/live/pipeline.py`: quality-ranked, whole-share entry pipeline
  with independent evidence, expiring plans, deterministic sizing, and
  account-wide risk.
- `src/titan_brain/live/massive_adapter.py`: continuous Massive stream ingestion
  separated from bounded historical gap repair. Shadow evidence never grants
  execution authority, and second aggregates cannot masquerade as completed
  minute bars.
- `src/titan_brain/live/notifications.py`, `latency.py`, and `eod_live.py`:
  deterministic redacted events, durable delivery, stage-separated timing,
  and create-only EOD packets.
- `src/titan_brain/live/activation.py`, `writer_lock.py`, `release.py`, and
  `cli.py`: single-account ownership, hash-bound one-use activation, verified
  release loading, health/status, pause, closeout, deactivation, and rollback.
- `scripts/build_full_live_release.py` and `install_full_live_paused.py`:
  deterministic release packaging and isolated PAUSED-only installation.

The checked-in full-live configurations deliberately have no live authority.
`config/full_live_ibkr.json` defines an isolated IBKR account-ending-3103
profile using Massive for market data and the official local TWS API for
broker reads, contract evidence, reconciliation, and the guarded order
transport. It cannot reuse the older account-ending-7153 state. The executable
runtime now contains the full autonomous path—quality-ranked discovery,
whole-share sizing, exact-reference entry, per-fill protection, durable target
and mandatory closeout, conservative unknown recovery, and an independent
notification worker—but the selected signed profile remains staged and
PAUSED. Unresolved owner policy, risk provenance, IBKR Read-Only/API-precaution
and external-data authority, exhaustive account reads, release-bound command
authority, and notification delivery remain activation blockers rather than
facts the runtime guesses or bypasses.

Premarket is analysis-only: the service can persist one read-only ranking at
each 30-minute slot from 07:00 through 09:00 America/New_York, with explicit
`execution_authority=false` and restart-safe slot de-duplication. No premarket
or after-hours order path exists. Once an approved autonomous policy and every
external receipt are incorporated into a new immutable release, regular-hours
entry is limited to 09:35–15:30; protection, exits, reconciliation, and closeout
continue independently of discovery and notification delivery. A built or
installed candidate is not evidence that this writer is activated or running.

See:

- `validation/full-live/2026-09-08/DEPLOYMENT_STATUS.md`
- `validation/full-live/2026-09-08/POLICY_MAP.md`
- `validation/full-live/2026-09-08/CAPABILITIES.json`
- `validation/full-live/2026-09-08/FAILURE_MATRIX.json`
- `validation/full-live/2026-09-08/OPERATIONS.md`

Run the standard-library suite with Python 3.11 or newer:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

No live order, cancel, account change, transfer, or scheduler activation is a
test. Passing engineering tests does not prove current broker capability,
notification delivery, trading performance, or profit.
