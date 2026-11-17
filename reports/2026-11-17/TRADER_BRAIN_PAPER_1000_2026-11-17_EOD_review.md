# Trader Brain — November 17, 2026 EOD Review

**Strategy:** `TRADER_BRAIN_PAPER_1000` · **PAPER ONLY** · **Official accounting: QUARANTINED_SOURCE_OFFLINE**  
**Daily process: F (auditability) · Daily Price Jumps scanner: F (unverifiable) · Data integrity: F**

## Authoritative ledger and checkpoints
The canonical episode is `d0eb2031-8dfb-4e1b-b53d-9bb1bd0b90ac`. Mac.attlocal.net remains **offline, last seen approximately 286 hours ago**. Last independently recovered Nov 5 cash is **$1,000**, not Nov 17 closing equity or buying power. There is no source-bound Nov 17 ledger/event chain, so an empty paper ledger **cannot** be asserted.

| Check | Finding |
|---|---|
| 07:00 research | Completed immutable contemporaneous event manifest unavailable |
| 09:15 plan | Reconstructed after its timestamp; zero **new authorization** but existing positions unknown |
| Intraday events | Canonical scanner and paper execution logs unavailable |
| 16:00 | Liquidation and swing exceptions unverified; no account certificate |
| **Official daily return** | **UNAVAILABLE — not certified** |
| **Official closing equity** | **UNAVAILABLE** |
| Trade rows and P&L | **UNVERIFIED, NOT CONFIRMED EMPTY** |
| Settled cash / unsettled proceeds | **UNAVAILABLE / UNAVAILABLE** |

No retrospective simulated fills were created. No real brokerage execution. Retired $500, older $1,000 episodes, and provisional signal books excluded.

## Premarket scanner and market benchmark
November 17 saved after-cutoff Robinhood screens returned **218 Daily Price Jumps results** and **384 under-$5 results**. Those are **not** genuine 9:15-frozen counts. The reconstructed bar study used completed 04:00–09:14:59 bars and same-clock-window RVOL against the previous five sessions. Macro/semiconductor breadth shifted during the session, making reconstructed morning momentum inadequate as an entry trigger.

| Ticker | Grade | Rebuilt PM move | Matched RVOL | Spread proxy | 90-day analog n | Move consumed | Research classification |
|---|---|---:|---:|---:|---:|---:|---|
| SLS | A | 263.45% | 132.26x | 1.105% | 17 | 74.2% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| MRVL | B+ | 4.34% | 1.48x | 0.112% | 18 | 61.1% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| COHR | B- | 7.36% | 1.87x | 0.069% | 20 | 59.8% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| RCAT | A- | -31.73% | 30.58x | 1.247% | 28 | 85.5% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| DIS | A- | -2.35% | 52.75x | 0.525% | 3 | 74% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| JD | B+ | -6.46% | 12.46x | 0.271% | 3 | 74.5% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| HD | B+ | -3.09% | 17.68x | 0.487% | 1 | 97.6% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| AYTU | A- | 45.63% | N/A | 0.377% | 3 | 98.9% | STRUCTURAL_REJECT |
| GP | B | 80.92% | 17995.51x | 0.391% | 9 | 53.1% | STRUCTURAL_REJECT |

**SLS:** Genuine Phase 3 survival endpoint announced 7 AM, not FDA approval. Reconstructed cutoff $27.15 (+263.45%), post-session third-party reference $20.325 (+172.09% vs Nov 16). That is **-25.14% from the reconstructed cutoff mark** and **-40.22% from the reconstructed $34 PM high**, not a tradable P&L. No early-detection or simulated execution credit.

**RCAT:** Nov 16 after-close earnings missed expectations and full-year outlook was withdrawn; reconstructed -31.73% premarket, third-party session mark $2.33 (-33.99%). Cash-account short/option contract executability unverified. Under-$5 substantive bearish-event watch **rejected** because extreme gap, 1.247% indicative spread and missing verified options book.

