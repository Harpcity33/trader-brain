# Trader Brain — Codex Handoff
## Session: August 20, 2026
## Mode: PAPER TRADING ONLY
## Account Model: $500 Starting Settled Cash / Cash Account Rules

---

# 1. Objective

Build and maintain a daily paper-trading system called **Trader Brain** that:

- Scans premarket and intraday movers.
- Identifies the highest-quality actionable momentum setups.
- Uses catalyst quality, liquidity, relative volume, price structure, sector breadth, extension, float/dilution risk, and historical follow-through to rank candidates.
- Records paper trades in real time.
- Never retroactively creates entries after seeing later price action.
- Deploys the full $500 starting settled paper cash each trading day.
- Does not recycle same-day sale proceeds as buying power because the model assumes a cash account.
- Closes all intraday paper positions by 4:00 PM ET unless a position was explicitly designated as a swing **before entry**.
- Maintains a cumulative ledger, scorecard, lessons learned, and scanner-performance statistics.

This system must distinguish between:

- PLANNED
- WATCH
- ENTERED
- OPEN
- CLOSED
- CANCELLED
- LOWER-CONVICTION / CAPITAL-DEPLOYMENT
- SWING
- NO TRADE

Never imply that any simulated trade occurred in a real Robinhood brokerage account.

---

# 2. Non-Negotiable Trading Rules

## Daily Capital

Starting settled paper cash each session:

`$500.00`

Required cumulative new entry notional each trading day:

`>= $500.00`

Because this models a cash account:

- Same-day sale proceeds become unsettled.
- Same-day proceeds may NOT be reused for new entries.
- Gross new entry notional is calculated only from the original settled buying power available that morning.
- If cumulative entry notional is less than $500, mark:

`DEPLOYMENT FAILURE`

even if the day's P&L is positive.

## Allocation

There is **no per-position allocation cap**.

Allowed examples:

- $500 in one high-conviction paper position.
- $300 in one position + $200 in another.
- Multiple smaller positions if diversification improves expected risk/reward.

Full deployment does NOT mean full account risk.

## Intended Daily Risk Cap

Target total stop-defined risk:

`<= $35`

Whenever technically possible.

Example:

If $500 is deployed into a stock at $10 with 50 shares and stop at $9.50:

Risk = `50 × $0.50 = $25`

That is acceptable.

If an options trade is used:

Maximum loss must be defined before entry and should be included in combined daily risk.

---

# 3. Required Daily Schedule

Trader Brain should operate on this cadence:

## 7:00 AM ET — Early Market Intelligence

Produce:

- Macro context.
- Futures.
- Premarket leaders.
- Daily Price Jumps candidates.
- Major earnings/news.
- Sector strength.
- Initial top candidates.
- Under-$5 candidate.
- 90-trading-day jump analog comparison.
- Preliminary catalyst and liquidity ranking.

No entries should be invented.

## 9:15 AM ET — Final Premarket Plan

Re-rank candidates using:

- Current price.
- Premarket % move.
- Premarket volume.
- Relative volume.
- Catalyst strength.
- Liquidity.
- Float.
- Dilution risk.
- Sector confirmation.
- Historical gap/fade tendency.
- Remaining Move Capacity.
- Extension.

For each candidate provide:

- Direction.
- Entry trigger.
- Stop.
- T1.
- T2.
- Skip condition.
- Conviction grade.
- Day-Trade Score.
- Intended allocation.
- Expected stop-defined risk.

## 10:00 AM through 4:00 PM ET — Hourly Paper Trading Desk

At minimum run at:

- 10:00
- 11:00
- 12:00
- 1:00
- 2:00
- 3:00
- 4:00

Each run must persist its state.

For every candidate record one of:

- ENTER
- WAIT
- CANCEL
- HOLD
- PARTIAL EXIT
- EXIT
- SWING APPROVED

Never rely on the end-of-day review to reconstruct these decisions.

---

# 4. Persistent State Requirement — Highest Priority Fix

