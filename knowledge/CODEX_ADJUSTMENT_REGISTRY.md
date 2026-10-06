# Trader Brain Codex Adjustment Registry

## Authoritative experiment

- Strategy ID: `TRADER_BRAIN_PAPER_1000`
- Status: ACTIVE
- Starting experiment balance: $1,000
- Legacy $500 experiment status: RETIRED as of 2026-10-06
- Rule: never mix balances, trades, P&L, deployment metrics, or scoreboards between the retired $500 experiment and the active $1,000 experiment.
- Rule: one authoritative ledger/state store must be used for EOD reporting and Codex learning. Duplicate state stores are non-authoritative until reconciled.

## Day-over-day learning contract

Every EOD handoff must append evidence rather than reset the strategy. For each adjustment below, maintain one of:
ACTIVE | HELPED | HURT | INCONCLUSIVE | SUPERSEDED | RETIRED

Every adjustment must include:
- hypothesis
- implementation target
- success metric
- failure metric
- observation count
- evidence
- next action

## Active adjustments as of 2026-10-06

### A001 — Catalyst quality before 90+ setup score
Status: ACTIVE
Hypothesis: requiring at least a B-grade fresh catalyst before a setup can score above 90 will reduce high-score failures in low-quality speculative names.
Implementation target: composite scorer / mover ranker.
Success metrics: improved win rate, profit factor, and expectancy for >=90 setups; fewer financing/reverse-split/no-catalyst names above 90.
Failure metric: lower opportunity capture without improvement in PF/expectancy.
Evidence: AGMH, XHLD, DLXY, repeated MI trades received very high scores despite weak or stale catalyst quality and produced poor outcomes.

### A002 — Remaining Move Capacity / move-consumed penalty
Status: ACTIVE
Hypothesis: penalizing setups where much of the session move is already consumed will reduce chase losses.
Implementation target: mover scanner and entry validator.
Required fields: FIRST_DETECTED_AT, FIRST_PRICE, MOVE_ALREADY_CONSUMED_PCT, REMAINING_MOVE_CAPACITY.
Success metrics: lower MAE and higher MFE after entry; fewer entries within 5% of HOD/LOD unless explicitly fade/reversal setups.
Evidence: DLXY entered near HOD with an unrealistic target; XHLD was entered after most upside was consumed.

### A003 — Same-ticker attempt limit
Status: ACTIVE
Hypothesis: limiting each ticker to two attempts unless a new higher-timeframe regime/catalyst appears will reduce churn.
Implementation target: execution/risk state machine.
Success metrics: lower loss per ticker, improved PF, fewer repeated stop-outs.
Evidence: repeated MI, OLB, SOFI, TGE, RIVN attempts frequently turned initial signals into cumulative losses.

### A004 — Institutional catalyst priority
Status: ACTIVE
Hypothesis: binding M&A, major contracts, earnings/guidance, regulatory decisions, and other high-certainty institutional catalysts should outrank raw microcap percentage movers.
Implementation target: fresh-news scanner + ranking weights.
Success metrics: higher share of top-ranked candidates with A/B catalysts; improved discovery of names such as CEG/PTC/RXO/ACN/TSLA/STX/ON.
Evidence: several high-quality institutional movers were missed while capital was allocated to weaker speculative setups.

### A005 — Fresh 9:15 rebuild
Status: ACTIVE
Hypothesis: rebuilding the board at 9:15 from all 7:00–9:15 news and market data will improve same-day catalyst capture.
Implementation target: 9:15 planner.
Success metrics: lower late-detection rate; more first detections before 9:30 for same-day catalysts.
Evidence: TSLA delivery release and other morning catalysts appeared after the initial overnight/prior-session screen.

### A006 — Discovery vs execution separation
Status: ACTIVE
Hypothesis: recording a mover as a discovery success even when execution is rejected will let Trader Brain learn scanner alpha separately from trade alpha.
Implementation target: scanner schema and EOD metrics.
Success metrics: distinct discovery hit rate, late-detection rate, execution win rate, and rejected-mover future MFE.
Evidence: OLB/BEAT/KOD/GLND and others showed real discovery value even when spreads/structure made entry ineligible.

### A007 — Meaningful allocation / avoid token probes
Status: ACTIVE
Hypothesis: sizing high-quality, low-risk setups meaningfully will improve capital efficiency versus tiny probe trades.
Implementation target: allocation engine.
Success metrics: capital utilization, P&L per unit risk, share of notional in top-ranked setups.
Failure metric: higher drawdown without expectancy improvement.
Evidence: OLB's best trade used one share while weaker names consumed more capital; large portions of paper buying power were frequently unused.

### A008 — Exact ledger integrity
Status: ACTIVE
Hypothesis: one authoritative ledger with exact entry/exit, spread, VWAP, catalyst, component scores, MAE/MFE and P&L will eliminate contradictory EOD reports and improve Codex learning.
Implementation target: persistence/state architecture.
Success metrics: zero duplicate official ledgers; zero unexplained P&L discrepancies; complete trade rows.
Evidence: multiple $1,000 runtime/state stores produced conflicting EOD equity and position states.

## Required EOD Codex output

Each day append:
1. official daily return and ending equity
2. cumulative scoreboard
3. trade-by-trade results
4. mover discoveries / misses / late detections
5. adjustment registry status updates
6. newly proposed experiments
7. data-integrity faults
8. exact next-session configuration/code changes

Codex should read this registry before modifying ranking, scoring, allocation, risk, or persistence logic.
