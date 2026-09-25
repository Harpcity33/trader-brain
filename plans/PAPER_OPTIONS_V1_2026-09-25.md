# Trader Brain Paper Options v1

Date: 2026-09-25
Status: PAPER-ONLY DESIGN

## Objective

Test whether Trader Brain can produce a repeatable positive-expectancy options process before any live autonomous deployment. The system must learn from every session, grade itself daily, and measure results net of realistic execution assumptions.

## Capital and Instruments

- Starting paper capital: $1,000.
- Options only.
- Initial authority: long calls and long puts only.
- No naked short options, spreads, exercise, or assignment handling in v1.
- Regular U.S. market hours only for signal confirmation and paper entries.

## Daily Research Window

Target window: 07:00-08:00 America/New_York on trading days.

Massive is the primary market-data source of truth wherever the subscribed feed provides the required field. Missing or unavailable Massive data must be labeled unavailable; the system must not silently substitute stale or guessed values.

The research pass should evaluate:

1. Broad market regime and index trend.
2. Premarket leaders/laggards and unusual movement.
3. Prior-day and multi-day momentum/mean-reversion context.
4. News/catalyst candidates when available from approved sources.
5. Underlying liquidity and price behavior.
6. Options-chain liquidity, bid/ask spread, volume, open interest, implied volatility, and greeks using Massive snapshots.
7. Candidate expirations and strikes suitable for a $1,000 paper account.
8. A ranked research watchlist for intraday monitoring.

Research does not itself create a BUY signal.

## Intraday Heartbeat

- Heartbeat target: every 2 minutes during the active monitoring window.
- The 2-minute heartbeat observes and updates candidate state only.
- The strategy decision timeframe is 15 minutes.
- A BUY signal may be emitted only after the relevant 15-minute bar/setup confirms.
- No 2-minute-only entries.

## Notification Policy

The user should receive notifications only for actionable 15-minute BUY signals or a true intervention-required fault.

Do not notify for:

- heartbeat success,
- ordinary scans,
- watchlist updates,
- near-signals,
- rejected candidates,
- routine position monitoring,
- routine end-of-day logging.

A BUY notification must contain:

- underlying ticker,
- CALL or PUT,
- exact option contract,
- expiration and strike,
- current bid/ask and intended paper limit entry,
- paper allocation / contract count,
- invalidation or stop condition,
- target / exit plan,
- 15-minute confirmation evidence,
- concise setup rationale,
- timestamp and data freshness.

No qualifying setup means silence.

## Paper Execution

After a valid signal, the paper engine records a simulated order and simulated fill using explicit fill assumptions. The engine then manages the paper position according to the predefined exit rules without repeatedly asking the user what to do.

Every paper action must be logged with source timestamps and assumptions.

## Risk Controls

The implementation must define and enforce, before activation:

- maximum capital allocated per position,
- maximum simultaneous exposure,
- maximum daily loss,
- maximum number of new trades per day,
- stale-data rejection,
- duplicate-order rejection,
- no-entry behavior when required Massive data is unavailable,
- a kill switch / disable-new-entries state.

Risk limits may not be loosened automatically during market hours.

## Massive Data Requirements

Preferred Massive capabilities for v1 include:

- option-chain snapshots for pricing, greeks, IV, open interest, latest quotes/trades, and underlying price,
- custom aggregate bars for 15-minute confirmation,
- option contract snapshots for final candidate validation,
- historical aggregates for setup review and backtesting.

Massive data is the canonical quantitative market source for fields it supplies. Broker data, when later introduced, remains canonical for account state, executable broker review, positions, and fills.

## End-of-Day Grading

At the end of each trading day, grade the system even if no trades occurred.

### Daily scorecard

Record:

- number of researched candidates,
- number of valid 15-minute signals,
- number of paper entries,
- wins,
- losses,
- breakevens,
- gross paper P&L,
- estimated slippage/fees,
- net paper P&L,
- win rate,
- average winner,
- average loser,
- profit factor,
- expectancy per trade,
- maximum intraday drawdown,
- maximum adverse excursion,
- maximum favorable excursion,
- average risk/reward realized,
- rule violations,
- stale/missing-data events,
- missed valid setups,
- false-positive signals.

### Grade dimensions

Assign A-F grades separately for:

1. Signal quality.
2. Entry quality.
3. Risk discipline.
4. Exit quality.
5. Data integrity.
6. Process adherence.

Do not collapse these into a single grade without retaining the individual dimensions.

## Learning Loop

After grading:

1. Identify what worked and what failed.
2. Separate market-regime effects from process errors.
3. Record false positives, missed setups, and exit mistakes.
4. Propose parameter changes only from accumulated evidence, not one isolated trade.
5. Never modify live-session risk limits automatically.
6. Store proposed strategy changes for later review/testing.
7. Carry validated lessons into the next day's 07:00 research pass.

The system should maintain an auditable history of predictions, paper trades, grades, and subsequent rule changes so improvements can be measured rather than assumed.

## Promotion Gate Toward Live Autonomy

Paper v1 does not authorize live trading.

Promotion should require a sufficient sample with positive net expectancy, acceptable drawdown, zero material rule violations, reliable data freshness, and stable execution behavior. The exact statistical and risk thresholds must be defined before promotion; they must not be invented after seeing favorable results.

## Scheduler Constraint

The 07:00 research pass and 2-minute heartbeat require an external scheduler/runtime capable of recurring execution. This repository specification does not itself schedule or execute jobs. The chosen runtime must persist state across heartbeats and trading days.
