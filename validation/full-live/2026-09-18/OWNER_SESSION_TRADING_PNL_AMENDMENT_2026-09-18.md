# Owner-approved session trading-P&L measurement amendment

Status: **OWNER APPROVED FOR IMPLEMENTATION — NOT ACTIVATED**

Target: the existing IBKR Pro No Borrow Margin account ending 3103.

## Owner instruction and precedence

On September 18, 2026, after being offered a choice between session trading
P&L against the pre-trade balance and the existing midnight whole-account
return, the owner explicitly selected:

> **session trading P&L against the pre-trade balance**

The preceding proposal explained that the pre-trade balance is fixed for the
day; trading P&L uses fills, execution fees and fresh prices; non-trading
gains/losses are excluded from this numerator and still require separate
account reconciliation. This selection changes the measurement basis and
baseline boundary, not the existing ten-percent loss trigger or fifteen-percent
profit aspiration, and does not itself activate trading.

This amendment supersedes the midnight-Eastern baseline and whole-account
NAV-minus-external-flows performance calculation in:

- `validation/full-live/2026-09-14/OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md`,
  SHA-256 `78571bb3f19157d5f2a8d81976ba7a4a4f0bdc782683b276130b59ca1627c2ad`.

The original operational approval and all unrelated restrictions remain
binding, with their historical files preserved unchanged:

- `validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md`,
  SHA-256 `cc9de013e880864b8d7400837a71c8742b3116a2fe7269f252cfc88bd50617cf`.
- `validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md`,
  SHA-256 `d27a1cc0c79629292f1353652440c984d3c7d510bcf3e08c4c595f25da7f4aae`.

## Exact new measurement

The new model is `session_trading_pnl_against_pre_trade_balance`.

1. Freeze positive USD total account equity/net liquidation value from a
   genuine pre-entry broker observation after initial exposure reconciliation
   establishes the permitted flat start. This is not midnight equity, cash,
   buying power, a market high-water mark or an arbitrary later balance. Bind
   the baseline to the exact account, New York trading date and its evidence.
   Freeze it before the first eligible trade. Restart, reauthentication,
   release change, later profits or deposits cannot reset or enlarge it.
2. Session trading P&L equals USD stock sale execution proceeds minus stock
   purchase execution costs, plus the fresh bid-marked value of residual
   permitted long inventory, minus actual reported execution commissions and
   fees. Count fills and fees once; reconcile duplicates and corrections.
   Unrealized trading P&L is included through the residual value, not added a
   second time. Bid marks are estimates, not guaranteed liquidation proceeds.
3. Deposits, withdrawals, dividends, interest, taxes, FX, accruals, transfers
   and other non-trading adjustments do not silently enter this trading-P&L
   numerator. Independently reconcile account changes and reduce spendable
   capacity for withdrawals/obligations. Unknown accounting, assets,
   adjustments or execution coverage block new entries rather than being
   assumed zero. This amendment does not declare external-flow completeness.
4. At or below `-0.10 * fixed_pre_trade_balance`, irreversibly lock new entries
   for the account trading day and invoke the ordinary guarded closeout path.
   Subsequent gains, balance changes or restarts cannot clear that loss latch.
5. Before the loss boundary, maximum aggregate downside capacity is
   `max(0, min(0.10 * fixed_pre_trade_balance,
   0.10 * fixed_pre_trade_balance + session_trading_pnl))`. All open, pending,
   uncovered and unresolved downside, proposed new risk, future commission
   obligations and positive execution reserves must fit this capacity.
   Fees already deducted from P&L are not deducted again as incurred fees;
   fees still to be incurred remain reserved. Gains cannot increase the
   original ten-percent ceiling.
6. The daily aspiration remains `0.15 * fixed_pre_trade_balance`, measured
   using the same session trading-P&L numerator. It is not a promise,
   compulsory trade, profit ceiling, forced exit or post-goal floor.
   `post_goal_floor` remains null. No legacy percentage overlay or additional
   dollar-profit/dollar-loss threshold is reinstated by this amendment.
7. Persist the immutable baseline, cumulative reconciled execution/fee
   history, observation ordering, incident state and loss latch. Arm a
   pending read incident before observing data. Missing, stale, future,
   conflicting, wrong-account/day or unverified observations cannot authorize
   entry. A later healthy reading cannot silently erase an unresolved failed
   read or blind interval; not observing a breach is not proof none occurred.

The broker's dedicated `reqPnL` callback may be used as a diagnostic
cross-check, but it is not the required numerator for this new model. Its
configured reset schedule must not be confused with this session boundary.
The new model must have its own evidence/state schema; it cannot be represented
by fabricating legacy realized-P&L, weekly-P&L, peak-equity, midnight-equity or
external-flow receipts. Authentic source, coverage and durable-state checks
remain required, with their actual scope represented honestly.

For this new model only, the older measurement-specific `reqPnL`, prior
week-to-date, equity-high-water, midnight-baseline and external-flow performance
receipt prerequisites are replaced by independently verified session-baseline,
cumulative execution/fee, accounting-reconciliation and monotone risk-state
evidence. This is a versioned replacement of measurement lineage, not a waiver
of authentic inputs, complete exposure reconciliation, audit/state consistency,
or historical loss/incident preservation. The legacy measurement paths retain
their own unchanged prerequisites; no old readiness property is redefined.

## Restrictions and activation boundary

All other approved long-stock, whole-share, no-borrowing, cash-capacity,
commission-reserve, entry-quality, no-ADD/re-entry, session/calendar,
protection, cancellation, notification and no-overnight rules remain
unchanged. In particular, no order-precaution bypass, unknown-submit retry,
overlapping exit or unsupported risk-reduction action is authorized.

Ten percent remains a control trigger, not a guarantee that gaps, halts,
slippage, delayed fees, failed protection or outages cannot produce larger
losses. Fifteen percent remains an aspiration, not an expected daily return.

This record authorizes implementation and testing of the changed measurement.
It is not a broker fact, signed data receipt, order instruction, credential
authorization, account-setting change, service start or trading activation.
The installed legacy release remains paused until a reviewed migration, real
source integration and release-bound acceptance satisfy the independent
activation gates. Historical approval files and historical loss/incident
state must not be rewritten or discarded to make the new path appear ready.
