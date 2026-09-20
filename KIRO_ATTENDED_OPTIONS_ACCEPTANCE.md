# Attended options — owner acceptance & activation checklist

Author: Kiro. This is a **report to the owner**, not an approval artifact, a
completed live integration, or an authorization to trade. It records the state
the attended-options work reached and the exact owner-only path from here.

Branch: `kiro/attended-options-integration`. Consolidates Kiro PR #9 (base),
PR #10 (recovered SDK), and PR #11 (milestone-1 analyzer), then milestones 2–10.

---

## 1. What was built (milestones 1–10)

All additive, offline, analysis/paper-only, with every existing stock-path
rejection gate and production config left unchanged (`options_enabled` remains
`false`; `SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE` remains closed):

- **M1** variable-risk calculator (`option_trade_analysis.py`) — three distinct
  losses, floor sizing, fail-closed (from PR #11).
- **M2** canonical option identity + deterministic wire form
  (`ibkr_option_orders.py`) and multiplier-aware accounting
  (`ibkr_option_accounting.py`).
- **M3** permissions/data/funding evaluator (`ibkr_option_funding.py`) —
  fail-closed on permission/entitlement/visibility/settlement/staleness/funds.
- **M4** thesis + joint scenario generator (`option_scenario_analysis.py`) —
  conservative executable bids, no invented probabilities.
- **M5** versioned options risk policy (`option_risk_policy.py`) — per-trade
  dollar budgets replacing a fixed percentage, $/%-premium/%-equity framing,
  correlated aggregation, equity ceiling; legacy loss latch preserved.
- **M6** bound attended review ticket (`option_attended_ticket.py`) — approval
  bound to a fingerprint of all material facts; any change invalidates it.
- **M7** overnight lifecycle + cumulative P&L (`option_lifecycle.py`) —
  lifetime P&L preserved across rollover/restart; unfilled close never flat.
- **M8** manual-intervention reconciliation gate
  (`option_reconciliation_gate.py`) — blocks on any unreconciled account-wide
  change or capture gap.
- **M9** end-to-end **paper** orchestrator (`option_paper_orchestrator.py`) +
  runnable demo (`scripts/titan-option-paper-demo`).
- **M10** this checklist + a verified reproducible release build.

## 2. What is PROVEN offline (verifiable now; no broker/network)

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests
python3 -I -S -B scripts/validate_repository.py            # -> "status": "PASS"
python3 -I -S -B scripts/titan-option-paper-demo           # end-to-end paper demo
```

- Full combined suite green (1949 pass / 3 skip at this writing); repo validator
  PASS.
- The paper demo runs the full lifecycle and every fault path fails closed
  (happy path completes; stale data, insufficient budget, manual intervention,
  rejected/unknown fills each stop at the right stage and open no position).
- The release build is REPRODUCIBLE with the option modules present: two builds
  from HEAD `22a94ce` produced byte-identical archives
  (`release_id 568fe7df46246810ae2e38a54e23c7e608e6d0ae5dadae18dda7f047e43f0bd7`,
  `archive_sha256 93a582eff06f349c22431c1799ea920886708754f1d4155217a7a892450c6825`).
  (Identity changes with source/config — re-verify against whatever HEAD you
  install.)

## 3. The critical scope boundary — options are NOT live-wired

**The option modules are analysis- and paper-only. They are deliberately NOT
wired into the live execution / policy / service path.** There is no live option
order route, no autonomous option execution, and no option broker transport
injected. `config/full_live_ibkr.json` still sets `options_enabled=false` and
the stock-only wire gates in `ibkr_orders.py` / `ibkr_sdk.py` still reject
non-STK contracts.

Consequently, installing this release does NOT enable options trading. Enabling
options for live trading is a **separate, reviewed, owner-authorized** effort
beyond this checklist: wiring the option identity/accounting/policy into the
live composition, an authenticated option broker transport, and an options
supported-execution config — each reviewed, none done here.

## 4. Live evidence the OWNER must gather (cannot be produced offline)

Nothing here authenticates a broker. Before any live option trading:

1. **Real option permissions & market-data entitlement** for the account
   (the M3 evaluator decides the verdict; the readings require the live
   Gateway).
2. **Authenticated funding** — settled cash, reserved funds, pending
   commitments, settlement status — from the live account.
3. **The existing full-live P8/P9/P10 evidence** (authenticated reconciled P&L,
   reporting-persistence across restart, fresh marks) — still required and still
   owner-only.

## 5. Owner activation checklist (owner-only, in order)

1. Review the integration PR (`kiro/attended-options-integration`) and merge PR
   #9 / #10 / #11 and this branch in the order you accept them.
2. Run the offline checks in §2 yourself, including the paper demo.
3. Decide whether to pursue live options at all; if so, commission the separate
   reviewed effort in §3 (live wiring + transport + supported-execution config).
4. Gather §4 live evidence against the live Gateway.
5. Build the reviewed release; install PAUSED; verify installed identity +
   runtime wiring + broker/data evidence agree.
6. Only the owner performs live activation, after reviewing the completed scope.

Kiro does not activate, place orders, inject a live transport, enable options,
reset a baseline, replace credentials, or lift any gate. The owner declares
readiness and activates.

## Bottom line

The attended-options analysis and paper lifecycle are built, tested, and
reproducibly buildable, with production untouched and every gate closed. The
path to live options is: owner review/merge, then a separate reviewed live-wiring
effort, then owner-gathered live evidence, then owner activation — all on the
owner's environment. Kiro built the offline half; the live half is the owner's.
