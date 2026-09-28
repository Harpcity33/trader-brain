# Owner-approved daily starting-equity risk amendment

Status: **OWNER APPROVED FOR POLICY IMPLEMENTATION — NOT ACTIVATED**

Target: IBKR Pro No Borrow Margin account ending 3103.

## Owner instruction and precedence

The owner requested "Let's work towards a 15% increase daily and no more
than a 10% loss" and then explicitly clarified: "It should replace the previous goals and it
should be measured off the daily starting balance."

This amendment replaces the prior -$100 realized-loss entry lock, +$150
aspirational goal, and +$125 post-goal floor for this IBKR policy. Those dollar
values remain historical policy, not additional active restrictions in the
amended model. It also resolves the previously unspecified daily drawdown
basis to the fixed daily starting balance; it does not approve a separate
cumulative peak-drawdown rule or any 3/4/5/6/8/12/20-percent overlay.

The earlier approved operational proposal and approval record remain
immutable and binding except for the replaced daily risk terms:

- `PROPOSED_OWNER_POLICY_2026-09-14.md`, SHA-256
  `cc9de013e880864b8d7400837a71c8742b3116a2fe7269f252cfc88bd50617cf`.
- `OWNER_POLICY_APPROVAL_2026-09-14.md`, SHA-256
  `d27a1cc0c79629292f1353652440c984d3c7d510bcf3e08c4c595f25da7f4aae`.

## Exact executable interpretation

The policy model is `account_day_starting_equity_percentage`.

1. The denominator is positive, authenticated total account equity/net
   liquidation value effective at 00:00 America/New_York on the account
   trading date, authenticated as the beginning-of-day balance. It is fixed
   for that day. Cash, buying power, a subsequent balance, and a high-water peak
   cannot substitute for it. A restart or release change cannot reset it.
2. Adjusted daily performance equals current authenticated total account
   equity minus authenticated net external cash flows since that fixed start
   minus the starting equity. Deposits are positive cash flows; withdrawals
   are negative. Open P&L and fees already incurred are included through
   current total account equity. They must not be subtracted a second time.
3. The daily loss budget is `0.10 * daily_starting_equity`. At or below
   negative ten percent adjusted performance, irreversibly lock new entries
   for the account day and invoke the ordinary guarded closeout path. A later
   price recovery or cash movement cannot clear the account-day loss latch.
4. Before the loss boundary, all new, open, pending, uncovered, and unresolved
   downside plus positive execution reserves and fees still to be incurred
   must fit remaining headroom. The aggregate risk ceiling is the smaller of
   the original ten-percent daily loss budget and adjusted equity above
   ninety percent of starting equity. Profits do not enlarge that ceiling.
5. The daily profit aspiration is `0.15 * daily_starting_equity`, measured on
   the same adjusted equity basis. It is not guaranteed, a trade requirement,
   a minimum daily earning promise, a forced exit, or a ceiling on profits.
   There is no replacement post-goal floor: `post_goal_floor` is `null`.
6. Missing, stale, non-finite, wrong-account, wrong-day, regressing, conflicting,
   or unauthenticated starting-equity/cash-flow evidence blocks entries.
   The runtime may not initialize the baseline from an arbitrary first read,
   assume zero cash flows from absent evidence, or manufacture an upstream
   receipt. Daily observation and loss-latch persistence remain mandatory.

Ten percent is a risk-control boundary, not a guarantee that a market gap,
slippage, failed protection, fees, or an outage cannot produce a larger actual
loss. Closeout remains subject to exact broker evidence and existing safety
interlocks; no duplicate, overlapping, or unconfirmed exit is authorized.

## Restrictions and authority retained

All other approved account, instrument, cash/unleveraged-capacity, commission,
positive-reserve, entry, no-ADD/re-entry, session, completed-bar, spread/depth,
protection, cancellation, reconciliation, notification, and no-overnight
restrictions remain unchanged. Ranking stays descriptive; A+ uplift remains
disabled. Premarket remains analysis-only. Order-precaution bypass remains
prohibited.

This policy amendment authorizes implementation and verification of these
policy terms only. It does not authorize an order, change a Gateway/account
setting, install a credential, create a HMAC key, fabricate or issue provider
evidence, send a notification, start a service, retire a scheduler, or consume
an activation. Authentic provider, daily-baseline/cash-flow, effective-pricing,
route, release, writer, and activation evidence remain independent gates.
The new policy must be released and installed through the ordinary reviewed
PAUSED workflow; no existing installed state or authority is changed by this
document.