The biggest failure of the current Trader Brain experiment is not mover discovery.

It is **state preservation**.

The EOD reviewer currently cannot reliably access the actual 7:00, 9:15, and hourly desk decisions.

Codex should implement a durable daily state store.

Suggested structure:

```text
trader_brain/
  data/
    2026-08-20/
      0700_scan.json
      0915_plan.json
      1000_desk.json
      1100_desk.json
      1200_desk.json
      1300_desk.json
      1400_desk.json
      1500_desk.json
      1600_close.json
      ledger.json
      mover_board.json
      lessons.json
      eod_report.md
```

Every scan and every decision should be timestamped and append-only.

Do NOT overwrite historical decisions.

Example decision record:

```json
{
  "timestamp_et": "2026-08-20T10:04:23-04:00",
  "ticker": "DE",
  "status": "ENTERED",
  "direction": "LONG",
  "setup": "Opening-range reclaim",
  "entry_price": 602.40,
  "quantity": 0.41466,
  "entry_notional": 250.00,
  "stop": 594.50,
  "t1": 610.00,
  "t2": 618.00,
  "conviction": "A-",
  "day_trade_score": 88,
  "reason": "Verified earnings catalyst + institutional liquidity + relative strength",
  "source_snapshot_ids": []
}
```

If fractional shares are not desired for the simulation, enforce integer-share rules explicitly.

Do not silently switch between integer and fractional-share modeling.

---

# 5. Official Daily Ledger Schema

Each paper trade should contain:

```json
{
  "ticker": "",
  "structure": "",
  "conviction_grade": "",
  "day_trade_score": null,
  "entry_timestamp_et": "",
  "entry_price": 0.0,
  "quantity_or_contracts": 0,
  "entry_notional": 0.0,
  "account_allocation_pct": 0.0,
  "stop": 0.0,
  "t1": 0.0,
  "t2": 0.0,
  "partial_exits": [],
  "final_exit_timestamp_et": "",
  "final_exit_price": 0.0,
  "realized_pnl_dollars": 0.0,
  "realized_pnl_pct": 0.0,
  "unrealized_pnl_dollars": 0.0,
  "status": "CLOSED",
  "is_swing": false
}
```

Daily account summary must include:

- Starting settled cash.
- Total gross new entry notional.
- Ending settled cash.
- Unsettled proceeds.
- Ending equity.
- Realized P&L.
- Unrealized P&L.
- Daily return vs starting $500.
- Deployment compliance.
- Total stop-defined risk.

Use exact arithmetic.

Flag any discrepancy.

Never silently repair historical numbers.

---

# 6. Scanner Ranking Model

Raw percentage gain should NOT be the primary rank.

Recommended model:

`Opportunity Score = Catalyst Quality × Liquidity × Remaining Move Capacity × Sector Confirmation`

Then apply penalties for:

- Extreme extension.
- Low float.
- Dilution risk.
- Wide spreads.
- Weak/no catalyst.
- Repeated gap-fade behavior.
- Thin liquidity.
- Parabolic exhaustion.

Suggested normalized factors:

```text
Catalyst Quality          0–100
Liquidity                 0–100
Relative Volume           0–100
Sector Breadth            0–100
Remaining Move Capacity   0–100
Technical Structure       0–100
90-Day Follow-Through     0–100

Extension Risk            0–100 penalty
Gap-Fade Risk             0–100 penalty
Float/Dilution Risk       0–100 penalty
Spread Risk               0–100 penalty
```

Possible formula:

```text
base =
  0.25 * catalyst_quality +
  0.15 * liquidity +
  0.10 * relative_volume +
  0.10 * sector_breadth +
  0.20 * remaining_move_capacity +
  0.10 * technical_structure +
  0.10 * historical_follow_through

penalty =
  0.30 * extension_risk +
  0.25 * gap_fade_risk +
  0.25 * float_dilution_risk +
  0.20 * spread_risk

final_score = clamp(base - 0.45 * penalty, 0, 100)
```

