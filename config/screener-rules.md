# Screener Rules

## Hard eligibility gates

A symbol is eligible only when all are true:

- Common stock or approved ETF on a supported U.S. venue
- Last eligible price is strictly greater than $5.00
- Daily volume is at least 750,000 shares
- Tradable status is active
- Required Massive fields have timestamps and are not stale
- Liquidity permits a defined entry, stop, and exit

Price and volume are gates, not scoring bonuses. Reject a candidate that fails either gate.

## Candidate signals

Rank eligible names using:
- Premarket gap and dollar volume
- Relative volume versus comparable sessions
- Spread and liquidity quality
- Technical structure, levels, and VWAP relationship
- Catalyst and context; fresh news is useful but not mandatory
- Prior-move behavior and tendency to hold or fade
- Market, sector, and peer confirmation
- Massive news, snapshot, aggregates, trades, quotes, and reference data

## Data discipline

Record endpoint/source, market timestamp, retrieval timestamp, adjusted/unadjusted status, and missing fields. Do not silently substitute delayed values. Flag halts, splits, offerings, symbol changes, and abnormal prints.
