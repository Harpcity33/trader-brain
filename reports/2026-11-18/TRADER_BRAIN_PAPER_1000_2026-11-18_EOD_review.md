# Trader Brain — November 18, 2026 EOD

**Strategy:** TRADER_BRAIN_PAPER_1000 · **Episode:** d0eb2031-8dfb-4e1b-b53d-9bb1bd0b90ac · **Paper only.**

## Official result — QUARANTINED

| Metric | Certified result |
|---|---|
| Official 2026-11-18 daily return | **UNAVAILABLE** |
| Official ending equity | **UNAVAILABLE** |
| Official P&L | **UNAVAILABLE** |
| Official ticker-level ledger | **UNVERIFIED (not empty)** |
| Last recovered historical cash (Nov 5) | $1,000.00; not today's equity |
| Current cumulative scoreboard | Unavailable |
| Process / scanner grade | **F auditability / F unverifiable** |

Desktop Commander reports Mac.attlocal.net **OFFLINE**, last seen ~310 hours before review. Today's canonical ledger, same-day positions, settlement and 4 PM liquidation cannot be inspected. No retrospective simulated fill or zero daily return has been fabricated. Discontinued $500 and duplicate/provisional states excluded.

## Chronology

| Source | Evidence |
|---|---|
| 07:00 board | Missing immutable completed board |
| 09:15 plan | [GitHub report](https://github.com/Harpcity33/trader-brain/blob/main/reports/2026-11-18/TRADER_BRAIN_PAPER_1000_2026-11-18_0915.md), [structured snapshot](https://github.com/Harpcity33/trader-brain/blob/main/reports/2026-11-18/TRADER_BRAIN_PAPER_1000_2026-11-18_0915_snapshot.json); after-cutoff reconstructed as-of; saved approval zero new shares/contracts/notional, not proof of whole-day flat trading |
| Intraday scanner | Canonical first-observation records unavailable |
| 16:00 ledger close | No source-bound certificate; all positions and exits unverified |

## Daily Price Jumps

The archived 09:15 snapshot contains **216 positive-mover** and **395 under-$5** live search results that were not frozen at cutoff. Pre-market bars ended at 09:14:59 ET; quotes around 09:14:25–09:14:36; VWAP is a 5-minute-close weighted approximation, not executable transaction VWAP. No 09:15 RTH VWAP or opening range exists. The canonical first-seen timestamp is missing for every named mover.

| Symbol | Reconstructed premarket | Estimated RVOL | Indicative spread | Move retained / capacity score | Outcome / audit |
|---|---:|---:|---:|---|---|
| NVDA | +2.33% | 0.96 | 0.044% | 58.2% / 53 | 2026-11-18 regular-session close $150.96, -2.90% (third-party quote); positive +2.33% reconstructed PM reversed despite strong Nov 17 after-close earnings |
| BRBR | +20.44% | N/A | 0.226% | 89.3% / 37 | Final regular-session close not independently verified here |
| NBIS | +3.97% | 0.7 | 0.272% | 44.4% / 34 | Independent regular-session closing price not verified |
| CRWV | +2.73% | 0.98 | 0.117% | 36.7% / 29 | Independent regular-session closing price not verified |
| PONY | +10.28% | N/A | 1.585% | 80.4% / 23 | Fresh Nov 18 earnings; regular-session close not independently verified; no qualified entry |
| AMD | +1.34% | 0.41 | 0.118% | 40.7% / 27 | Independent regular-session closing price not verified |
| HIHO | +45.67% | N/A | 0.557% | 75.9% / 7 | Third-party delayed intraday approximately $1.65 (+34.15%), NOT confirmed 4pm close; $1.19-$1.97 tracked range |
| CUE | +24.03% | N/A | 0.568% | 82% / 10 | Close not independently verified |
| GMM | +19.5% | 8.42 | 0.601% | 37.7% / 8 | Close not independently verified |
| GP | +15.27% | N/A | 1.681% | 48.2% / 2 | Close not independently verified |

### Major confirmed external benchmarks

Nvidia's after-close November 17 earnings exceeded expectations, but its **November 18 regular session finished about -2.90% at $150.96** versus a reconstructed +2.33% 09:15 premarket gap. This is an external failed-gap benchmark, not proof of an executable paper short or long. [Nvidia context](https://www.fool.com/coverage/stock-market-today/2026/11/17/stock-market-today-nov-17-nvidia-slides-ahead-of-earnings-but-soars-in-after-hours-trading/).

PONY's earnings announcement was released about **04:00 ET Nov 18**, showing 75.2% revenue growth, but a ~$1.67M premarket turnover and 1.585% indicative spread kept it unqualified. [Company report](https://www.stocktitan.net/news/PONY/pony-ai-inc-reports-third-quarter-2026-financial-results-total-o7mrwg3rdasa.html).

HIHO's **06:00 ET Nov 18** release reported +107% sales growth; despite an external delayed **+34.15% at ~$1.65**, its ~3.23M float, listing issues, transaction complexity and spin-off uncertainty support a structural rejection. This is not a verified 4 PM close. [Company release](https://www.stocktitan.net/news/HIHO/highway-holdings-returns-to-strong-revenue-growth-and-operating-o98b6uuss2yo.html).

External earnings benchmarks **TJX** (roughly +10% session) and **TGT** (roughly +6%) and bearish offering event **AAOI** (about -9% premarket after $550M ATM announcement) are absent from the recovered **top-10** morning board. It is **not established** whether they were missing from the full raw scanner; no retrospective discovery credit. [TJX](https://www.reuters.com/business/off-price-retailer-tjx-beats-quarterly-sales-estimates-2026-11-18/), [TGT](https://apnews.com/article/7a0f5f6ddbc171095b060a903595f074), [AAOI](https://www.benzinga.com/trading-ideas/movers/26/11/63383084/applied-optoelectronics-stock-slides-on-dollar550m-atm-weeks-after-dollar600m-raise).

[Reuters November 18 broad market report](https://www.reuters.com/business/nasdaq-futures-jump-nvidia-forecast-lifts-ai-trade-ahead-anthropic-pricing-2026-11-18/) describes a mixed close, approximately Nasdaq +0.39%, S&P 500 -0.24%, and Dow -1.12%. Premarket AI strength did not uniformly hold.

No mover has validated first-discovery timestamp, completed OR15, RTH VWAP, atomic NBBO/depth, risk-reserved settled buying power and authenticated paper fill. **Discovery alpha / execution alpha: unavailable.** Full per-ticker issuer risk, estimated floats, 90-session analog n and D+1/D+5 and gap fade counts are preserved in the adjacent JSON.

## Best Under-$5 Opportunity

**PONY** is the original research pick for fresh issuer evidence. It was **REJECT_CURRENT_ENTRY**: 382,482 premarket shares, ~$1.67M turnover, ~1.585% indicative quote spread and missing time-matched RVOL. **HIHO**, while a large external price mover, was a structural reject. GP also structurally rejected (reverse split/float/quote risk); CUE and GMM were rejected as lacking fresh verified issuer catalysts. **Approved executable under-$5 opportunity: NONE.**

## Capital and risk

The reconstructed plan listed **unapproved** NVDA 5 shares at reference $162.20, stop $156.70, T1 $168, T2 $174 (**$811 notional; $27.50 stop risk**) and BRBR 30 shares at reference $13.70, stop $12.95, T1 $14.55, T2 $15.50 (**$411 notional; $22.50 risk**). Combining both would consume **$1,222 = 122.2% of the original $1K seed** with **$50 = 5%** nominal stop risk: cash-oversubscribed, even though both individual examples were research only. The actual account's settled capital, prior exposure and risk limits cannot be certified. No same-day proceeds were recycled or assumed available.

Existing morning policy reports normal trade risk 3%, exceptional A+ 4%, premarket 2%, total planned open 5%, new-entry lock 6%, hard daily kill 8%, weekly lock 12%. Preserve without changing and fail closed without certified current equity.

## Cumulative scoreboard and grades

True initial seed $1,000.00; historical Nov 5 recovered cash $1,000.00. Today: **compounded equity, cumulative P&L and return, closed trades, wins/losses, win rate, mean/largest win/loss, profit factor, expectancy, drawdown, allocation utilization, mover-detection and mover-trade outcomes all UNAVAILABLE/UNVERIFIED**. No active historical legacies imported.

Grades: Analysis **C**, Discovery **F (unverifiable)**, Execution **not gradable**, Allocation **not gradable**, Risk **not gradable**, Risk-adjusted result **not gradable**, Data integrity **F**, Hindsight avoidance **A**. Overall process **F (auditability)**; Daily Price Jumps **F (unverifiable)**.

## Complete Codex learning / cumulative adjustment handoff

- Worked: canonical source isolation, primary issuer catalyst context, structural-risk rejections, no invented trades.
- Lost opportunity: unmeasurable trading P&L, NVDA failed gap, external TJX/TGT upside and AAOI downside absent from recovered top-10.
- Scanner misses: unprovable without raw first-seen manifest; positive jumps scanner underweights adverse events.
- Scoring defects: no true VWAP, limited NVDA n=3 analog, missing actual OR15/NBBO, unavailable RVOL on under-$5 names.
- Allocation/risk defects: simultaneous illustrations $1,222 exceed original $1,000, actual equity/settlement unknown.
- Proposed exact change: **A080**, shadow-only cross-sector issuer-event coverage with provenance and post-open regime-inversion checkpoints.
- Next tests: A072/A069 source-ledger regression; A078/A080 5-session issuer recall and timestamp-injection; A075/A077 prospective opening-gap filter; A071 settled-cash concurrent allocation tests.
- Pass metrics: >=90% primary-event recall within 5 min on eligible online sources, 100% ranked omissions explained, 20% fewer false chases with <=10% recall loss, zero false account certification.
- Integrity: host offline, absent immutable 07:00 manifest, post-cutoff 09:15 archive, inaccessible intraday and close, historic TypeError fix unverified.

A001–A079 retained with prior designations, all **INCONCLUSIVE** for effectiveness. **A080** proposed only, also **INCONCLUSIVE**. No code deployment, Codex ingestion, improved trading results, policy relaxation or paper-order permission is claimed. Full structured patch, criteria, per-ticker audit and all 80 adjustments appear in the JSON files.

**Single highest-priority improvement for Nov 19:** implement/verify **A072**, a source-bound, host-independent, immutable 4 PM canonical paper-account certificate, and restore access to the authoritative device.