This is only an initial proposed weighting and should be optimized from accumulated paper results.

---

# 7. Important Scanner State Change

Extended movers need TWO separate outcomes:

## REJECT ENTRY

Means:

Do not buy right now.

## KEEP ON MOMENTUM WATCH

Means:

The stock remains eligible if it later gives:

- Pullback.
- VWAP reclaim.
- Opening-range reclaim.
- Higher-low setup.
- Fresh volume expansion.
- Breakout from consolidation.
- Renewed acceleration.

Do NOT remove a stock simply because it is already +50%, +80%, or +100%.

Transformative catalysts can create a new price regime.

---

# 8. Transformative Catalyst Override

Historical extension should receive less weight when the catalyst is unusually material.

Examples:

- Phase 3 success.
- FDA decision.
- Major government contract.
- Acquisition.
- Transformational partnership.
- Unexpected earnings/guidance revision.
- Bankruptcy resolution.
- Major legal/regulatory ruling.

When these occur, the system should permit a stock to exceed its normal 90-day move envelope without automatically labeling the setup exhausted.

Still require confirmation before entry.

---

# 9. 90-Trading-Day Jump Model

For each serious mover compare today's move to the last ~90 trading sessions.

Track:

- Largest prior up days.
- Largest prior down days.
- Follow-through day 1.
- Follow-through day 2.
- Follow-through day 3.
- Gap-fade frequency.
- Average close vs intraday high.
- Average next-day return.
- Average five-day return.
- Whether large gaps historically held VWAP.
- Whether the ticker repeatedly diluted after spikes.

Historical analogs should guide risk.

They should NOT automatically override a genuinely transformative catalyst.

---

# 10. Daily Price Jumps / Big-Mover Audit

Every EOD review must evaluate whether the scanner added signal.

For every serious mover classify:

- SURFACED EARLY + ACTIONABLE
- SURFACED EARLY + CORRECTLY REJECTED
- SURFACED EARLY + SHOULD HAVE REMAINED ON WATCH
- SURFACED TOO LATE / CHASE
- MISSED HIGH-QUALITY MOVER
- AFTER-THE-FACT ONLY — NO CREDIT

Track over time:

- Total movers surfaced.
- High-quality movers surfaced.
- Movers traded.
- Mover-trade wins.
- Mover-trade losses.
- Average mover-trade return.
- Missed high-quality movers.
- Chase rate.
- False-positive rate.

---

# 11. Best Under-$5 Screen

Treat under-$5 names separately.

Required checks:

- Fresh catalyst.
- Premarket/current volume.
- Relative volume.
- Spread.
- Float.
- Dilution risk.
- Liquidity.
- Listing/compliance risk.
- 90-day behavior.
- Execution quality.

A stock can fail the entry screen but remain on the watch screen.

Do NOT force an under-$5 trade purely to satisfy the category.

If no high-quality under-$5 stock exists, defined-risk low-cost options may be evaluated.

---

# 12. Social-Media Inputs

Social investor content is supplemental only.

It should never override:

- Verified news.
- Primary filings.
- Earnings releases.
- Clinical releases.
- SEC filings.
- Real-time price/volume data.

Each day grade social inputs:

- Useful Signal
- Noise
- No Value

No trade credit should be assigned merely because a creator mentioned a ticker.

---

# 13. August 20, 2026 — Market Findings

Today's broad market was weak.

Approximate major-index result:

- S&P 500: -0.85%
- Nasdaq: -1.0%
- Dow: -1.27%

Relative-strength pockets included:

- Energy.
- Crypto-related equities.
- Select earnings-driven names.

Major notable candidates from today's analysis:

## DE — Deere

Classification:

`HIGH-QUALITY INSTITUTIONAL LONG`

Catalyst:

- Earnings beat.
- EPS approximately $5.10.
- Revenue beat.
- Raised lower end of full-year net-income guidance.

Observed behavior:

- Approximately +4% to +5% premarket.
- Traded nearly +9% intraday at points.
- Finished around $613.86.
- Prior close approximately $580.63.

