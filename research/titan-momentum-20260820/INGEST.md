# Titan Momentum Research — Trader Brain Ingest

Source: Titan Momentum Intelligence Lab handoff generated 2026-08-20.

## Status

**RESEARCH / SHADOW-TEST ONLY.** The source package explicitly states that its proposals are unapproved and must not be imported into a live trading runtime without owner review. Codex must treat this file as research intelligence, not executable live parameters.

## What Trader Brain should learn now

1. **Acceleration Score is a movement selector, not directional conviction.** The study found higher score associated with larger excursion in both directions while reward/risk asymmetry stayed essentially flat. Do not size a trade merely because the score is high.
2. **Trigger quality matters more than raw mover rank.** A hot name still needs a valid entry structure and exit plan.
3. **VWAP reclaim deserves first-priority shadow testing.** It ranked strongest among tested entry structures on stop-independent MFE/|MAE|; HOD breakout ranked weakest among the listed structures.
4. **Keep the momentum-state focus.** ACCELERATING and BREAKOUT were among the strongest tested states for excursion asymmetry. Avoid interpreting state alone as directional prediction.
5. **Do not chase extension.** Add explicit awareness of distance above VWAP and distance below HOD. The study found very extended entries degraded asymmetry, with inversion beyond extreme short-ATR extension.
6. **Exhaustion needs confirmation.** A large upper wick was the key necessary condition in the proposed exhaustion logic; large upper wick + well off highs + sector reversal was the strongest tested no-entry/exit combination.
7. **Open-position management should be dynamic.** The study favored active trailing over hold-to-15:55. TRAIL_1M was strongest pooled; VWAP_RECLAIM + TRAIL_5M was the strongest tested structure/exit pairing, but this is in-sample and remains shadow-test only.
8. **PCS is more appropriate than entry score for managing an open position.** Retain Position Continuation Score as a separate concept from entry selection.
9. **Under-$5 protections remain important.** Recent reverse split and severe listing risk remain strong rejection signals. Fresh catalyst, spread, premarket dollar volume, and top-decile movement remain useful gates to shadow-test. Do not weaken dilution rules from this single sample without independent confirmation.
10. **Late entries deserve extra skepticism.** 14:30–15:30 ET was the weakest tested entry window.

## Data-quality guardrails

- 23/23 behavioral leakage checks passed in the research quality audit.
- The reconstructed Acceleration Score did not populate Titan's live 92+ tier, so the live 75/85/92 band edges were **not validated**.
- Catalyst quality was missing on a large share of observations; free-float and market-cap fields also had material point-in-time limitations.
- NBBO spread was sampled rather than continuously available and was null on many observation rows.
- Options delta/OI and true option NBBO spread filters were not testable under the available Massive entitlements.
- Quantitative market data came from one provider and was not independently cross-provider corroborated.
- Research is based on a 90-session window ending 2026-08-19; do not overfit it into permanent rules.

## Codex operating instruction

Use these findings as **additional evidence in paper/shadow evaluation**. When generating a candidate decision, record whether the candidate agrees or conflicts with the Titan findings (VWAP reclaim, extension, momentum state, exhaustion, reverse-split/listing risk, late-entry window, PCS/exit behavior). Do not automatically modify live thresholds or execution logic from this ingest.

## Recommended validation loop

For each future Trader Brain paper/live candidate, log:

- setup structure
- price vs VWAP and HOD at entry
- short-ATR extension if available
- momentum state
- catalyst freshness/category
- reverse split / listing / dilution flags
- spread and dollar-volume quality
- entry time
- PCS evolution after entry
- exit rule used
- MFE, MAE, realized R, and whether a 1m/5m trail would have improved the result

Compare these fields against the Titan hypotheses weekly and only promote a shadow rule after an independent forward sample supports it.
