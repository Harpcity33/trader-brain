# Attended options: implementation and acceptance plan

Status: engineering handoff, not a deployment, trade recommendation, broker
authorization, or claim of live readiness. No production settings or broker
permissions are changed by this document or the accompanying pure calculator.

## Owner-approved product direction

- Prioritize an attended options platform, with exact-order owner review.
- Select contracts expiring within 10 calendar days at entry.
- Permit positions to remain open overnight.
- Make the full account the allocation ceiling, recalculated from fresh facts.
- Replace the proposed options policy's fixed 10% loss rule with trade-specific
  loss analysis. Do not substitute another universal loss percentage.
- Support owner intervention; do not assume Titan is the only account actor.

Full-account capacity is not a target allocation, a recommendation to invest
everything, or a reset to an original balance each morning. Losses reduce the
account, existing positions remain committed, pending orders reserve resources,
and proceeds are reusable only when broker/account settlement rules permit it.
Account equity and margin buying power are not interchangeable with spendable,
unborrowed cash. No historical account balance belongs in source or defaults.

## Proposed first execution lane — not yet owner-approved

Long calls and puts only; no short legs, spreads, leverage, or adjusted contracts
until explicitly supported. Use limit-order entries during regular market hours,
exclude new same-day-expiry positions initially, and plan to close before expiry.
An expiry exit can fail to fill: retain escalation and exercise/delivery handling.
These are engineering proposals requiring product acceptance, not decisions
inferred from the approved 10-calendar-day window or permission to hold overnight.

## Current starting point