Why it mattered:

- Fresh verified catalyst.
- Institutional liquidity.
- No low-float concerns.
- Strong relative strength in a weak tape.
- Significant additional move after the initial gap.

Trader Brain lesson:

A stock like DE should often outrank a more extreme microcap mover because its **catalyst quality + liquidity + remaining move capacity** can be superior.

---

## MSTR — Strategy

Classification:

`CRYPTO MOMENTUM / THEME TRADE`

Observed premarket:

- Roughly $114–$115.
- Approximately +10%.

Catalyst/theme:

- Bitcoin moved above $70,000.
- Broad crypto-equity confirmation.

Related strength included:

- COIN.
- CRCL.
- BLSH.
- BMNR.
- CAN.

Risk:

MSTR was already historically extended.

Recent history included strong single-day gains around:

- +5.95%
- +7.61%

A nearly +10% premarket gap exceeded normal recent behavior.

Correct response:

`DO NOT CHASE`

But:

`KEEP ON MOMENTUM WATCH`

Wait for:

- Pullback.
- VWAP reclaim.
- Fresh breakout.
- Renewed acceleration.

---

## COIN — Coinbase

Classification:

`CRYPTO MOMENTUM / HIGH-LIQUIDITY`

Observed:

- Roughly +6% to +8% premarket.
- Continued strength later.

Positive attributes:

- Strong sector breadth.
- Strong theme confirmation.
- Institutional liquidity.

Preferred execution:

- Pullback/reclaim.
- Opening-range break.
- Momentum continuation with BTC confirmation.

---

## AAP — Advance Auto Parts

Classification:

`DOWNSIDE MOMENTUM`

Observed:

- Approximately -16% to -21% premarket.
- Later fell roughly -26.7%.

Catalyst:

- Revenue miss.
- Weak guidance.

Potential structure:

- Defined-risk bearish option.
- No naked shorting assumption.

Why it mattered:

A clear known premarket negative catalyst continued materially lower after the open.

The scanner needs a downside board, not only long-side movers.

---

## WMT — Walmart

Classification:

`DOWNSIDE CONTINUATION`

Observed:

- Approximately -5.8% premarket.
- Finished roughly -9.6%.

Catalyst:

- Disappointing comparable-sales growth.
- Weak near-term guidance.

Potential structure:

- Defined-risk put/debit spread if liquidity, spread, strike selection, and premium fit the $500 account.

---

## MRNA — Moderna

Classification:

`DAY-TWO FADE / EXTREME VOLATILITY`

Context:

Previous session:

- Extremely large upside move.

August 20:

- Approximately -8% premarket.
- Later down more than 20%.

Correct approach:

Do not assume a fade merely because prior-day move was extreme.

Treat as advanced setup requiring confirmation.

---

## CAN — Canaan

Classification:

`UNDER-$5 / CRYPTO SYMPATHY`

Observed:

- Approximately +10%.
- Price around $0.25–$0.27.

Positive:

- Crypto sector breadth.

Negative:

- Largely sympathy-driven.
- Penny-stock execution risk.
- Large historical volatility.
- Listing/compliance concerns.
- Weak fit for institutional-quality screen.

Correct classification:

`WATCH`

Not automatically:

`APPROVED TRADE`

---

# 14. August 20 Official Paper Ledger

Because the actual timestamped desk execution outputs were not available to the EOD reviewer, **no paper entries may be reconstructed retrospectively**.

Official ledger:

```text
Starting settled paper cash: $500.00
Verified simulated entries: 0
Gross new entry notional: $0.00
Open positions: 0
Closed positions: 0
Realized P&L: $0.00
Unrealized P&L: $0.00
Unsettled proceeds: $0.00
Ending settled cash: $500.00
Ending equity: $500.00
Daily return: 0.00%
Deployment requirement: FAILED
```

Reason:

No preserved timestamped entry record.

Do NOT generate hypothetical official trades from DE, MSTR, COIN, AAP, WMT, MRNA, CAN, or any other ticker.

