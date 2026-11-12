# Trader Brain | November 12, 2026 EOD audit
**TRADER_BRAIN_PAPER_1000 · paper only · QUARANTINED_SOURCE_OFFLINE**

## Official accounting
Mac.attlocal.net was offline (~166 hours since last connection). Its canonical ledger was last independently read November 5: $1,000 simulated cash, 0 positions and 0 completed trades *as of that earlier date only*. The authoritative November 12 opening equity, settled cash, transactions, swings, closing positions, exact P&L, ending equity and official return are **UNAVAILABLE**. Today's ledger is **UNVERIFIED, NOT CONFIRMED EMPTY**.
Canonical episode ID `d0eb2031-8dfb-4e1b-b53d-9bb1bd0b90ac`. Retired $500 (ended Oct 6), previous $1,000 episode, archived XLAB and provisional model are excluded. No retrospective paper fills or 4PM liquidations fabricated.

## 7 AM, 9:15 AM, desk, 4 PM
- 7:00 immutable issuer research: not recovered.
- 9:15 archived plan: reconstructed after cutoff, 234 price-jump results and 272 under-$5 results fetched after cutoff; $0 authorized new paper allocation, 0 new shares, 0 options contracts. No early-discovery credit.
- Intraday scanner and fill event-chain: unavailable; cannot assert zero cycles or missed first detections.
- 4:00 PM: account position liquidation, swing exception and exact closing valuation unverified.

## Major mover audit
External regular-session closes are research comparators, never paper fills. Approximate premarket numbers below are retrospective completed-bar marks, not executable quotations. 09:21 spreads are later not 09:15 NBBO.

| Ticker | Reconstructed 09:15 move | Later spread | Model consumed | External session outcome | Classification |
|---|---:|---:|---:|---|---|
| NVDA | -12.00% | 0.07% | 99.6% | Close $166.05 (-4.42%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| NBIS | -18.75% | 0.13% | 89.7% | Close $128.91 (-16.67%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| TTEK | 12.07% | 1.65% | 100.0% | Close $39.43 (4.84%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| AVEX | 13.60% | 3.62% | 89.7% | Observed $13.91 (14.67%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| LUNR | 11.66% | 0.67% | 74.7% | Close not verified | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| CRWV | -13.69% | 0.12% | 91.6% | Close $56.59 (-6.88%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| CTVA | -12.85% | 0.66% | 95.1% | Close $9.19 (-24.3%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| MSTR | -6.70% | 0.08% | 97.0% | Close not verified | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| CURI | 18.07% | 3.20% | 99.6% | Close $2.86 (20.68%) | REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH |
| PLCE | 438.29% | 1.96% | 74.3% | Close $0.4646 (134.06%) | STRUCTURAL_REJECT |
| CENN | 42.31% | 6.06% | 31.3% | Close $0.1806 (23.44%) | STRUCTURAL_REJECT |

**Critical contrasting behavior:** NVDA fell 12.00% at the reconstructed 09:15 mark but recovered to $166.05 (-4.42%) by close. CTVA fell 12.85% at the reconstructed 09:15 mark, but closed $9.19 (-24.30%) after guidance weakness. Treat both as **external paths only**; neither grants execution alpha or validated missed profit.

**Best under-$5 research catalyst: CURI.** Revenue +13%, adjusted EBITDA +67%, raised guidance. At 09:15 it was around $2.7983, +18.07% on 121,253 shares / $0.34M turnover, with a later 3.20% spread and 99.6% modeled consumption. It closed $2.86 (+20.68%), but lacked qualifying quote/structure evidence. PLCE (Chapter 11) and CENN (very wide spread, speculative) are structural rejects; neither gets paper profit credit.

Clock-matched volume proxy has up to 7 sessions; current float, dilution, reverse split, atomic bid/ask, quote depth, RTH VWAP/opening range, full sector-breadth decision snapshot, and validated catalyst-matched 90-day follow-through are all insufficient for an executable attribution. The exact per-symbol reconstructed 90-day analog statistics, volumes and turnover are included in JSON.

## Capital and cumulative scoreboard
TTEK conditional study only: 12 shares × $42.45 = $509.40 notional; ($42.45−$40.80) × 12 = $19.80 nominal stop-defined risk, 1.98% of stale historical seed. No authorization, fill or certified risk policy. NVDA/NBIS spreads unpriced; no position sizing. Current compounded balance, win rate, expectancy, drawdown, utilization, discovery alpha, execution alpha and all current cumulative metrics are unavailable; no same-day proceeds recycled.

## Grades
Analysis C+; mover discovery F (unverifiable); execution N/A; allocation N/A; risk control N/A; risk-adjusted result N/A; data integrity F; hindsight avoidance A. **Daily process F — auditability; Daily Price Jumps scanner F — unverifiable.**

## Learning and handoff
Preserved all prior A001–A075 with effectiveness INCONCLUSIVE. Added A076 SHADOW_ONLY to separate negative-gap reversal from continued institutional selling, prospective tests on >=40 events across five sessions, decision-time quote and opening-range gates, zero unverified fills. Keep A072 source-bound 4PM certificate and A069 TypeError repair as first priority. All nine learning categories, complete A001–A076 registry and tests are in linked JSON.

**Highest priority Friday November 13:** A072 independent, source-bound 16:00 paper-ledger certification, resilient to host failure. Without it, account performance remains unknowable.

Source: [9:15 snapshot](https://github.com/Harpcity33/trader-brain/blob/main/reports/2026-11-12/TRADER_BRAIN_PAPER_1000_2026-11-12_0915_snapshot.json). [Reuters market close](https://www.reuters.com/business/nasdaq-futures-fall-nvidia-turmoil-rattles-ai-stocks-2026-11-12/). [Corteva issuer](https://www.prnewswire.com/news-releases/corteva-reports-third-quarter-2026-results-updates-fy-2026-outlook-302936913.html).
