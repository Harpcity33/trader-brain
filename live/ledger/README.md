# Live Ledger

Create one immutable row per proposed or actual order event in `YYYY-MM.csv`. Suggested columns:

`timestamp_et,trade_id,approval_id,symbol,event,side,quantity,order_type,limit_price,fill_price,stop,target,fees,pnl,score,prediction_rank,risk_before,risk_after,rule_deviation,evidence_link`

Never store brokerage credentials, API keys, account numbers, or other secrets in this repository.