**GP:** 30.85M reconstructed PM shares and ~$88.11M turnover, yet 17,995.51× matched RVOL is **unreliable without sufficient volume baseline**; approximately 1.216M estimated float, financing/going-concern and exchange risk. Third-party $2.49 (+76.60%) does not overturn structural rejection. This is the A079 case.

**AYTU:** $2.70 signed all-cash deal from Nov 16; reconstructed $2.6505 mark gives ~1.87% gross spread to consideration before deal/carry/transaction costs. This is not unbounded momentum or a qualified day trade.

**MRVL:** Conditional 4 shares at $153.40 ($613.60 notional), stop $147.40 ($24.00 nominal risk), T1 $159.40 and T2 $165.40. Entry required a completed >$153.30 5-minute close above RTH VWAP and OR15 high, volume and SOXX breadth, with settled buying power. None is verified; **$0 authorized, no simulated fill**. External source prices vary; official daily close not used.

All first-canonical-detection timestamps, decision-time atomic NBBO/depth, true 90-day catalyst-matched gap fade, RTH VWAP, completed opening range, float/corporate-action freshness and executable remaining move are unavailable. Discovery alpha and execution alpha are separately **unmeasurable**.

## Best Under-$5 Opportunity
**Research: RCAT bearish issuer event. Qualified simulated entry: NONE.** GP's large price jump is a structurally rejected external benchmark; AYTU has bounded deal spread. No retroactive trades or missed-profit credits.

## Risk, utilization and cumulative scoreboard
Historical policy: 3% normal planned risk, 4% A+, 2% premarket, 5% total open planned risk, 6% new-entry daily lock, 8% hard daily kill, 12% weekly lock, positive execution reserve. All percentages require *certified current equity*. New morning authorization and newly authorized stop risk were **$0.00**, but realized/new-trade totals and current open risk remain **unverified**. No unsettled funds assumed reusable; no old mandatory deployment.

Original $1,000 seed remains historical. **Current compounded equity, cumulative P&L/return, trades, W/L, win rate, average/largest wins/losses, profit factor, expectancy, maximum drawdown, capital utilization, mover discovery totals and mover trade performance are all unavailable** because canonical Nov 17 records are missing.

Grades: analysis C+; mover discovery F-unverifiable; execution N/G; allocation N/G; risk control N/G; risk-adjusted outcome N/G; data integrity F; hindsight avoidance A; overall F; scanner F-unverifiable.

## Codex handoff and learning
All A001–A078 prior policy designations and INCONCLUSIVE causal outcomes have been retained. Proposed **A079 (SHADOW ONLY)** quarantines implausibly high RVOL ratios generated by tiny matched historical denominators and requires timely catalyst provenance, quote/depth, RTH VWAP/opening-range and current float checks. Run 50 prospective cases and 20 low-baseline injections; success is zero fake timing/fills, 20/20 correctly quarantined inputs, fewer false chases without more than 10% loss of qualified recall.

Nine machine-actionable sections, source links, per-mover fields, complete risk controls and experiment acceptance/rejection criteria are in the companion JSON. **Priority remains A072** host-independent, immutable 16:00 canonical account certificate, with A069 TypeError repair and A078 morning manifest verification.

### Final
**Official daily return: N/A; official ending equity: N/A; current scoreboard: UNVERIFIED; daily grade F; Daily Price Jumps scanner grade F.** Next session priority: source-bound EOD certification, not greater deployment or risk.

References: [09:15 research](https://github.com/Harpcity33/trader-brain/blob/main/reports/2026-11-17/TRADER_BRAIN_PAPER_1000_2026-11-17_0915.md), [SLS market record](https://www.investing.com/equities/galena-biopharma-historical-data), [RCAT market record](https://www.investing.com/equities/timefirevr-historical-data), [GP market record](https://www.investing.com/equities/greenpower-motor-company-inc-historical-data), [AYTU SEC filing](https://www.sec.gov/Archives/edgar/data/1385818/000143774926037188/aytu20260930_10q.htm), [market context](https://apnews.com/article/4d5a35288288389d894e63e6bebe8240).
