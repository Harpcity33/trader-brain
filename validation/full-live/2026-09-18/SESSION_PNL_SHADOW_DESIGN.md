# Proposed session trading-P&L shadow, September 18, 2026

## Scope and authority

This is a research/development alternative, not an amendment to the September 14 owner approval. The currently recorded policy measures midnight Eastern whole-account net liquidation less external flows. The new module measures session trading P&L from a claimed flat pre-entry starting point; those are different contracts.

`src/titan_brain/live/broker/ibkr_session_pnl_shadow.py` has no production wiring, broker/network/Keychain access, CLI, mutation route, or authority issuer. All public results are explicitly unapproved, diagnostic-only and unauthenticated. Synthetic input remains labeled synthetic. A successful calculation must not be fed to the existing risk-evidence adapter as if it were approved broker evidence.

## Calculation

For USD whole-share long stock, aggregate execution cash flows plus residual inventory marked at a fresh bid, minus execution-linked actual fees. Require explicit claimed coverage from baseline through observation, reconciled positions/exposure/accounting, no unexplained accounting delta, and consistent account/instrument/time scopes. Bid size must cover the marked residual quantity; a bid remains an estimate rather than guaranteed liquidation proceeds.

Interest, dividends, taxes, FX, accruals and corporate actions are not silently folded into this trading measure. They need separate account reconciliation. The input status enums express caller claims; they are not evidence issuers. Unknown data is never substituted with zero. Execution corrections remain blocked pending a supported correction/reconciliation design.

The optional separate SQLite store is create-only, freezes baseline identity/value per opaque account binding and New York session date, persists cumulative executions/fees, rejects observation rollback, and retains a measured 10% loss latch across restart or recovery. A deposit cannot increase the baseline. This is ordinary durability, not authenticated or tamper-proof custody. The pure evaluator itself has no memory; persistent behavior requires the store.

The latch explicitly covers observed valid breaches only. False does not establish that no breach occurred between observations or during blocked/missing-data intervals. The public result exposes this limitation; no automatic production recovery is implemented here.

## Read diagnostics

Additive diagnostics in `ibkr_read.py` / `ibkr_runtime.py` distinguish local dispatch attempts, scoped callback/end receipt, missing commissions and normalization. They expose no account identifiers or financial values. Dispatch return is not broker acknowledgement; local callback timing is not a broker valuation timestamp. Existing hard deadlines, normalization requirements and command gates are unchanged.

The pinned SDK was independently exercised offline at server protocol 223: protobuf requests for account P&L and completed orders serialize correctly and corresponding decoders invoke raw and normalized callbacks. This rules out the tested deterministic routing/serialization hypothesis; it does not explain the real Gateway's missing callbacks or prove delivery.

## Required before production integration

1. Settle and record the changed measurement semantics explicitly, preserving approval history.
2. Implement authenticated input acquisition and independently bounded all-client execution coverage, correction reconciliation and non-trading-accounting checks. A TWS end marker alone does not prove global continuity or source valuation time.
3. Introduce honest versioned authority/pricing evidence with supported scope. Local HMAC authenticates issuance, not factual correctness; never weaken the meaning of existing v1 assertions by filling them with fabricated success values.
4. Preserve richer order-status evidence (held reason, parent dependency, quantities, identity, warnings) before accepting a specific `PreSubmitted` stop as protection. Do not globally promote queued orders.
5. Test fault/recovery intervals, partial fills, cancellation races, unknown submissions, stale inputs, actual fees, early close and broker flatness. Persist unresolved blind-interval incidents; a healthy later snapshot cannot reconstruct unobserved threshold crossings.
6. Keep live deployment paused until release-bound acceptance. No fixture result or paper fill establishes live readiness.

## Primary-source basis

- [IBKR P&L reset semantics](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/profit-loss-pn-l/introduction): configured/instrument resets are not automatically midnight Eastern account returns.
- [Commission and fees report](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/commission-and-fees-report): execution-linked cost information.
- [Execution details](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/execution-details/request-execution-details) and [completed orders](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/retrieving-completed-orders/introduction): bounded visible history, not an unrestricted historical ledger.
- [Order status meanings](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/understanding-order-status-message): acceptance/election/held distinctions matter for protection.
- [Weekly authentication](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication): owner authentication remains an operational boundary; no indefinite zero-touch claim.

## Validation record

- Full unittest discovery: 1,388 tests passed in 63.893 seconds using the pinned SDK test environment.
- Final shadow-only rerun after explicit observed-breach limitation fields: 37 tests passed in 0.390 seconds.
- Independent review confirmed both diagnostic concurrency/retry fixes and exact Decimal replay canonicalization. No remaining actionable finding in the non-authorizing scope.
- Repository validation and `git diff --check`: passed.
- One read-only candidate broker diagnostic at 2026-09-18T10:31:48Z failed before collection with `IBKR_RUNTIME_READ_CONNECTION_NOT_ESTABLISHED`; command connection remained false and cleanup completed. Port 4001 had no listener; Gateway UI showed owner login. This run does not establish callback completeness or fresh account state.
- Installed release remains unchanged and paused at generation 16, authority disabled, with no writer lease. No orders, live activation, credentials, policy changes or broker-setting changes were performed.
