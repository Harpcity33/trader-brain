# Live Options Ledger

This directory is the append-only evidence domain for
`titan_options_live_attended_v1`. Live equity and aggressive-paper records must
never be written here or included in this ledger's aggregate metrics.

Each decision/trade record must contain at least:

- immutable record ID, timestamp, strategy version, account suffix, and data
  provenance/revisions;
- `setup_id`, `SETUP_SCORE`, `EXECUTION_SCORE`, and hard-gate result/reasons;
- underlying, market regime, sector context, thesis, structural invalidation,
  targets, expected holding period, and instrument-route decision;
- option ID, type, expiration, DTE, strike, multiplier, delta, gamma, theta,
  vega, entry/exit IV, bid, ask, midpoint, spread dollars/percent, sizes, volume,
  open interest, and quote timestamps;
- exact order tuple, review reference/fingerprint, review expiry, all alerts,
  `order_checks`, fees, disclosures, confirmation audit, order ID, state changes,
  executions, and unknown-state handling;
- `planned_R`, `realized_R`, `MAE_R`, `MFE_R`, exit efficiency, gross/net P&L,
  spread/slippage/fees, process grade, underlying/option returns, implied and
  realized moves;
- tactical planned loss and full-premium maximum/stress loss, each in dollars
  and as a fraction of current broker-confirmed equity; and
- stock/call/put/debit-spread route comparison, marking the selected route as
  actual and every feasible unselected route as shadow.

Records are append-only JSONL or immutable dated artifacts. Corrections append a
new record referencing the superseded record; historical records are never
rewritten. Secrets, full account numbers, connector credentials, and tokens are
forbidden.

