# $500 pilot — non-executing validation package

## Actual scope

The owner has specified **$500** of potential Agentic-account test capital.
This branch implements validation of supplied observations, not discretionary
account management, an order-writing service, or a switch to live trading.
No deposit has been initiated or assumed, no order has been submitted, and the
running paper services, their data-provider allowlists, and ledgers are unchanged.

An attempted order-writing controller was blocked before it was added. It is
not present in this branch. The implemented alternative is deliberately a
non-executing validator; it must not be described as a completed autonomous
trading engine or an order submission adapter.

## Implemented

`src/titan_brain/pilot_validation.py` provides:

- Exact account and option-quote identity checks; no default/last-four-only
  account selection.
- Fresh account, quote and confirmed-signal checks, complete-pagination and
  cross-asset/cash-flow reconciliation flags, and unknown-delivery blocking.
- A fixed $500 capital ceiling, a draft $50 weekly loss ceiling derived from
  the earlier 10% paper cap, and a draft $15 planned-loss ceiling derived from
  3% of $500. These values do not rewrite the paper configuration.
- Full option-premium exposure plus a **modeled** $1.30 round-trip cost reserve.
  Planned stop loss is calculated separately and never substituted for the
  full-premium stress loss. Actual fees remain unverified.
- Open/pending exposure reservation and loss-adjusted weekly capacity. Profits
  and additional deposits cannot silently enlarge the initial ceilings.
- Cumulative broker-order evidence validation: acknowledgments are not fills;
  duplicate observations do not double-count fills; partial fills and pending
  cancellation retain risk; a confirmed cancellation may still leave a filled
  position that needs reconciliation; out-of-order fill regression and changed
  terminal/identity evidence are rejected.

Successful readiness status is **OBSERVATIONS_PASS_NOT_LIVE_READY**, and every
readiness report explicitly retains `live_submission_enabled=false`.

## Limits of this implementation

Observations are supplied by the caller. These functions do not fetch broker
snapshots or validate that a caller is an authenticated broker. A production
normalizer must bind the exact account, instrument and order identity, preserve
provider timestamps, complete all relevant pagination, and reconcile external
cash flows and positions. That normalizer is not supplied here.

The order-evidence helper supports partial-fill observations for multi-contract
historical orders. The pilot candidate validator itself permits **one contract**.
A cancellation observation can release the unfilled pending-order reservation;
any executed quantity remains position exposure. Consumers must never erase
that exposure because the remaining order was canceled.

No live dispatcher, broker write transport, automatic stop placement,
expiration liquidation service, recovery daemon, funded-account activation,
or production lifecycle integration has been added. No broker protection has
been demonstrated. A passing test does not establish trading readiness,
profitability, or a guaranteed maximum loss.

## Verification

Run offline, from this branch/worktree:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python3 -B -m unittest discover -s apps/control_center/tests -v
```

All new fixtures use a synthetic account and contract. Tests require no broker
credentials, no market data, no funded account, and no paid model calls.
Do not enable any existing live-trading setting to run these tests.

## Sources and integration constraints

Robinhood's official documentation permits authorized agent trading; the
brokerage capability is not the same as a verified implementation in this repo:
https://robinhood.com/us/en/support/articles/trading-with-your-agent/
https://robinhood.com/us/en/support/articles/agentic-trading-overview/

The connected Robinhood tool specifications were also inspected without calling
any order-writing tool. Their per-operation account eligibility, review alerts,
explicit cancellation confirmation, and idempotency requirements must be
preserved by any separately operated implementation. Coding does not remove
those requirements or the limitations of this assistant's role.
