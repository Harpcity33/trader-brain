# Equity Live Core — Deterministic Risk Overlay v1

This is an additive migration overlay for the existing
`robinhood-momentum-engine`. It changes no reconciliation, order-review,
explicit-confirmation, unknown-order, premarket-protection, or EOD-flatness
control. The prior automation snapshot remains the rollback baseline.

On every proposed entry, use current broker-confirmed usable equity and the
smaller of the configured percentage cap and applicable absolute dollar cap:

- normal planned risk: 3%
- A+ planned risk: 4%
- premarket planned risk: 2%
- premarket stress risk: 4%, including positive liquidity/slippage reserve
- total open plus pending planned risk: 5%
- irreversible daily new-entry lock: 6%, with the existing $100 ceiling
- hard daily-loss kill: 8%
- weekly loss lock: 12%
- live drawdown review: 20%

Record planned/stress dollars and percentages plus remaining daily and
portfolio risk. Unknown or inconsistent broker equity, P&L, open/pending risk,
or reserve evidence fails closed. This overlay never authorizes an order.

