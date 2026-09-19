# Session capture and risk integration repair

Implemented and tested September 18, 2026. This is a source checkpoint, not
production selection, an authenticated session measurement, or live activation.
The installed account remains on its earlier paused release.

## Implemented repairs

- The new session reader uses exact-account `reqAccountUpdatesMulti` with an
  empty model and full account values, matching callback/end/cancel IDs. It
  never calls the capped account-summary subscription or reqPnL. Missing ends,
  wrong account/model, conflicting currencies/values, AccountReady=false and
  unconfirmed cleanup fail closed. Legacy readers retain their original scope.
- A separate same-collection exposure route returns immutable facts, the
  unpromoted account base and matched family pages. It does not publish the
  legacy strict-read cache, invent old risk values or claim all-client scope.
- Qualified PreSubmitted stops require frozen exact-account/contract/order
  facts plus matching fresh status/remaining quantity, standalone transmitted
  USD whole-share regular-hours GTC SELL STP, and no hold/warning/conditional
  ambiguity. Generic queued orders remain unprotected. No fill guarantee.
- Session risk store v2 retains all audit events and all pending/failed
  incidents while compacting the in-memory healthy-state projection. Unique
  observation tokens, CAS, sticky loss and full cold replay remain enforced.
  V1 files are rejected, not migrated or reset.
- A separate private append-only typed observation ledger preserves actual
  fills, signed fees, source/receipt times and limitations. The risk pending
  marker commits before a read; evidence commits before calculation/risk
  completion. Crashes between commits leave unresolved risk. Reopen verifies
  the complete chain. This is unkeyed local consistency, not malicious rollback
  protection or source authentication.
- Independent cash reconciliation compares real baseline/current cash with
  deduplicated execution cash, actual signed fees and explicitly identified
  non-trading adjustments. Unexplained residuals are retained; NLV is never
  substituted for cash and residuals never become invented external flows.
- PolicyBundle recognizes the pinned owner-approved session contract, without
  selecting it. Separate session risk inputs/evaluation and a pipeline builder
  reuse durable exposure accounting without fabricated realized/weekly/peak or
  midnight/flow values. Fixed ten-percent capacity, positive reserves,
  historical loss flags, pending incidents and cash checks remain. New-model
  calculation is isolated from caller Decimal precision.

## Actual read-only observations

At 15:15:47 UTC, three successive real session captures completed, including
the previously failing third read in 598 ms. Raw observation and risk stores
reopened identically. At 15:26:52 UTC, a second fresh three-read rehearsal also
completed; observed cash reconciliation matched with zero material blockers,
the third read again took 598 ms, and both stores reopened identically.
Command connections stayed closed and cleanup completed in both attempts.

These are bounded finite-read observations, not a full-day live soak. An empty
visible execution roster or matching cash identity does not establish exhaustive
account history or non-trading-adjustment coverage. Diagnostic baselines and
their deliberately incomplete incidents are never production baselines.

The approved new numerator does not use current NLV. Missing provider economic
timestamps are not an extra unconditional baseline policy requirement: original
fresh exact-account callbacks and stable flat pre-entry observations are checked,
while timing remains explicitly local-receipt/non-atomic. This does not remove
all-client, continuous coverage, accounting, or authentication requirements.

## Tests and review

The final full suite passed 1,698 tests in 84.341 seconds, including the pinned
SDK protocol test and low-precision exposure regression. Repository structural
and security-pattern validation passed for 204 Python files, 34 JSON files and
four TOML files. Whitespace checks passed. These are software checks, not live
trading acceptance.

Hermetic pinned API 10.50.2/server223 tests decode actual outbound request/cancel
protobuf and inbound SDK callbacks with every socket forbidden. Five repeated
collections leave no synthetic subscription active. Independent review covered
subscription lifecycle, proof-bound stop eligibility, ledger/store ordering and
replay, accounting, policy isolation, same-collection exposure and risk consumers.

The risk-store benchmark commits 11,700 begin/complete pairs: 23,401 FULL-sync
events, about 10.3 seconds append and 1.5 seconds cold replay, 20,807,680 bytes.
Loss latch and restart state match. This demonstrates compressed event volume,
not 6.5 hours of broker operation, and does not qualify the separate cumulative
observation ledger/calculator for production cadence.

## Deliberate remaining boundary

Session activation is unconditionally blocked until the mode-specific source
verifier, continuously acquired history, measurement seal, service/preflight/
final-dispatch/closeout integration and reviewed migration are complete. Generic
open/manual risk cannot substitute original entry-to-stop numbers for current
bid-to-stop remaining downside; it remains blocked pending that evidence.
Account-effective all-in remaining fees and fresh residual bid marks still need
their real sources. No config boolean can override these missing integrations.

Gateway's Master client field was visibly blank. Setting the existing local
read client 19735 as Master would change access to other clients' trade/commission
reporting; action-time confirmation was requested and no setting was changed.
Existing source data production was inspected read-only: actual SHADOW pilot/
decision binding matched configuration, but no eligible candidate was fresh at
15:20:13 UTC. No candidate or freshness was manufactured. Discovery composition
already exists; reviewed real binding/configuration is still needed.

No orders, credential replacement, new paid service, live activation, automatic
incident clearing, production database mutation or installation occurred.

Primary protocol references: [IBKR EClient API](https://interactivebrokers.github.io/tws-api/classIBApi_1_1EClient.html),
[IBKR order-status documentation](https://interactivebrokers.github.io/tws-api/order_submission.html).
The installed pinned SDK and actual bounded reads additionally establish the
tested local request/callback behavior; they do not authenticate broader scope.
