# Trader Brain — 2026-11-19 EOD PAPER review

**Strategy:** `TRADER_BRAIN_PAPER_1000` · **Canonical episode:** `d0eb2031-8dfb-4e1b-b53d-9bb1bd0b90ac` · **Status:** QUARANTINED, host offline 334h as checked post-close.

## Official $1,000 paper ledger and timetable

| Checkpoint | Verified result |
|---|---|
| 07:00 | Immutable event and catalyst manifest missing |
| 09:15 | Reconstructed after cutoff: **$0 new allocation authorized**, zero shares/contracts |
| Intraday | Canonical scanner and desk events inaccessible |
| 16:00 | Intraday paper liquidations and swing exceptions unverified |
| Official ending equity and daily P&L/return | **UNAVAILABLE**, not zero |
| Last historical cash | $1,000 on Nov 5, **not Nov 19 equity** |

All trade fields (entry/exit timestamps and prices, structure, grade/score, quantity, allocation %, stops, targets, partials, realized/unrealized P&L, settled cash, unsettled proceeds, gross entry notional) **cannot be certified**. The ledger is *unverified, not confirmed empty*. No retrospective paper fills. Retired $500 state and duplicate/provisional books excluded.

## Market and research outcomes

AP November 19 closing report: S&P 500 approximately **-2%**, Nasdaq **-2.3%**, Dow **-2.2%**. Early AI gains reversed. This is an externally verified market benchmark, not Trader Brain P&L.

Morning as-of prices are reconstructed; relative volume uses at most five clock-matched sessions, later indicative quotes are ~09:18–09:19, premkt VWAP is a five-minute close proxy, and 90-day averages are **not catalyst-matched**.

| Ticker | Reconstructed 09:15 % | Matched RVOL | Later spread | Premkt move retained | Actual Nov19 close % | Morning research decision |
|---|---:|---:|---:|---:|---:|---|
| PANW | 6.64% | 7.76× | 0.166% | 61.3% | 6.44% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| GOOGL | 4.25% | 3.29× | 0.01% | 97.5% | -3.52% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| PDD | 4.35% | 18.74× | 0.345% | 40.4% | 11.03% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| CRWV | 4.96% | 1.16× | 0.119% | 74.8% | -6.1% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| NBIS | 5.27% | 0.95× | 0.071% | 72.9% | -9.42% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| AVGO | 2.86% | 0.95× | 0.044% | 85.6% | -2.12% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| BABA | -1.51% | 4.1× | 0.144% | 50.6% | -1.27% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| SLS | -13.82% | 11.91× | 1.5% | 54% | -17.05% | STRUCTURAL_REJECT |
| NTES | -4.86% | 21.52× | 2.509% | 70.7% | -11.39% | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| SEAT | 102.34% | N/A× | 0.755% | 83.4% | 62.5% | STRUCTURAL_REJECT |
| GMM | 23.15% | 1.56× | 0.542% | 53.6% | 23.87% | STRUCTURAL_REJECT |
| INHD | 38.18% | N/A× | 0.312% | 36% | Unverified | STRUCTURAL_REJECT |
| CHSN | 44.22% | 877.4× | 14.932% | 77.8% | -4.71% | STRUCTURAL_REJECT |

**PANW:** A earnings catalyst Nov18 16:05, PM mark $362.49 (+6.64%), actual close **$361.81 (+6.44%)**. Strong direct issuer story; no verified 09:45 VWAP/OR15/spread trigger. **PDD:** B+ mixed earnings, reconstructed PM $80.82 (+4.35%), close **$85.99 (+11.03%)**. Some later continuation, but not valid simulated execution. **GOOGL, CRWV, NBIS:** respectively +4.25%, +4.96%, +5.27% premarket references reversed to **-3.52%, -6.10%, -9.42%** at the close. Positive morning sector breadth did not persist. **SLS:** $150M diluted offering at $13, close **$14.89 (-17.05%)**, structural/rejection understandable. **NTES** earnings negative, finished **-11.39%**.

**Ranked-board omissions:** SNEX **-26.71%** (earnings Nov18 ~16:15), LQDT **-24.99%** (Nov19 ~06:55 earnings), WSM **-15.16%** (Nov19 ~08:00 earnings) and VVV **+10.40%** (07:00 earnings) did not appear in retrieved top 13. This is not proof the full scanner missed them. First-seen timestamps and decision-time liquidity are unavailable.

