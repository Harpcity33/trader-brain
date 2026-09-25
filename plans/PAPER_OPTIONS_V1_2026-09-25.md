# Trader Brain Paper Options v1

Date: 2026-09-25
Status: PAPER-ONLY DESIGN / READY FOR RUNTIME IMPLEMENTATION

## Objective
Test whether Trader Brain can produce repeatable positive expectancy before any live autonomous deployment. Hunt aggressively for opportunity, never force a trade, grade every session, and learn from misses as well as entries.

## Capital and Instruments
- Starting paper capital: $1,000.
- Options only: long calls and long puts.
- Regular U.S. market hours for entries.
- No naked short options, spreads, exercise, or assignment handling in v1.

## Risk Budget
- Weekly drawdown stop: 10% of Monday starting equity. Week-one maximum loss: $100.
- Weekly limit does not expand with intrawweek profits. Once hit, no new entries until the next trading week and a review is required.
- Normal planned loss per trade: 2-3% of current account equity.
- Exceptional setup ceiling: 4%, only when all strategy and liquidity gates pass.
- Aggregate open risk must remain inside the remaining weekly loss budget.
- Risk limits may never be loosened automatically during market hours.

## Daily Research — 07:00-08:00 America/New_York
Massive is the primary quantitative market-data source wherever the subscribed feed supplies the field. Missing Massive data is labeled unavailable, never guessed or silently replaced.

Research evaluates market regime, index trend, premarket movers, catalysts, prior/multi-day momentum, liquidity, option-chain bid/ask, volume, open interest, IV, greeks, expiration/strike suitability, and historical context.

### Pre-market brief
Publish one unified Trader Brain pre-market brief containing:
- Top 3 options candidates for the 15-minute confirmation lane.
- Top 3 fast day-trade candidates for the 5-minute momentum lane.
- For each: direction, thesis, key price level, volume requirement, confirmation window, invalidation, preferred contract profile where applicable, and exact condition that promotes WATCH to BUY.

These are watch candidates, not BUY signals.

## Intraday Heartbeat
- Target cadence: every 2 minutes during the active market monitoring window.
- Heartbeat is observation/state update only; no 2-minute-only entries.
- Search broadly and aggressively. No valid setup is an acceptable outcome; forced trades are prohibited.

### Lane A — 15-minute options confirmation
A BUY may be emitted only after the relevant completed 15-minute setup confirms and final validation passes.

### Lane B — 5-minute fast momentum/day-trade
The heartbeat may detect developing spikes, but a BUY may be emitted only after a completed 5-minute bar confirms. Require abnormal/relative volume evidence, price/volume momentum, adequate option liquidity, acceptable spread, clear underlying breakout/continuation structure, and defined invalidation.

Grade Lane A and Lane B separately.

## Model/Reasoning Escalation
- Routine 2-minute heartbeat: GPT-5.6 Sol Instant / lowest appropriate reasoning effort.
- 07:00 research: High reasoning.
- Trigger candidate: freeze the triggering market-data snapshot and escalate that candidate to High reasoning for final BUY/NO-BUY validation.
- End-of-day grading and learning: High reasoning.
- After the decision, routine heartbeat returns to Instant.
- If escalation fails, times out, or required data becomes stale: NO TRADE.
- The final reviewer must evaluate the same triggering snapshot plus clearly timestamped newer data; it may not silently judge a materially different market state.

## Notification Policy
Notify the user only for:
1. the unified pre-market brief,
2. actionable 5-minute or 15-minute BUY signals,
3. true intervention-required faults,
4. the unified end-of-market report.

Do not notify for heartbeat success, ordinary scans, near-signals, rejected candidates, routine monitoring, or watchlist churn.

A BUY alert must include underlying, CALL/PUT, exact contract, expiration/strike, bid/ask, intended paper limit entry, allocation/contract count, invalidation/stop, target/exit plan, confirmation evidence, concise rationale, lane (5m or 15m), timestamp, and data freshness.

## Paper Execution
After a valid BUY signal, record a simulated order/fill using explicit assumptions. Manage the paper position according to predefined exits without repeatedly asking the user. Log every action, source timestamp, assumption, and outcome.

## Data and Safety Controls
Enforce stale-data rejection, duplicate-order rejection, no-entry when required Massive data is unavailable, persistent state, maximum simultaneous risk, and a disable-new-entries kill state.

Massive is canonical for quantitative market fields it supplies. A future broker connection is canonical for account state, broker eligibility, positions, executable reviews, orders, and fills.

## End-of-Market Report and Grading
Publish one unified Trader Brain end-of-market report. Grade the system even if no trades occurred.

Track researched candidates; 5m and 15m valid signals separately; paper entries; wins/losses/breakevens; gross and net paper P&L; estimated slippage/fees; win rate; average winner/loser; profit factor; expectancy; intraday and weekly drawdown; MAE/MFE; realized risk/reward; rule violations; stale/missing-data events; false positives; missed valid setups; remaining weekly risk budget; and performance of all six pre-market watch candidates.

Assign separate A-F grades for signal quality, entry quality, risk discipline, exit quality, data integrity, and process adherence. Also grade whether inactivity was correct or whether a qualifying opportunity was missed.

## Learning Loop
After grading: identify what worked/failed; separate regime effects from process errors; record false positives, missed setups, and exit mistakes; compare the 5m and 15m lanes; propose parameter changes only from accumulated evidence; never change session risk automatically; store proposed changes for later testing; and carry validated lessons into the next 07:00 research pass.

Maintain an auditable history of predictions, paper trades, grades, missed opportunities, and rule changes.

## Promotion Gate Toward Live Autonomy
Paper v1 authorizes no live trading. Promotion requires a predefined sufficient sample, positive net expectancy, acceptable drawdown, zero material rule violations, reliable data freshness, and stable execution behavior. Thresholds must be defined before promotion rather than after favorable results are observed.

## Runtime / Scheduler Requirement
The repository specification does not itself wake up or send recurring notifications. A persistent runtime/scheduler must implement the 07:00 research job, 2-minute heartbeat, reasoning escalation, state persistence, BUY notifications, and end-of-market report. Keep this paper runtime isolated from the stopped legacy Codex heartbeat and avoid duplicate pre-market/end-of-day notifications.