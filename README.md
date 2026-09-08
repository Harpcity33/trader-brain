# Trader Brain / Titan Momentum

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
- `src/titan_brain/live/massive_adapter.py`: query-only compatibility reader for
  the existing local Massive-backed Titan store. Shadow evidence never grants
  execution authority.
- `src/titan_brain/live/notifications.py`, `latency.py`, and `eod_live.py`:
  deterministic redacted events, durable delivery, stage-separated timing,
  and create-only EOD packets.
- `src/titan_brain/live/activation.py`, `writer_lock.py`, `release.py`, and
  `cli.py`: single-account ownership, hash-bound one-use activation, verified
  release loading, health/status, pause, closeout, deactivation, and rollback.
- `scripts/build_full_live_release.py` and `install_full_live_paused.py`:
  deterministic release packaging and isolated PAUSED-only installation.

The checked-in full-live configuration deliberately has no live authority.
Current Robinhood tooling remains attended and lacks the daemon broker client,
whole-broker advanced-order visibility, and client-reference reconciliation
needed for true autonomy. Risk-overlay provenance, numeric liquidity/score
thresholds, live validation providers, and the existing Codex notification
bridge are also unresolved. These are signed activation blockers, not TODOs
that the runtime guesses around.

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
