# Human-support follow-up draft — not sent

Please escalate the existing inquiry to an API/account-risk specialist. We
have narrowed the intended workflow to regular US equity hours only; there
will be no premarket or after-hours trading. The authenticated account is
No Borrow Margin with IBKR Pro tiered stock pricing. Gateway Read-Only API
has been disabled with the owner's approval. All order precautions must
remain enabled, including no bypass of API order precautions. We will not
purchase additional data services without separate owner authorization.

Please confirm these specific points:

1. Which TWS API account tags and callbacks constitute immediately spendable,
   unleveraged USD capacity for this exact No Borrow Margin account, including
   fees, pending buys, holds, and same-day sale proceeds? Are AvailableFunds,
   BuyingPower, CashBalance, and SettledCash sufficient, and what update or
   settlement limitations should an automated client handle?
2. With the existing fee-waived US nonconsolidated streaming subscription and
   a completed Market Data API acknowledgement, what real-time data is
   actually available through TWS API? Does it permit the broker to perform
   its price-percentage precautions when the trading signal/quote source is
   an external licensed provider? We are not asking for consolidated NBBO
   entitlement to be assumed from a nonconsolidated feed.
3. Can this exact setup submit whole-share BUY LIMIT, regular-hours GTC SELL
   stop-market, SELL LIMIT exits, and cancellations programmatically without
   per-order manual Transmit and without enabling any precaution bypass? If
   a precaution requires a human acknowledgement, what documented state and
   callback identifies the blocked order without creating a duplicate?
4. For separate dedicated read/command clients plus any manual/external
   orders, which master-client configuration and APIs provide complete open
   order and execution visibility? What is the retention boundary for
   completed orders/executions, and what supported historical source should
   resolve an old exact orderRef/orderId/permId that falls outside that window?
5. Are there account-specific limitations on sequential regular-hours GTC
   stop-market protection after partial fills, cancellation acknowledgement,
   or flattening an owned whole-share equity position before the close?

The prior automated reply recommended bypassing precautions. That is outside
the approved controls, so please identify a supported alternative or confirm
the limitation explicitly. We do not seek a guarantee of fills, stop prices,
profits, or loss limits.
