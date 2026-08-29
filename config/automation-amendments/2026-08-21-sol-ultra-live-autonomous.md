# SOL ULTRA MASSIVE LIVE-AUTONOMOUS POLICY — LATEST USER AUTHORIZATION

This amendment explicitly promotes the SOL Ultra candidate-selection refinements to the user's autonomous Robinhood Titan Momentum Engine. It supersedes every earlier clause that treats a Massive/Titan score, the existence of fresh news, or a momentum-state label as a standalone entry veto. Massive remains the market-data and event source; the active Robinhood Agentic account remains the only order-routing and account-state authority.

## Ranking and directional qualification

- Keep computing `signal_strength`, Acceleration Score, normalized structure score, and `weighted_opportunity_score`, but use them for attention ranking, cohort measurement, and confidence calibration—not as minimum entry thresholds. Earlier numeric score minima, including the score conversions in the GLOBAL 25%-LESS-STRICT AMENDMENT, no longer prohibit an otherwise fully qualified entry.
- A high score never creates directional authority or exceptional sizing by itself. A low score never vetoes an exact, independently verified directional setup. Record the score and available evidence at decision time for later calibration.
- Fresh news or an identified catalyst is optional in every authorized lane. Continue current issuer-newsroom, SEC/filing, exchange/listing, corporate-action, dilution, adverse-news, and promotion/manipulation checks. The ability to perform those checks remains mandatory; any adverse or unresolved result remains a hard rejection.
- DORMANT, BUILDING, ACCELERATING, BREAKOUT, PARABOLIC, EXHAUSTED, and FADING are context labels, not authorization gates. Act on the underlying causal evidence. Exhaustion, failed structure, loss of support/VWAP, adverse liquidity, or invalidation remains a hard rejection even if the label is delayed or favorable.
- Market/sector confirmation is context and a tie-breaker, not a standalone veto. Missing 90-day analogs or sector data reduces evidence coverage but does not automatically reject a setup when every contemporaneous execution and risk input is available.
- Rank by currently executable opportunity quality and remaining move capacity, not by raw percentage gain or whichever score is highest.

## Live entry posture

- Prioritize the study's more stable above-$5 cohorts: the opening 25 minutes, opening-range breakouts, VWAP reclaims/holds, and first controlled pullbacks after an impulse. Prefer these over late vertical percentage leaders.
- For an above-$5 INITIAL PROBE, retain the exact live trigger, one completed holding/higher-support/clean-retest bar, and at least two independently positive soft signals from price acceleration, volume acceleration, relative volume, and sector confirmation; at least one must be price or volume acceleration. Remove only the numeric score/subtotal floors.
- For a normal-size build, require a completed controlled base, higher low, or five-minute retest with renewed price acceptance and volume pace. Never build a losing or weakened thesis.
- Premarket, under-$5, and long-option lanes lose their numeric score and fresh-catalyst minima but retain every lane-specific time, eligibility, structure, Level 2, spread/depth, dilution/listing/manipulation, chase, protection, and no-overnight rule. Under-$5 TIGHT_BASE alone is not sufficient because that cohort was unstable in the SOL Ultra study.
- Maintain at most one open position and one active entry intent per symbol. Permit at most one completed round trip per ticker-session unless a materially new base/retest forms after the prior thesis is closed and the re-entry is independently documented. Duplicate or repeated Massive triggers are ranking evidence, not additional order authority.

## Non-negotiable execution and risk gates

- Require a current Massive NBBO no more than 15 seconds old and a fresh Robinhood quote/Level 2 check. If Massive and Robinhood materially disagree, reject the entry.
- Require both the applicable absolute spread/depth rule and quoted spread no greater than 15% of entry-to-structural-stop risk.
- Require session eligibility, exact contemporaneous trigger, price inside the reviewed limit and controlling ATR/chase ceilings, expanding volume pace, positive structural risk distance, no exhaustion lock, no unresolved halt, and a clean Robinhood pre-trade review.
- The current GLOBAL 25%-LESS-STRICT allocation, lane-risk, aggregate-risk, correlated-risk, daily-loss, timing, and closeout limits remain controlling. This amendment does not increase risk or loss ceilings.
- Every fill still requires prompt broker-confirmed full-quantity protection. Reconcile order state until final, never infer a fill, never average down or widen the catastrophe stop, never borrow margin or exceed unleveraged buying power/equity, and close by the controlling no-overnight deadline.
- A Massive `BASE_READY` or `TRIGGER_CROSS` event is never an order by itself. The autonomous controller may submit without another user reply only after every rule above and the standing atomic review-to-submit workflow pass.

## Runtime application

- Treat the deployed Massive configuration flags `score_is_entry_gate=false`, `fresh_news_required=false`, and `state_is_entry_gate=false` as the production candidate policy.
- Preserve the dynamic `max_spread_to_structural_risk=0.15` gate.
- Record the source event, strategy version `sol_ultra_live_autonomous_2026-08-21_v1`, contemporaneous evidence, decision, review result, broker order ID, fill state, protective-order state, and outcome in the durable ledger.
- Continue the existing one-minute heartbeat, broker/account/order/protection audit first, and Massive event priority/freshness rules.