Illustrative backtests may be calculated separately but must never appear in the official paper ledger.

---

# 15. Experiment Scoreboard Through August 20

Current auditable formal experiment days:

- August 18
- August 19
- August 20

Current official scoreboard:

```text
Initial experiment balance: $500.00
Current hypothetical compounded balance: $500.00
Cumulative P&L: $0.00
Cumulative return: 0.00%
Closed paper trades: 0
Wins: 0
Losses: 0
Deployment-compliant days: 0
Total audited days: 3
Deployment compliance: 0%
```

Not yet meaningful:

- Win rate.
- Average winner.
- Average loser.
- Profit factor.
- Maximum drawdown.

Do not fabricate these metrics.

---

# 16. Previous Important Lessons

## August 18

Notable:

XOS was surfaced around 9:05 AM near:

- $4.61
- +120.6%
- ~24.9M shares

Lesson:

Rejecting an immediate chase was reasonable.

Removing XOS from active watch was not.

Correct future state:

`REJECT ENTRY + KEEP ON MOMENTUM WATCH`

AMLX was a major missed high-quality mover.

It had:

- Strong Phase 3 catalyst.
- Institutional-quality liquidity.
- Large move.
- Significant additional upside after premarket.

Lesson:

Rank catalyst quality and liquidity more heavily than raw % gain.

---

## August 19

MRNA was surfaced as an extraordinary mover.

Approximate observed premarket state:

- ~$118.50
- +88%
- ~9.6M shares

It later traded as high as approximately:

- $163.47

Lesson:

A stock already +80% or more can still have meaningful remaining move capacity if the catalyst is transformative and liquidity is enormous.

Also:

Sector breadth from related biotech/pharma names strengthened the signal.

Lesson:

Add:

`Transformative Catalyst Override`

---

# 17. Daily Grading Framework

Each session grade A–F across:

- Analysis Quality
- Mover Discovery
- Execution Discipline
- Allocation / Concentration
- Deployment Compliance
- Risk Control
- Risk-Adjusted Result
- Hindsight Avoidance
- Under-$5 Screen
- Social Signal Quality

Do NOT grade only by P&L.

Example:

A flat day with no deployment should not receive an A merely because the system avoided losses.

---

# 18. EOD Required Output

Every market close report should end with:

## Official Daily Result

- Daily P&L.
- Daily return.
- Ending equity.

## Deployment Compliance

- PASS or FAIL.
- Gross new entry notional / $500.

## Scoreboard

- Compounded balance.
- Cumulative P&L.
- Cumulative return.
- Closed trades.
- Wins.
- Losses.
- Win rate.
- Average win.
- Average loss.
- Largest win.
- Largest loss.
- Profit factor.
- Maximum drawdown.
- Deployment compliance percentage.

Only calculate metrics when enough history exists.

## Scanner Stats

- Movers surfaced.
- Movers traded.
- Mover wins/losses.
- Average mover return.
- Missed high-quality movers.
- Chase rate.
- False positives.

## Grades

- Daily Strategy Grade.
- Daily Price Jumps Scanner Grade.

## Lessons

- What worked.
- What cost opportunity or money.
- Highest-priority improvement.

## Prompt Changes

Explicit modifications for the next session.

---

# 19. Codex Implementation Priorities

## Priority 1 — Persistent Intraday Ledger

Fix immediately.

Every run writes:

- Timestamp.
- Candidate.
- Score.
- Decision.
- Proposed trigger.
- Actual trigger status.
- Allocation.
- Entry.
- Stop.
- Targets.
- Exit.
- P&L.
- Cash state.

The EOD process must read these files rather than reconstructing history.

## Priority 2 — Market Data Integration

Use Massive wherever possible for:

- Real-time trades/quotes.
- Intraday aggregates.
- Historical aggregates.
- Volume.
- Previous close.
- High/low.
- Options chains where needed.
- Historical 90-day analysis.

Prefer exact timestamps.

Store raw snapshots used to make each decision.

