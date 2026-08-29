# +$150 NET ATTACK OBJECTIVE / -$100 ACCOUNT-DAY LOSS LOCK — LATEST USER AUTHORIZATION

This amendment supersedes every earlier conflicting daily profit objective, stretch designation, percentage-based daily soft or hard loss threshold, post-profit score threshold, profit-floor posture, and statement that an earlier daily-loss rule remains controlling. It does not relax any instrument, eligibility, liquidity, Level 2, structural-trigger, chase, exhaustion, account, broker-review, protection, aggregate-risk, correlated-risk, or no-overnight requirement. Where this amendment is silent, the SOL Ultra Massive live-autonomous policy and all non-conflicting safeguards remain controlling.

## Golden rule and accounting

- The primary daily attack objective is +$150 net. It is an aggressive objective and operating priority, not a promise, forecast, guaranteed minimum, mandatory quota, or instruction to manufacture a trade. The controller must genuinely seek qualified profit every trading day and must not skip an otherwise qualified setup from generalized conservatism, but remaining flat is required when no contemporaneous setup passes every controlling execution and risk gate.
- Establish `START_OF_DAY_EQUITY` once from the first fresh, broker-confirmed total-account-equity snapshot obtained before that session's first fill. Never reset or improve this baseline intraday. If a reliable baseline or current equity cannot be read, suspend new entries.
- Compute `ACCOUNT_DAY_PNL = current broker-confirmed total account equity - START_OF_DAY_EQUITY`, adjusted only for a broker-confirmed non-trading cash flow. Track broker-confirmed realized net P&L separately. For loss control use `LOSS_GAUGE = min(ACCOUNT_DAY_PNL, realized net P&L)` so unrealized gains cannot conceal realized losses and unrealized losses cannot be ignored.
- Success against the +$150 objective is reported from broker-confirmed end-of-day net P&L. Open profit is not secured profit. Reaching +$150 is not by itself a take-profit signal, an automatic runner exit, or permission to weaken risk controls.

## Hard -$100 account-day lock

- The hard loss lock triggers immediately and irreversibly for the session when `LOSS_GAUGE <= -$100`. This fixed-dollar rule replaces all earlier percentage daily-loss cutoffs.
- At the lock, cancel and reconcile every unfilled entry, prohibit all new entries, adds, and re-entries, and promptly close remaining exposure through broker-reviewed, quantity-reconciled safe exits. Keep valid protection active until the replacement exit or close is broker-confirmed. Never submit overlapping full-quantity exits or infer a cancellation, fill, or flat position.
- Before every entry or add, cap total stop-defined open risk plus a conservative slippage reserve at the lesser of all existing aggregate/correlated/lane limits and the remaining loss headroom to -$100. Do not knowingly submit an order whose planned downside could cross the lock.
- The loss lock cannot be reset or overridden by a rebound, profit objective, setup score, Massive event, catalyst, opening-session urgency, later-exit rule, or willingness to accept more loss. Stops and limit prices cannot guarantee execution, so gaps, halts, and slippage may cause the final loss to exceed $100 despite compliant planning.
- Use -$75 on `LOSS_GAUGE` as a nonterminal warning and recalibration point. It is not permission to stop searching for qualified opportunities, but any resumed entry must be a fresh independent setup whose conservative planned downside fits the remaining headroom. Existing loss-streak pauses remain controlling.

## Opening-session attack posture

- Treat 9:30-10:30 AM ET as the primary opening attack window. Begin full live discovery and plan maintenance at 9:30, but retain the controlling submission windows: regular-session equities no earlier than 9:35 and options no earlier than 9:45.
- On every opening-window wake, perform the broker account/order/position/protection audit first, process fresh high-priority Massive events, and actively evaluate the top three executable opportunity-quality leaders plus the strongest independently qualified downside long-put candidate.
- Maintain exact contemporaneous plans for the best one or two independent candidates. When every controlling gate passes and the exact live trigger occurs, complete Robinhood review and submit promptly under standing authority. Use the authorized size envelope when structure, liquidity, depth, protection, and remaining loss headroom support it; do not default to token size.
- Active participation means mandatory search, preparation, and prompt execution of qualified opportunities—not a mandatory order count. Never chase, invent evidence, lower a hard gate, or trade merely because the +$150 objective has not yet been reached.

## Profit protection

- Do not stop merely because broker-confirmed account-day P&L first reaches +$100. Continue to seek the +$150 objective only through independently qualified opportunities and dominant existing runners.
- At +$125, attempt to protect a +$100 account-day floor. At +$150, attempt to protect a +$125 account-day floor, normally stop initiating new positions, and manage only dominant existing runners. These floors are management objectives rather than guaranteed fills; gaps and slippage can breach them.

## Massive, Robinhood, and reconciliation boundary

- Massive remains the market-data, research, ranking, and event source only. A Massive event is never an order or fill. Robinhood Agentic account ending 7153 remains the sole source of account state, buying power, review results, orders, fills, positions, and protection state.
- Every order still requires the applicable Robinhood pre-trade review, broker-confirmed submission state, final-state reconciliation, and prompt full-quantity protection. Never infer a fill, cancellation, stop, or flat account from Massive or local runtime state.

## Evidence gate for later exits

- This amendment does not yet authorize delaying an exit merely because the +$150 objective is unmet. Existing catastrophe stops, structural invalidations, protection rules, and current profit-management logic remain controlling until a separately named and versioned later-exit amendment is promoted.
- Promotion requires a causal study of 100% of usable supplied one-minute candle data from market open through close, with every excluded row accounted for, chronological out-of-sample validation, transaction-cost and slippage sensitivity, gap/halt tails, MFE, MAE, exit efficiency, peak-profit giveback, and drawdown reported.
- A future validated amendment may supersede only the explicitly identified discretionary trim, runner-trail, acceleration-decay, and peak-giveback clauses. It may never widen the original catastrophe/thesis stop, leave a position unprotected, override the -$100 lock, weaken reconciliation, or authorize an overnight hold.
