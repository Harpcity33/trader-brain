# Live Equity Ledger

Append-only records for the established professional live equity strategy.
Only `instrument=stock` records belong here. Each record requires an approved
`setup_id`, separate `SETUP_SCORE` and `EXECUTION_SCORE`, deterministic planned
and stress risk, actual fills, R metrics, process grade, broker evidence
revision, and every feasible shadow route. Historical rows are never edited.

This ledger does not grant order authority. Broker mutations remain inside the
existing attended Robinhood review and exact-confirmation flow.