## Priority 3 — Mover Scanner

Scan:

- Premarket % gainers.
- Premarket % losers.
- Relative-volume leaders.
- Unusual dollar-volume names.
- Gap-ups.
- Gap-downs.
- Opening-range breakouts.
- VWAP reclaimers.
- New intraday highs with volume.
- New intraday lows with volume.

## Priority 4 — Catalyst Verification

Rank source quality:

1. Company press release / IR.
2. SEC filing.
3. FDA / government source.
4. Major financial news.
5. Secondary financial media.
6. Social media.

No strong catalyst grade should rely only on social media.

## Priority 5 — Cash-Account Engine

Track separately:

```text
settled_cash
unsettled_proceeds
gross_new_entry_notional
open_market_value
realized_pnl
unrealized_pnl
equity
```

Do not recycle intraday sale proceeds.

## Priority 6 — Automatic Deployment Check

At each desk update calculate:

```text
remaining_required_notional =
  max(0, 500 - gross_new_entry_notional)
```

By 2:00 PM ET, if the account remains under-deployed:

Start evaluating lower-conviction but still rule-compliant capital-deployment trades.

Do NOT force a reckless trade.

Label any such position:

`LOWER-CONVICTION / CAPITAL-DEPLOYMENT`

The strategy should still attempt to meet the required $500 gross entry notional.

## Priority 7 — Separate Long and Downside Boards

Today's AAP and WMT behavior shows the system should not only rank long gainers.

Maintain:

- LONG MOMENTUM BOARD
- SHORT/DOWNSIDE OPTIONS BOARD
- UNDER-$5 BOARD
- SWING BOARD

## Priority 8 — Remaining Move Capacity

Develop a feature that estimates whether meaningful move remains.

Inputs could include:

- Current gap vs historical gap distribution.
- Catalyst category.
- Premarket volume.
- Dollar volume.
- Float.
- Sector confirmation.
- Distance from VWAP.
- Distance from opening range.
- First pullback depth.
- New high volume.
- Historical post-gap follow-through.

---

# 20. Safety / Integrity Requirements

The system must NEVER:

- Claim a real Robinhood trade occurred.
- Create a paper entry after seeing later prices.
- Change a historical paper fill to improve results.
- Assume an entry triggered without a timestamped record.
- Recycle unsettled paper proceeds.
- Count a mover as "called" if it was only discovered at EOD.
- Hide deployment failures.
- Hide losing trades.
- Replace actual historical ledger data with backtest estimates.

If data is missing:

Flag it.

Do not invent it.

---

# 21. Recommended Next-Code Deliverables

Codex should produce the following modules:

```text
trader_brain/
  config.py
  data_provider.py
  massive_client.py
  catalyst_engine.py
  mover_scanner.py
  historical_analog.py
  scoring.py
  risk_engine.py
  cash_account.py
  paper_broker.py
  ledger.py
  desk.py
  eod_review.py
  scoreboard.py
  models.py
  storage.py
  main.py
```

Suggested commands:

```bash
python main.py scan --time 07:00
python main.py plan --time 09:15
python main.py desk --time 10:00
python main.py desk --time 11:00
python main.py desk --time 12:00
python main.py desk --time 13:00
python main.py desk --time 14:00
python main.py desk --time 15:00
python main.py close --time 16:00
python main.py review --date 2026-08-20
```

---

# 22. Definition of Success for the Next Session

The next Trader Brain session is successful operationally if:

1. Every scheduled report persists.
2. Every candidate decision persists.
3. Every simulated fill has a timestamp.
4. Every exit has a timestamp.
5. Gross new entry notional is calculated automatically.
6. Same-day proceeds are not recycled.
7. Stop-defined risk is calculated before entry.
8. EOD review can fully reconstruct the session using only saved state.
9. Scanner results are graded against actual outcomes.
10. No hindsight entries are created.

Performance comes after auditability.

The immediate goal is to convert Trader Brain from a market-research workflow into a **stateful, measurable paper-trading system**.
