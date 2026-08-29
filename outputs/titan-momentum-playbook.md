# Titan Momentum Playbook

Version 1.2 — August 19, 2026

## Mission

Capture an unusually large share of legitimate intraday momentum while keeping every trade survivable. A **big mover** is an early-caught instrument transitioning through BUILDING, ACCELERATING, and BREAKOUT with exceptional catalyst, participation, price acceptance, and liquidity; it is not defined by any fixed percentage gain. The decision to remain in it is based on whether the move is still being sponsored by real demand, whether price is accepting higher levels, and whether the position can remain protected at a rational structural level.

There is no knowable perfect top and no undefeated trader. The operational target is repeatable positive expectancy: small predefined losses, selective aggression, unusually large winners when the tape permits them, and no single trade capable of disabling the account.

## What the research changes

1. **Strength earns holding time.** Selling a winner merely because it reached a fixed percentage gain is the disposition effect in automated form. Brokerage-account research documents the tendency to sell winners too early and hold losers too long; subsequent performance did not justify it. [Odean, *Are Investors Reluctant to Realize Their Losses?*](https://faculty.haas.berkeley.edu/odean/papers/disposition/disposition.html)

2. **Attention is not confirmation.** Extreme returns, abnormal volume, and news attract retail buying, but the highest-attention stocks can subsequently underperform. A big leaderboard gain therefore needs catalyst, structure, liquidity, and order-flow confirmation; raw percentage change is never sufficient. [Barber, Lin, and Odean, *Resolving a Paradox*](https://faculty.haas.berkeley.edu/odean/Papers%20current%20versions/resolving_a_paradox_retail_trades_positively_predict_returns_but_are_not_profitable.pdf)

3. **Execution quality is part of the edge.** Research attributes much individual-investor underperformance to aggressive orders. Limit entries, spread/depth checks, and skipped vertical candles are not hesitation; they prevent the signal from being consumed by slippage. [Barber et al., *Just How Much Do Individual Investors Lose by Trading?*](https://escholarship.org/uc/item/0m37p6sd)

4. **Candles are evidence, not prophecy.** Price patterns can contain information, but a named candle by itself is not a trading system. The engine uses candle anatomy only in combination with volume, location, liquidity, catalyst, and the broader tape. [Caginalp and Laurent, *The Predictive Power of Price Patterns*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=932984)

5. **A small minority can demonstrate persistent skill, but the base rate is severe.** The day-trader evidence finds the vast majority lose; previously successful traders are the group with evidence of profits after costs. That makes an auditable ledger, setup-level statistics, and resistance to overfitting mandatory. [Barber et al., *The Cross-Section of Speculator Skill*](https://faculty.haas.berkeley.edu/odean/papers/Day%20Traders/Day%20Trading%20Skill%20110523.pdf)

6. **Stops reduce catastrophic downside but can truncate trends.** Research on trailing stops generally finds downside-risk reduction, not a universal return boost. The solution for this strategy is a broker-held catastrophe stop plus a volatility-aware structural trail, rather than an arbitrary tight percentage. [Dai et al., *Risk Reduction Using Trailing Stop-Loss Rules*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3338243)

7. **Broker mechanics dominate theory.** Robinhood option stops can be entered only from 9:45 a.m. to 4:00 p.m. ET, may fill far from the trigger in volatility, and can be activated by a brief price shock. Equity and option orders do not execute during a trading halt and may face a radically different reopening price. [Robinhood option-stop documentation](https://robinhood.com/us/en/support/articles/stop-market-order-options/) and [Robinhood halt documentation](https://robinhood.com/us/en/support/articles/trading-halts-on-individual-stocks/)

## Two-score decision model

The existing **Acceleration Score** decides whether a symbol deserves entry. Once filled, a separate **Position Continuation Score (PCS)** decides whether to hold, add, tighten, trim, exit, or re-enter.

### Position Continuation Score — 0 to 100

| Component | Points | Healthy evidence |
|---|---:|---|
| Price structure | 25 | Higher highs and lows; closes near bar highs; pullbacks hold prior breakout or a rising short-term structure |
| Participation | 20 | Expanding volume on pushes; contracting volume on pullbacks; new highs produce price progress |
| Level control | 15 | Above VWAP, opening-range high, premarket high, or a reclaimed HOD level; retests hold |
| Liquidity and tape | 15 | Stable/tight spread; adequate displayed depth; offers lift without immediate rejection |
| Catalyst integrity | 10 | Fresh, independently verified catalyst remains valid; no adverse filing or clarification |
| Market and sector confirmation | 5 | Relevant peers/index support the direction rather than sharply diverging |
| Remaining capacity | 10 | Not excessively extended from the last base; orderly rather than climactic movement |

Apply explicit penalties for widening spreads, evaporating depth, failed price progress on record volume, repeated upper wicks, halt risk, dilution evidence, or divergence from the sector. Never invent a component when its data is unavailable.

### PCS actions

- **90–100 — dominant:** Hold 75–100% of the position. Adds may occur only from a controlled base or retest and within all risk caps. Do not take a full profit merely because the gain is large.
- **80–89 — healthy:** Hold the majority; take no more than 0–25% unless the position is already parabolic. Trail below validated structure.
- **70–79 — vulnerable:** No adds. Realize 25–50% and tighten the remainder only after confirming that the warning is more than one noisy print.
- **Below 70 — broken:** Exit the remainder when structure failure is confirmed, or immediately for a hard invalidation.

The score is recalculated from current data on every run. An early-caught big mover with PCS 94 is treated as a live leader regardless of its current percentage gain; any mover with PCS 62 is treated as broken.

## Candle and tape interpretation

No single candle authorizes a trade. The following observations update the PCS:

### Continuation evidence

- A 1- or 5-minute bar closes in its upper quartile with a real body exceeding half its range.
- Breakout volume expands relative to comparable recent bars, while pullback volume contracts.
- A shallow pullback holds above VWAP, the opening range, premarket high, prior HOD, or the latest confirmed higher low.
- A new high is accepted: the next bar holds above or successfully retests the breakout instead of immediately closing back inside the old range.
- Spread and displayed depth remain stable as price advances.

### Exhaustion evidence

- A new high forms on climactic volume but produces little additional price progress.
- A wide-range bar leaves an upper wick of roughly 40% or more and closes below its midpoint.
- Two failed HOD attempts create a lower high, especially with increasing sell volume.
- Price loses the latest 1-minute higher low and then the corresponding 5-minute structure or VWAP without a fast reclaim.
- Spread roughly doubles, bids disappear, or a halt resumes with immediate rejection.
- The catalyst is contradicted, a dilution filing appears, or the sector reverses sharply.

A full discretionary exit normally requires one hard invalidation or at least two independent exhaustion signals. A single wick in an otherwise dominant tape calls for evaluation, not panic.

## Exit architecture for exceptional movers

1. **Initial protection:** Immediately after a confirmed fill, place broker-held protection for the full remaining quantity at the thesis invalidation. Size the position from that distance.
2. **At +1R:** Do not reflexively move to exact break-even if that sits inside normal noise. Raise protection only to a valid higher low, breakout retest, or volatility-adjusted structure.
3. **At +2R:** Realize 0–25% when PCS remains at least 90. Protect a profit floor while retaining a meaningful runner.
4. **During sustained acceleration:** Trail the core beneath the latest confirmed 5-minute higher low, or beneath a confirmed 1-minute higher low when the move has become parabolic. Use a volatility buffer and the adaptive-stop exception below rather than mechanically tightening into normal noise.
5. **Parabolic phase:** Stop adding. Take 25–50% into strength when exhaustion evidence begins, then trail the runner beneath the last validated micro-base rather than using the total percentage gain as the exit trigger.
6. **Broken phase:** Exit on failed breakout acceptance, confirmed loss of structural support, catalyst invalidation, or deteriorating liquidity. Do not wait for a predetermined profit target.

Track the high-water mark and **peak-profit giveback**. A large giveback is tolerated only while the PCS remains dominant and the structural stop is still rational. The intended open-profit giveback bands are approximately 25–35% while orderly acceleration is intact and 15–20% after the move becomes parabolic; these are monitoring bands, not guaranteed execution prices.

### Adaptive-stop exception

There are two different stops:

- The **catastrophe/thesis stop** defines maximum acceptable loss and remains non-negotiable.
- The **profit-management trail** protects an open winner and may adapt as volatility and structure change.

The engine may reset a profit-management trail farther from the current price only when PCS is at least 95, the catalyst and liquidity remain intact, price is still accepting higher levels or building a new base, measured volatility has expanded enough to make the old trail statistically noisy, and no exhaustion cluster is present. Before widening the trail, reduce quantity if necessary so the dollar risk to the replacement stop does not exceed the trade's original planned risk or any lane, thesis, aggregate, or daily cap. The replacement may not cross below the original thesis invalidation, create an unprotected position, or turn a position that has reached +2R into a planned net loss after reasonable costs.

This exception may override the monitoring giveback bands; it may not override hard account-risk limits. Evidence must be recorded before the replacement, the replacement order must be reviewed, and protection must be restored or the position closed if the change fails.

The automation does not claim it can identify the exact peak. It seeks a high-quality exit zone by retaining a runner while PCS remains dominant and exiting into an exhaustion cluster or confirmed structural break.

Robinhood does not support bracket orders. Never create overlapping full-quantity exit orders. Use a native trailing stop only if the MCP review explicitly confirms that order type and quantity handling. Otherwise, ratchet the single structural stop through reviewed cancel-and-replace actions; if replacement is rejected, restore protection or close safely. Stop orders do not operate during extended hours, and execution price is not guaranteed.

## Premarket equity pilot

Premarket entries are allowed from 7:05–9:20 a.m. ET only as a deliberately small pilot lane. The lane exists to capture an exceptional verified leader before the opening crowd, while recognizing that Robinhood stop, stop-limit, and trailing-stop orders do not execute during extended hours.

All of the following must be true:

- Equity only. No premarket options, OTC securities, $1–$5 stocks, recent volatility-halt names, warrants, acquisition vehicles, or securities Robinhood marks noncompliant or ineligible for extended-hours trading.
- Broker authentication, account state, buying power, quotes, depth, scans, order review, and order-status tools have operated normally for at least three consecutive one-minute wakes. Any usage-limit, stale-state, authentication, or connector warning closes the lane.
- Acceleration Score is at least 93, catalyst is a fresh primary-source event worth the full 15 points, and liquidity earns at least 9 of 10. Dilution, promotion, listing, and pump/manipulation risks must each be no more than 2 of 10. Extension risk must be no more than 6 of 10.
- Price and volume acceleration improve across at least three consecutive observations. A base contains at least two completed one-minute holding or higher-support bars with controlled pullback volume and stable liquidity.
- Entry is an exact live cross through a precomputed base high, with renewed volume pace. Price must remain within 1.5 short ATR of the trigger and within 4 short ATR of extended-hours VWAP. A vertical first discovery candle is never bought.
- Spread is no more than 0.25%, displayed depth is at least 20 times the intended order, premarket dollar volume is at least $10 million, and quotes/depth are internally consistent.
- Use whole shares and a buy limit only. Maximum premarket allocation is $250, maximum modeled structural risk is $8, maximum aggregate premarket exposure is $250, and only one premarket position may be open. No premarket add or pyramid.

Before submission, precompute the structural invalidation, the marketable sell-limit sequence that would close the position, and the regular-hours GTC stop intended for 9:30. A queued stop is not counted as active protection before 9:30.

After a fill, monitor every one-minute wake. A structural break, failed expansion, quote/depth deterioration, spread above 0.75%, missed wake longer than 90 seconds, stale market data, authentication loss, or connector failure requires an immediate marketable sell-limit exit and broker-confirmed status. Do not wait for the inactive stop. No new premarket position may be opened after 9:20. By 9:28, close unless PCS is at least 95 and the structure supports carrying through the open. At 9:30, immediately verify that full-quantity broker-held protection is active; otherwise close safely.

Treat this as a shadow-tested pilot. Review its opportunity set and hypothetical/actual outcomes separately after five sessions before increasing size, lowering thresholds, or extending it to low-priced stocks.

## Atomic review-to-submit rule

Standing autonomous authority is already present. Once an exact contemporaneous trigger has fired and a Robinhood review is clean, submit without requesting another user confirmation. If the quote moves beyond the reviewed limit but remains inside the planned 0.5-short-ATR chase ceiling, perform one immediate refresh, review, and submit. Otherwise abandon the attempt. A clean preview is not an order; after submission, verify broker status until it is filled, canceled, rejected, or expired.

## Infrastructure health gate

No new position may be opened unless brokerage access, account state, order-status reads, live quotes, and the intended protection/exit workflow are currently available. When flat, any authentication or service-limit failure suspends entries. When exposed, protection and safe-close attempts outrank discovery, and the user is notified immediately if verified access is lost.

## Re-entry after an early exit

An early exit does not permanently disqualify a leader. It does require a new trade thesis.

### Fast false-stop reclaim

A limit re-entry is allowed when all of the following occur:

- The prior exit appears caused by a wick or liquidity sweep rather than catalyst failure.
- Price closes back above the invalidated level promptly and then holds or retests it.
- PCS recovers to at least 90, spread/depth normalize, and the catalyst remains intact.
- A new stop can be placed at a distinct, current structural level inside the remaining risk budget.

### New-base continuation

A re-entry is allowed after at least two completed 1-minute bars or one completed 5-minute bar form a controlled base, pullback volume contracts, and renewed expansion clears the base/HOD on convincing volume. If price travels more than roughly 1.5 short-term ATRs beyond the planned trigger before a fill, skip the chase and wait for another base.

Each re-entry is a fresh trade with a new entry, stop, R value, and ledger record. Realized losses remain in the daily loss calculation. Re-entry size is no larger than the prior position unless the new setup scores at least 95 and all aggregate-risk rules remain satisfied. After two consecutive failures in the same symbol, pause at least 15 minutes and require a materially new base plus PCS at least 95; there is no revenge click and no automatic buyback.

## Pyramiding

Build a dominant leader in as many as three planned tranches—approximately 30%, 30%, and 40%—only when earlier shares are profitable and the thesis strengthens. Before each add, lift earlier protection when structure justifies it so total stop-defined risk remains inside the lane, thesis, portfolio, and daily budgets. Never add during a vertical candle, immediately into a halt, or because the price fell.

## Thirty-minute learning loop

At each :00 and :30, preserve a decision-time snapshot and review:

- Candidate rank changes and missed valid leaders.
- Entry slippage and spread paid.
- Maximum favorable and adverse excursion for every position.
- Exit efficiency: realized gain divided by maximum favorable excursion.
- Peak-profit giveback and whether it occurred with a dominant or deteriorating PCS.
- False-stop rate and five-/fifteen-minute performance after exit.
- Re-entry quality, not merely whether the price later went higher.
- Setup-level expectancy, profit factor, and drawdown.

Only repeated evidence may change adaptive thresholds. A single spectacular winner cannot justify looser loss limits, and a single false stop cannot justify removing protection.

## Current broker constraint

As of June 4, 2026, Robinhood states that its margin accounts no longer have PDT trade-count restrictions or the $25,000 PDT minimum. It instead monitors intraday margin adequacy in real time. The engine never borrows margin and treats broker-reported buying power, maintenance requirements, and rejected-order warnings as authoritative. [Robinhood day-trading documentation](https://robinhood.com/us/en/support/articles/pattern-day-trading/)

## Non-negotiable truth

Aggression belongs in selectivity, sizing within defined risk, and holding proven strength—not in widening stops, chasing vertical candles, averaging down, or manufacturing trades to hit a daily quota. A flat day is better than an attention-driven loss. The system’s claim to excellence must come from audited expectancy and drawdown, never from mythology.