**Anthropic (ANT):** IPO priced $83 on Nov18, started trading **11:48 ET Nov19 at $111.50**, not available at 09:15. Multiple closing feeds vary (~$95.41 vs $97.17); independently published AP closing report says $97.17. Any model using $83 offering price as a retail paper entry is invalid; opening-cross/slippage and post-IPO bars are required. No first-day trade is credited.

**Discovery alpha:** unavailable. **Execution alpha:** unavailable. No post-hoc mover discovery credit.

## Best under $5

**SEAT** was the most substantive under-$5 *event watch* in the original board, but its debt-for-equity recapitalization leaves existing holders with ~20% of stock and brings ~45.8 million new shares. Morning $2.59, later spread 0.755%; closed **$2.08, +62.50% versus Nov18 but -19.69% versus reconstructed morning mark**. Original **STRUCTURAL_REJECT** upheld. GMM and INHD lack sufficiently verified current issuer catalyst/structure; CHSN had ~14.93% indicative spread and reversed to $0.0972 (-4.71%). **No qualified simulated entry.**

## $1,000 cash-account risk and scoreboard

Normal 3% trade risk, A+ max 4%, premarket 2%, combined open planned/stress 5%, 6% daily entry lock with $100 secondary cap, 8% hard daily kill and 12% weekly lock from `config/risk_limits.json`. No risk limit changed. Current certified settled buying power and exposure unavailable.

PANW/PDD/CRWV *illustrative alternatives* would total **$1,958.10** in notional and **$75.10** nominal stop-defined risk, equivalent to **195.81%** and **7.51%** of the stale $1,000 seed, respectively. They were mutually exclusive examples, not executed, and together would oversubscribe cash/risk. Morning approved new cash/risk were $0/$0, not evidence there were no intraday trades. No unsettled proceeds are reused.

Cumulative compounded equity, P&L, return, win/loss count, average/large wins and losses, profit factor, expectancy, drawdown, utilization, mover win rate, mover returns, verified late/missed detections: **UNAVAILABLE**. Only original $1,000 seed and Nov5 stale $1,000 historical cash are known.

## Grades

| Quality category | Grade |
|---|---|
| Analysis | C+ |
| Mover discovery | F — unverifiable |
| Execution | Not gradable |
| Allocation / concentration | Not gradable |
| Risk control | Not gradable |
| Risk-adjusted result | Not gradable |
| Data integrity | F |
| Hindsight avoidance | A |
| **Overall process** | **F — auditability** |
| **Daily Price Jumps** | **F — unverifiable** |

## Cumulative Codex learning and exact proposal

A001–A080 retained with original policy designations; all **INCONCLUSIVE** for measured causal effectiveness. New **A081** shadow-only: IPO opening-auction gating and source-bound cross-sector/downgap issuer-event manifest. No code deployment or paper order authorization.

1. **Worked:** issuer fact-checking, canonical identity, structural dilution rejects, zero retrospective fills.
2. **Lost opportunity:** market gains and downside moves remained unactionable and uncredited without contemporaneous signals.
3. **Scanner:** no reliable first-seen times; SNEX/LQDT/VVV/WSM rank omissions, not proven full-scan misses.
4. **Scoring:** positive premarket AI breadth diverged from close; unvalidated small-N analogs; IPO lacks prelisting OHLC or VWAP.
5. **Allocation:** simultaneous research studies exceed current seed and risk, no current settled cash.
6. **Rules for Nov20:** require first IPO market print + 5/15-minute bars, source news manifest, fresh sector rechecks; maintain cash/risk fail-closed.
7. **Tests:** 5 shadow sessions, 50 issuer events, 5 IPO debuts, 15 negative catalysts, 10 outage and 10 bad-timestamp injections; A072 canonical 20+10+10+10 replay suites.
8. **Metrics:** issuer recall >=90% in 5 minutes online; 100% exclusion documentation; zero phantom pre-IPO entries, false equity, or fabricated paper fills; >=20% fewer false gap chases with <=10% candidate recall reduction.
9. **Unresolved:** Mac offline, current 4 PM certificate missing, TypeError, no current event chain or settlement audit.

**Top priority for Nov20: A072 canonical 4 PM immutable source-bound accounting certificate, restore source connection and capture/fix TypeError.**

Complete data including exact A081 patch, every one of 13 mover feature audits, market source links, and all nine learning sections: [Nov19 machine-actionable JSON](https://github.com/Harpcity33/trader-brain/blob/main/knowledge/daily-paper-handoffs/2026-11-19-TRADER_BRAIN_PAPER_1000.json). Cumulative registry: [A001–A081](https://github.com/Harpcity33/trader-brain/blob/main/knowledge/daily-paper-handoffs/2026-11-19-adjustment-registry-summary.json).
