# IBKR gate evidence — September 14 evening follow-through

Scope: setup and bounded read-only verification for the IBKR account ending
3103. No order was placed, modified, or canceled. No paid subscription was
added. This is evidence, not a provider-authority or activation receipt.

## Owner-authorized setting change

The owner expressly approved turning off **only Read-Only API** in the open
IB Gateway. The request disclosed that connected API clients could then
submit orders, that Titan would remain PAUSED, and that **Bypass Order
Precautions for API Orders** would remain off.

The Read-Only API checkbox was visibly checked before the change and visibly
unchecked afterward; Apply was invoked. The next fresh source probe at
`2026-09-15T00:00:11Z` completed the account-read collection that had failed
under Read-Only API. No other Gateway setting was intentionally changed.
Reopening the native configuration subsequently yielded an unavailable
screenshot/stale menu-only accessibility surface, so a fresh post-Apply
visual verification of the precaution checkbox is still required. Successful
reads do not establish that control's state or grant order authority.

The observed pre-change error had code 321 **and the canonical Read-Only API
cause**, in a modern callback with a dot-delimited request prefix. Code 321
alone is generic validation failure. The new classifier retains only an
allowlisted cause; it does not retain raw broker text or advanced JSON.

## Authenticated Portal observations

At approximately `2026-09-15T00:01–00:02Z`, the authenticated IBKR Portal's
account settings showed:

- Account Type: **No Borrow Margin**.
- IBKR Pricing Plan: **IBKR Pro; Stocks: Tiered**.
- Current US equity subscription: **US Real-Time Non Consolidated Streaming
  Quotes**, **Fee Waived**. Total displayed subscription charge: **USD 0**;
  no pending subscriptions.
- **Market Data API access is not certified**. The API acknowledgement has
  not been completed. The non-commercial subscriber status was also not set.
- The separate Portal trading session was not connected to live brokerage/
  market-data permissions. Its portfolio values were explicitly delayed and
  are not used here as fresh broker reconciliation or risk evidence.

The Market Data API Supplement was opened for owner review only. It contains
legal use/redistribution restrictions and requires an electronic signature.
The owner was asked to review and submit it personally if accepted. No
signature or submission was performed by the agent. Acknowledgement alone
would not prove API feed entitlement, NBBO coverage, or no-warning execution.

Tiered pricing is now an observed account fact. The approved $1/order floor
is still not a verified all-in upper bound for every allowed quantity/route.
The remaining fee gate is that exact bound, not the identity of the plan.

## Support response reviewed

The owner's existing inquiry received an authenticated IBKR Message Center
email dated September 14 at 07:40 ET. The response identifies itself as
AI-generated and offers escalation to a human representative. It does not
confirm No Borrow Margin-specific API field/callback or same-day-proceeds
semantics. Its external-market-data answer recommends enabling the API order
precaution bypass. That recommendation was **not implemented** and does not
satisfy the approved no-bypass policy.

No support reply was sent in this turn. Private email headers, destinations,
full account identifiers, and credentials are not included in this evidence.

## Interpretation boundaries

- A successful collection proves the requested callbacks ended for that
  observation, not unlimited historical or every-client execution coverage.
- IBKR completed-order retrieval is bounded to the current day; absent old
  references remain unresolved. Successful fills alone never prove an
  autonomous confirmation contract.
- API acknowledgement, data entitlement, precaution behavior, account
  capacity semantics, effective fees, and activation authority are separate
  gates. None is inferred from another's success.
- The private HMAC provider/policy receipt is an application verification
  control. It is not a certificate that IBKR issues in that custom format.

## Scheduler/runtime separation

The saved original `robinhood-momentum-engine` automation is PAUSED. The
separate IBKR desk heartbeat is ACTIVE on the requested schedule: premarket
analysis at 07:00, 07:30, 08:00, 08:30, and 09:00 ET, then one-minute runs from
09:30 through 15:59 on weekdays, subject to the prompt's exchange-calendar
gate. Its current prompt still requires exact attended confirmation and is
not the full-autonomous coordinator. Neither autonomous launchd label is
loaded. The installed runtime remains PAUSED with authority 0 and no writer
lease. No schedule was changed during this verification.

The API reader is currently client 19735 and the command client is 19736.
Gateway's Master API client ID was observed blank. Under the official client
visibility contract, callback completion on this reader does not prove
execution visibility for the command client, other clients, or manual TWS/FIX
orders. A verified configuration/coverage solution is still required; no
master-client setting or client ID was changed in this investigation.

Official references: [error codes](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/error-codes),
[completed-order scope](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/retrieving-completed-orders/introduction),
[order precautions](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/understanding-order-precautions),
[platform versus API data](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/tws-data-vs-api-data),
[master/client-ID visibility](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/client-id-0-and-the-master-client-id),
[stock commissions](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php?re=amer).
