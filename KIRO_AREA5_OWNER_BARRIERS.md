# Kiro completion report — Area 5 owner/live barriers

Author: Kiro (autonomous completion of the trader-brain handoff). This is a
**report back to the owner**, not an owner-approval artifact and not part of the
release provenance set. It records precisely what remains before the
session-trading risk model can be selected and activated, and why those steps
cannot be done from this source-only handoff.

## Summary

Areas 1–4 are engineered and covered offline (see `KIRO_WORK_PLAN.md` in the
build scratch for the per-unit record; committed on branch
`kiro/step0-hermetic-tests`). Area 5 (production session-risk integration) has a
hard boundary: the model is fully built and unit-tested but is deliberately
wired to **no live consumer**, and the
`SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE` gate
(`src/titan_brain/live/policy.py:380-384`) fires the moment the session model is
selected. Lifting that gate needs evidence that only the owner's live
environment can produce. It has **not** been weakened, deleted, or bypassed.

## What is TRUE in the code today (safe state)

- If `config["risk"]["model"]` is set to the session model, the live pipeline
  **fails closed**: `build_account_risk_snapshot` returns
  `SESSION_RISK_SNAPSHOT_PATH_REQUIRED` (`pipeline.py:617-618`) and
  `update_session_latch` raises `SESSION_RISK_REQUIRES_SEPARATE_DURABLE_STATE`
  (`risk_runtime.py:558`). The session model therefore cannot run at all today.
- The risk-evaluation core is correct: `evaluate_entry` dispatches session mode
  to `_evaluate_session_entry`, which enforces the fixed baseline, monotone
  state, staleness gate, and full downside/cost reservation.
- `test_live_session_pipeline.py` asserts the UNAVAILABLE blocker is present and
  the legacy path fails closed for session mode. These guarantees are intact.

## OWNER / LIVE barriers to lift the gate (cannot be done offline)

1. **P8 — Authenticated reconciled session P&L.** Genuine, reconciled broker
   observations of sale proceeds minus purchase costs plus residual long bid
   value minus signed fees, measured against the frozen pre-trade balance, from
   the live IBKR Gateway for account ending 3103. Synthetic data cannot satisfy
   the signed measurement boundary.
2. **P9 — Reporting-persistence proof.** Two genuine readings of the Gateway
   Master API client-id / reporting scope (the 19735 change), one before and one
   after a controlled restart, proving persistence. The offline evaluator
   `src/titan_brain/live/reporting_persistence.py` decides the verdict; the
   readings themselves require the live Gateway.
3. **P10 — Fresh current bid marks.** Correctly-scoped current bid marks for any
   residual permitted inventory (for bid-to-stop open-risk revaluation), from
   live Massive market data. Blocks on `SESSION_OPEN_RISK_REVALUATION_REQUIRED`
   until supplied.
4. **Model selection + migration + paused install + activation.** Selecting the
   session model in a reviewed release, a state-preserving migration, a paused
   install on the intended host, and the final activation are the owner's
   distinct actions (Area 7 + the owner's separate activation), performed only
   after P8–P10 genuinely pass against that release.

## What Kiro can still do offline (does NOT lift the gate)

- Bind the pure, fail-closed modules into the production path with fixture
  tests: `plan_freshness.py` (pre-dispatch external-change invalidation) and
  `handoff.py` (recovery/takeover), and optionally make the session snapshot
  builder reachable in the pipeline behind the still-closed gate — all asserting
  fail-closed behaviour when live evidence is absent, and that the UNAVAILABLE
  blocker remains. Pending the owner's direction on how far to wire.

## Bottom line

The project is NOT live-ready, and Kiro will not represent it as such. When the
offline wiring the owner authorises is complete, the remaining path to "ready"
is exactly P8–P10 plus selection/migration/paused-install/activation — all owner
actions on the owner's live environment. The owner declares readiness and
activates; Kiro does not.
