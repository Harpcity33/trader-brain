# Codex Handoff — 2026-10-06

## Experiment state
- Active strategy: TRADER_BRAIN_PAPER_1000
- Legacy $500 experiment: RETIRED today by user instruction.
- Do not compound or update the retired $500 scoreboard going forward.
- Paper trading only.

## Today’s key learning from the active $1,000 experiment
- Control Center ending indicative equity: approximately $893.49.
- Recorded active-branch mark-to-market decline from $1,000 start: approximately -10.65%.
- Closed-trade history through today: 29 closed trades, 4 wins, 25 losses, approximately 13.79% win rate.
- Cumulative realized P&L reported: approximately -$93.73.
- Profit factor reported: approximately 0.17.
- Today’s recorded Control Center new-entry notional: $138.6765.
- Today’s recorded closed-trade realized P&L: approximately -$19.23 stored; fill-derived approximately -$19.2403.
- Open pre-designated swing at EOD: DLXY, materially underwater at EOD.
- Risk violations: 0; stop-defined risk remained controlled.

## What worked
- OLB first entry: fresh same-day catalyst, strong liquidity/RVOL, enough remaining move; target achieved.
- Risk sizing remained disciplined.
- Fail-closed handling prevented invented fills on data faults.

## What hurt
- XHLD entered after most upside was consumed.
- DLXY entered near session high with target geometry inconsistent with remaining move.
- OLB was re-entered twice after the best impulse had already been monetized.
- Capital utilization remained low despite available risk budget.

## Confirmed / high-quality scanner miss
- CEG: major Google/Constellation power agreement, institutional liquidity, strong event-day move; no scanner signal recorded.

## Highest-priority changes for next session
1. Require catalyst grade >= B for 90+ setup score unless TECHNICAL_ONLY.
2. Add MOVE_ALREADY_CONSUMED_PCT and REMAINING_MOVE_CAPACITY to ranking and entry validation.
3. Limit attempts per ticker to two unless a new higher-timeframe regime/catalyst appears.
4. Rebuild the board at 9:15 from fresh same-day news.
5. Prioritize institutional catalysts over raw microcap percentage moves.
6. Make allocations meaningful when stop-defined risk is low; avoid token-sized probes.
7. Persist exact spread, VWAP, component scores, MAE/MFE and exit details.
8. Collapse duplicate state stores into one authoritative $1,000 ledger.

## Tomorrow’s experiments
- Test A001: compare >=90 setups with B+ catalyst vs technical-only/weak-catalyst setups.
- Test A002: reject or downgrade entries with low Remaining Move Capacity; track MFE/MAE avoided.
- Test A003: enforce two-attempt limit per ticker and compare per-ticker P&L.
- Test A004/A005: measure whether 9:15 fresh-news rebuild surfaces A-grade institutional catalysts before 9:30.
- Test A007: track percentage of deployed notional allocated to top-3 ranked rule-compliant setups.

## Data-integrity requirements
- Exactly one authoritative strategy ID: TRADER_BRAIN_PAPER_1000.
- Exactly one official ledger/state store for EOD.
- No migration or mixing with retired $500 results.
- Any conflicting state must be flagged, not averaged or silently selected.
