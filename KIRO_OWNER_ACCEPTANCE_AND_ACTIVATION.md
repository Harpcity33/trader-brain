# Kiro handoff-back — owner acceptance & activation checklist

Author: Kiro (autonomous completion of the trader-brain / Titan handoff). This
is a **report to the owner**. It is NOT an owner-approval artifact and NOT part
of the release provenance set. It records the state Kiro drove the system to,
what is proven offline, and the exact live evidence and steps that only the
owner can perform to reach "ready" and activate.

Branch: `kiro/step0-hermetic-tests` (PR #9). Read alongside
`KIRO_AREA5_OWNER_BARRIERS.md` (the detailed gate analysis).

---

## 1. Where the system stands

Areas 1–6 of the handoff are engineered and covered offline; Area 5 is complete
as its **offline slice** (the session-risk model is built, unit-tested, and
wired reachable behind a still-closed gate). The system is installed-ready in a
**paused** posture. It is **NOT live-ready**, and Kiro does not represent it as
such: the final activation is the owner's distinct action after the live
evidence below genuinely passes.

The readiness gate `SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE`
(`src/titan_brain/live/policy.py:380-384`) is **intact** — never weakened,
deleted, or bypassed. It fires the moment the session risk model is selected, so
the session model cannot run today; the live pipeline fails closed.

## 2. What is PROVEN offline (verifiable now, no broker/network/Gateway)

Run from a clean checkout with Python 3.11+ (this build used python3.12):

```sh
git status --short                                   # clean tree
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests
python3 -I -S -B scripts/validate_repository.py      # -> "status": "PASS"
git diff --check                                     # whitespace clean
```

- **Offline suite green:** 1768 tests pass, 3 skipped (the 3 skips are the
  documented live-only cases). No network, broker, token, paid service, Gateway
  session, or keychain password is required.
- **Repository validation PASS:** `scripts/validate_repository.py` reports
  `"status": "PASS"` over 215 Python / 34 JSON / 4 TOML files.
- **Reproducible release build (Area 7 acceptance item):** building twice from
  HEAD `81a9cc2` produced **byte-identical** archives:
  - `release_id`     `65af48d3822877689ce0c1ee17859cc6aaf50c404dce1f5d4e7ff76b7adcce65`
  - `archive_sha256` `34e77d76d7c5a7b2c4cfb2d15fe000adcb75241909b60a459a397e66e3e6cd97`
  - `manifest_sha256``08fb44fb750a85144b3c1c05aec48580fa3540da6c8cd18fc88eaf8209815a99`
  - command: `python3 -B scripts/build_full_live_release.py --output-dir <dir outside repo> --config config/full_live_ibkr.json`
  - (release_id / hashes will change if source or config changes — re-run to
    confirm against whatever HEAD you install.)

## 3. What Kiro completed on PR #9 (offline, gate stays closed)

Every item below keeps the UNAVAILABLE gate closed and has a test asserting
fail-closed behaviour / that the blocker remains. Massive stays the market-data
provider throughout.

- **Area 1** — reporting-persistence evaluator; honest coverage-refusal; blind-
  interval sticky incident; takeover/resume FSM; plan-freshness external-change
  invalidation.
- **Area 2** — proved no broker-subscription accumulation across disconnect.
- **Area 3** — pinned accounting-replay determinism across decimal contexts.
- **Area 4** — pinned incurred-vs-future fee no-double-charge.
- **Area 5 (offline slice)** — pipeline session-mode branch reachable & fails
  closed; pre-submit external-change/expiry gate in the order path;
  handoff-to-session-state binding (pure adapter); session store threaded
  through the discovery composition into the IBKR pipeline.
- **Area 6** — verified discovery/execution acceptance is already enforced;
  added the exact-TTL plan-expiry-boundary pin.
- **Area 7** — reproducible-build proof (above) + this checklist.

## 4. LIVE evidence the OWNER must gather (cannot be produced offline)

These lift the gate. Synthetic data cannot satisfy their signed measurement
boundaries.

1. **P8 — Authenticated reconciled session P&L.** Reconciled broker observations
   (sale proceeds − purchase costs + residual long bid value − signed fees) vs
   the frozen pre-trade balance, from the live IBKR Gateway for the intended
   account.
2. **P9 — Reporting-persistence proof.** Two genuine readings of the Gateway
   Master API client-id / reporting scope — one before and one after a
   controlled restart — proving persistence. The offline evaluator
   `reporting_persistence.py` renders the verdict; the readings need the live
   Gateway.
3. **P10 — Fresh current bid marks.** Correctly-scoped current bid marks for any
   residual permitted inventory (bid-to-stop open-risk revaluation), from live
   Massive market data. Blocks on `SESSION_OPEN_RISK_REVALUATION_REQUIRED` until
   supplied.

## 5. Deferred live-integration seams (built to the boundary; final wiring is a live step)

These are the last hooks between the completed offline modules and the running
system. Each is safe to leave unwired (the pipeline fails closed without them);
each is a small, well-located change best made and verified against a live
Gateway, not offline:

- **(i) Session-store open at startup.** Open `SessionTradingStore` from the
  install root alongside `LiveStateStore` at service/CLI startup and pass it into
  `build_discovery_executor(...)`. The composition already accepts it
  (`session_trading_store`, default `None`); the open/close lifecycle belongs to
  whoever opens `LiveStateStore`.
- **(ii) Recovery handoff call site.** Call `begin_recovery_handoff(...)` at the
  writer-lease RECOVERED branch (reading the session store at takeover). The
  pure adapter (`handoff_binding.py`) is built and tested; only the live seam
  remains.
- **(iii) Plan fingerprint at submit.** Supply `plan_bound_fingerprint` to
  `submit_entry` (computed from the sizing snapshot via
  `account_exposure_fingerprint`) so the pre-submit freshness gate is armed.

## 6. Owner activation checklist (owner-only actions, in order)

1. Revoke the temporary GitHub PAT used for pushing to PR #9 (if not already).
2. Review PR #9 and merge it into `main` when satisfied.
3. Select the session risk model in a reviewed release and build it
   (`build_full_live_release.py`); confirm the build is reproducible against the
   HEAD you intend to install.
4. Perform the state-preserving migration — do NOT erase existing
   baseline/history/incident/loss state to make migration or readiness succeed.
5. Install **paused** on the intended host (`install_full_live_paused.py`);
   confirm installed source/config hashes match the reviewed release.
6. Complete seams (i)–(iii) against the live Gateway and re-run the offline
   suite + validation green.
7. Gather P8, P9, P10 against that release on the live IBKR Gateway / Massive.
8. Run the owner's doctor/readiness/rehearsal on the live host (never run offline
   by Kiro). Confirm single-writer ownership, actual notification delivery, and
   current broker/data readiness.
9. Only when P8–P10 pass and readiness is proven: **you** declare it ready and
   **you** activate. Kiro does not activate, place orders, reset a baseline,
   replace credentials, or lift the gate.

## Bottom line

Kiro drove Areas 1–6 (Area 5 offline slice) to a paused, install-ready,
reproducibly-buildable state with a green offline suite, keeping the readiness
gate closed throughout. The remaining path to "ready" is exactly the live
evidence P8–P10, the three deferred live-integration seams, and the owner's
selection / migration / paused-install / readiness / activation — all on the
owner's live environment. The owner declares readiness and activates.
