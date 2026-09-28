# IBKR daily-equity provider contract review — September 14, 2026

## Result and scope

No documented source examined proves both the required 00:00 America/New_York whole-account starting equity and exhaustive external cash flows synchronized with a current valuation no more than five seconds old. This is a source-contract gap, not a missing HMAC-signing function. The production gate remains blocked; no provider, receipt, balance, zero-flow assertion, credential, or trading authority was fabricated.

The owner explicitly selected the daily starting balance and the 15%/10%
percentages. Midnight and the synchronized five-second proof are the current
implementation's precise interpretation and evidence requirements; they are
not words from that owner instruction or promises made by IBKR. This review
does not silently change that checked-in contract or claim the owner separately
selected those provider-timing details.

This review used public IBKR documentation and read-only local source/SDK inspection. No broker requests, report generation, account changes, credential reads, paid data, or order operations were performed. No historical-only parser was added because it would not close the requested live gate.

## What the existing TWS route actually supplies

- The installed `ibapi` source exposes `reqAccountSummary`, `reqPositions`, `reqPnL`, and `reqAccountUpdates`. Its summary-tag inventory includes `NetLiquidation` and `PreviousDayEquityWithLoanValue`; inspecting these definitions does not authenticate an account or establish current data availability.
- The local bridge requests current account-summary balances, positions, orders, executions, and realized P&L (`src/titan_brain/live/broker/ibkr_read.py:60`, `:100`, `:612`). An authenticated successful read can prove what IBKR returned, subject to collection/account/client coverage; it does not establish an unrequested historical balance or an exhaustive transfer ledger.
- `PreviousDayEquityWithLoanValue` is previous-day 16:00 ET marginable equity in the securities segment. It is not whole-account NLV at midnight. Current `NetLiquidation` is not a historical starting balance. [IBKR account-value definitions](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-updates/account-value-keys).
- Initial account-summary values are followed by changed-value updates on a three-minute schedule which IBKR says cannot be changed. A fresh callback receipt alone therefore does not independently prove a five-second economic valuation. [IBKR account-summary timing](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-summary/introduction).
- `reqAccountUpdates` has richer `updatePortfolio` market-price/market-value fields and `updateAccountTime`, but those are not currently collected. The documented callbacks have asynchronous update behavior; `accountReady=false` during broker resets warns that subsequent values may be stale or incorrect. This is a possible future read-only integration, not proof of an atomic NLV/position/transfer watermark. [IBKR account-update callbacks](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-updates/receiving-account-updates).

## Alternative sources examined

| Source | Genuine documented data | Why it does not satisfy this live contract |
|---|---|---|
| Activity Flex: Change in NAV | Reporting-period starting/ending NAV, deposits/withdrawals including cash transfers, separate internal cash transfers and asset transfers | Period-level accounting does not itself establish midnight valuation or intraday completeness. [Field definitions](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm). |
| Activity Flex: Cash Transactions | Currency, transaction date/time, amount, type; includes dividends as well as deposits/withdrawals | Not every cash entry is an external contribution. No verified five-second complete-through watermark or atomic current-NLV binding was found. [Field definitions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm). |
| Activity statements | NAV, P&L and transactions for the statement period | IBKR specifies reporting cutoffs of 17:15 EST for commodities and 20:20 EST for securities, with statements available around midnight. Availability time must not be relabeled as valuation time. [Reporting schedule](https://www.interactivebrokers.com/docs/web-api/account-management/reporting/activity-statements). |
| Account Management transaction history | Deposits, withdrawals, position transfers and internal transfers, up to seven days back | Documented limit is one request per ten minutes; no five-second synchronized complete-through guarantee. The institutional Account Management API is not granted by a TWS SDK login. [History endpoint](https://www.interactivebrokers.com/docs/web-api/account-management/funds-and-banking/introduction), [access context](https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-account-management/). |
| PortfolioAnalyst transactions | Transactions for specified accounts and one specified contract | Not an exhaustive account cash-flow query; one request per fifteen minutes. `includesRealTime` indicates whether trades are up to date, not a complete account-transfer watermark. [Endpoint schema](https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio-analyst/transaction-history), [pacing](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/). |

Flex authentication requires a separately enabled service token and a configured query ID. The token can cover linked accounts according to query inclusion; any future configuration must select and validate the exact account. Checked-in provider bindings contain no Flex token/query or Account Management OAuth locator; this review did not search or read secrets to assert whether unrelated credentials exist. Creating a token alone would not repair the timing and completeness gap. [Token setup](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/enable-and-create-access-token), [query setup](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/create-a-flex-query).

## Local binding boundary

- `ibkr_risk_evidence.py:566` requires exact New York midnight starting time, flow time equal to valuation time, same trading date and age 0–5 seconds. `:1475` binds the signed valuation amount/time to the raw snapshot. These are local risk-evidence requirements, not broker API promises.
- `ibkr_read.py:614` currently labels the snapshot with local collection completion time. A future genuine producer must distinguish receipt time from economic valuation/complete-through time; signing that local timestamp cannot create a provider guarantee.
- The stable ledger already freezes an authenticated day baseline and retains flow watermarks across restart/release. It cannot establish the missing first baseline or recover unobserved transfers.
- The existing HMAC is a local authenticity mechanism. A holder of that key can attest supplied bytes, but cannot thereby make guessed, delayed, or incomplete provider data true.
- Do not infer cash flows from cash-balance changes: trades, fees, dividends, accruals, FX and transfers are different events. Do not substitute buying power, previous-day loan value, first late-day NLV, or NLV minus an unaligned P&L field for the approved starting balance.

## Executable next steps requiring a choice

1. **Keep the exact present contract:** obtain an IBKR API-support answer identifying an available no-new-fee account-specific route with (a) whole-account NLV effective 00:00 America/New_York, including DST/holiday behavior; (b) complete external cash/asset-transfer scope, correction/cancellation/FX treatment and explicit complete-through time; (c) current NLV and flows aligned to the same valuation instant within five seconds; and (d) permissions, pacing, reset and reconnect semantics. Ask for real response field definitions and a sanitized sample, not a general statement that data is “real-time.” Once proved, configure only the necessary read credentials/query and implement/test that exact parser and binding. No such route was established here.
2. **Change the measurement contract:** the owner may explicitly choose a broker-supported reporting/session boundary and an independently defensible cash-flow timing policy after seeing those limitations. That requires a new immutable policy amendment and corresponding parser, reconciliation and adverse-transfer tests before activation. Merely widening freshness or renaming report dates is not sufficient; neither a promise of no transfers nor an empty history response proves absence of unreported inflows.
3. **Pending either decision:** retain blocked new entries and use genuine read-only broker reports for reconciliation/research. Continue unrelated notification and broker-coverage setup separately. No new subscription or payment is justified by these findings, and no paid product was shown to solve this contract.

The honest completion boundary is therefore “source investigated; exact live provider contract unavailable,” not “authentic daily feeds connected.”