Inspection baseline: Kiro PR [#9](https://github.com/Harpcity33/trader-brain/pull/9),
commit `7c39f0d44d0e9e2dd9d7369c182f4ca392603dda`. The PR was open and unmerged at
handoff. SDK-recovery PR [#10](https://github.com/Harpcity33/trader-brain/pull/10)
is separate; preserve its reviewed inventory pin and provenance. Do not equate
either PR's existence, passing unit tests, or an SDK snapshot with live readiness.

The current IBKR production path is stock-only:

- `config/full_live_ibkr.json`: allows stocks and sets `options_enabled=false`.
- `src/titan_brain/live/broker/ibkr_orders.py`: `IbkrContractIdentity` accepts
  only STK/USD/SMART and has no complete option identity.
- `src/titan_brain/live/broker/ibkr_sdk.py`: rejects non-STK contracts and
  expects option-specific fields to be empty in the existing wire boundary.
- `src/titan_brain/live/reconcile.py`: option exposure/orders block new risk.
- `src/titan_brain/live/session_trading_calculation.py`: rejects non-STK inputs,
  omits option multipliers from cash/mark arithmetic, and retains a fixed 10%
  session-breach calculation. It is not the new options risk implementation.
- `src/titan_brain/live/attended_control.py`: equity-specific request and
  capability validation require a distinct reviewed options path.
- `src/titan_brain/live/cli.py`: discovery startup does not pass the session
  trading store; `execution.py` applies the new fingerprint gate only when
  callers supply `plan_bound_fingerprint`.
- `src/titan_brain/live/policy.py`: session mode still produces
  `SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE`.

The legacy `src/titan_brain/options.py` contains reusable analysis concepts,
but its Robinhood assumptions and expiry ranges do not prove IBKR integration.
Do not simply enable an options flag or remove the current rejection guards.

## Numbered milestones and acceptance evidence

1. **Pure, read-only variable-risk arithmetic (first deliverable).** Accept
   explicit contract, quote, balance, fee, scenario, and budget inputs; calculate
   whole-contract feasibility and three separate loss amounts. It must not
   fetch brokerage data, choose budgets, generate prices, recommend trades,
   create executable orders, or assert readiness. Unknown settlement, stale or
   inconsistent funding, missing reserves, or invalid inputs block sizing.
   Acceptance: deterministic offline tests, Decimal money arithmetic, no
   rounded-up quantity, no broker dependency/write route, and no hardcoded
   account amounts. A feasible quantity is not permission to place an order.
   Deliverables: `src/titan_brain/option_trade_analysis.py`,
   `tests/test_option_trade_analysis.py`, `scripts/titan-option-analysis`, and
   the synthetic `examples/attended_option_analysis.json`. The API is
   `analyze_option_trade(payload, now=...)`; the CLI accepts JSON and an optional
   `--now` test clock. Initial supported arithmetic is a single long option,
   1–10 calendar days to expiry, with an intended exit before the expiry date.
   This is supplied-scenario arithmetic, not live integration, provenance
   authentication, a pricing/edge model, or a portfolio joint-stress engine.

2. **Exact option identity and wire representation.** Resolve and bind broker
   conId, underlying, call/put, strike, expiry, trading class, currency, exchange,
   multiplier, deliverable, exercise style, and settlement type. Validate the
   actual expiry calendar and last trading time; do not infer identity from a
   display symbol or assume every contract represents 100 ordinary shares.
   Acceptance: reject ambiguous/adjusted/unsupported contracts; golden SDK
   serialization tests and multiplier-aware reconciliation/accounting tests.

3. **Actual IBKR permissions, data, and funding.** Establish evidence for the
   chosen account's option permissions, current session, market-data entitlement,
   quote provenance/age, exchange increments, and account-effective fees.
   Collect account-wide positions, pending orders, reserved funds, settled cash,
   and other obligations; avoid double-counting reservations already included
   in a broker balance. Unknown settlement blocks sizing, not merely a warning.
   Acceptance: delayed/missing quotes, incomplete visibility, denied permission,
   stale balances, conflicting cash facts, and request-limit failures fail closed.

4. **Thesis and joint scenario analysis.** Define underlying-price/event
   invalidation and a latest intended exit before sizing. Compare strikes and
   expiries using scenarios that jointly move underlying price, remaining time,
   implied volatility, and liquidation spread/depth. Include overnight/weekend
   gaps, IV contraction, thesis success/failure, and no-move time decay.
   Acceptance: independently reviewed pricing/market inputs with uncertainty,
   conservative executable-bid assumptions, and no invented probability or
   expected-return precision. Greek estimates are not guaranteed exit quotes.

5. **Trade-specific loss budgets and portfolio capacity.** Show planned-exit
   loss, adverse-scenario loss, and full premium-plus-fee exposure separately,
   in dollars and as percentages of premium and current account equity. Apply
   explicit accepted budgets and aggregate correlated exposure; the calculator
   does not decide those budgets. A wider thesis-based exit can require fewer
   contracts or no feasible trade. Preserve fee minima and avoid counting spread
   or slippage twice. Acceptance: full-account allocation never implies borrowed
   funds, automatic all-in sizing, or a guarantee of loss limited to the stop.
   Version a distinct options policy; preserve historical risk approvals and
   incident history instead of deleting the legacy stock/session loss latch.

6. **Bound, attended review ticket.** Display exact contract, side, quantity,
   limit, time-in-force, expiry/overnight implications, all three loss numbers,
   accepted budgets, fees, data age, and account/exposure fingerprint. Bind the
   owner's approval to those facts and a short validity window. Reconcile and
   revalidate immediately before submission. Acceptance: material quote,
   quantity, budget, position, or order changes invalidate approval; no silent
   widening, averaging down, retries that duplicate orders, or policy revisions.

7. **Overnight lifecycle and cumulative P&L.** Persist original cost, fees,
   fills, thesis, accepted risk plan, and lifetime trade P&L across midnight and
   restarts. Separately show session P&L against its pre-trade baseline and
   daily marked P&L using documented carry-in marks and non-trading cash flows.
   Do not reset a losing position's history each morning. Acceptance: replay
   tests cover partial fills, late fees, multiple days, restart, expiry, and
   exercise-created exposure; an unfilled close never marks a position flat.

8. **Manual intervention and unknown activity.** Verify reporting-client scope
   and continuous account-wide capture, including activity from other clients.
   Reconcile owner orders/position changes before further new risk; preserve
   incidents and require a refreshed attended ticket. Acceptance: manual trades,
   unknown orders, disconnects, duplicate/late events, subscription rejection,
   and incomplete history cannot silently bypass exposure or freshness checks.

9. **Paper integration, fault testing, and review.** Wire the actual CLI/service
   composition, durable store, execution fingerprint, option broker adapter,
   reconciliation, and policy—not only pure helper functions. Acceptance:
   end-to-end paper tests demonstrate the attended lifecycle and loss arithmetic;
   fault injection covers stale data, failed exits, lost acknowledgements,
   restarts, gateway reconnects, and expiry boundaries. Paper fills alone do not
   establish live liquidity, permission, fee accuracy, or reliable protection.

10. **Paused deployment and explicit owner activation.** Produce a reviewed,
    reproducible release with policy-bound acceptance evidence, rollback plan,
    diagnostics, notifications, and a verified owner-intervention path. Install
    paused. Acceptance: release identity, runtime wiring, actual broker/data
    evidence, settlement-aware funds, and current readiness checks all agree.
    Only the owner performs live activation after reviewing the completed scope.
    This handoff does not authorize live orders, transfers, or exercise actions.

## Copy-paste Kiro task

Review the supplied pure calculator and its tests, then implement the remaining
milestones above in small reviewed changes. Preserve all existing stock-path rejection gates
and production settings until a distinct options path has acceptance evidence.
Do not treat feasible arithmetic, source configuration, a broker login, or an
SDK attestation as trade readiness. Keep owner-approved directions separate from
proposed restrictions and unresolved budget choices. Report changed files, tests
actually run, remaining wiring gaps, and whether any deployment state changed.
Never commit account identifiers, balances, statements, credentials, local
private paths, screenshots, or runtime evidence to this public repository.

## Run the first deliverable offline

From the repository root, using Python 3.11 or newer:

```sh
python3 -I -S -B scripts/titan-option-analysis examples/attended_option_analysis.json --now 2026-10-05T16:00:05+00:00
```

The fixture, prices, fees, contract ID and account values are entirely synthetic.
Expected one-contract results are planned loss 52, stressed loss 152 and premium
exposure 202; the maximum quantity under those supplied limits is 2. This is not
a contract recommendation. `--now` marks a simulation clock; it must not be used
to re-label old real-world facts as current broker data. Omitting it uses current
time, so the dated example correctly fails freshness checks outside its window.

The input fixture documents the JSON shape. Monetary values are decimal strings
or integers, while contract IDs, multipliers and quantities must be integers.
`pending_debits_not_in_balances` includes only independently reconciled reserves
and obligations not already deducted from the supplied balances. Never populate
missing balances or reserves with invented zeroes. This analyzer has no facility
to establish their truth; live use needs the evidence-bound adapter in milestone 3.

Fee inputs model one entry order and one exit order, with separate fixed and
per-contract components. They are supplied cost reserves, not verified broker
commissions; multi-order exits, partial-fill fee effects, routing/regulatory fees
and broker-specific caps require a suitably conservative supplied reserve or an
extended fee model. No minimum supplied budget is inferred from the fixture.

The report always has `analysis_only=true`, `live_authority=false` and
`executable=false`. `analysis_complete` means arithmetic completed, not risk or
order approval. `requested_quantity_fits_limits` assesses only the specified
quantity; the maximum does not imply that every smaller quantity fits when
fixed fees interact with profitable scenario assumptions. The 100,000-contract
search cap is an implementation bound, not a trading limit or recommended size.

## Primary references

- [OIC: volatility and the Greeks](https://www.optionseducation.org/advancedconcepts/volatility-the-greeks):
  option value responds to underlying, time, and volatility; estimates do not
  guarantee a future liquidation price.
- [IBKR: stop and stop-limit disclosures](https://www.interactivebrokers.com/en/trading/ordertypes.php?m=stopModal):
  a stop does not guarantee its execution price; a stop-limit can remain unfilled.
- [IBKR: exercise and delivery actions](https://www.interactivebrokers.com/en/trading/delivery-exercise-actions.php):
  expiry/exercise can create delivery exposure and broker intervention.
- [OCC: Characteristics and Risks of Standardized Options](https://www.theocc.com/getmedia/a151a9ae-d784-4a15-bdeb-23a029f50b70/riskstoc.pdf):
  long-option buyers can lose their entire premium; direction alone is not enough.
