# Robinhood Options Momentum Engine

`titan_options_live_attended_v1` is an additive, single-leg options lane. It is
separate from `robinhood-momentum-engine`; neither its discovery failures nor
its broker-data failures may change the equity engine's state.

## Initial production authority

- Long calls and long puts only.
- LIMIT orders in `regular_hours` only.
- One contract is the normal maximum.
- Exact, immediately preceding, unexpired Robinhood review and the connector's
  exact attended confirmation phrase are required for every mutation.
- No unattended placement, cancellation, replacement, exit, exercise, or
  assignment authority.
- Debit spreads remain disabled until a fresh broker response positively shows
  `option_level_3`, an eligible margin/limited-margin account, and live connector
  support for the exact multi-leg order.

The repository definition is not itself an order authorization. Installation
must preserve the existing equity heartbeat and set the scheduler's target task
explicitly. On startup the heartbeat re-fetches broker eligibility and fails the
options lane closed if any required fact is missing.

Deployment status: `READY_NOT_INSTALLED`. Codex allows only one active heartbeat
per task, and the current task is correctly occupied by the production equity
heartbeat. The options engine therefore remains paused in source until the user
authorizes a dedicated options task. It was not converted to a cron workaround,
and the equity heartbeat was not modified.

## Current eligibility snapshot

The read-only validation captured in
`validation/options-eligibility.md` supports single-leg long calls/puts for the
account ending 7153. It does not support debit spreads. Eligibility is dynamic;
the heartbeat must re-check it before each review.

## Failure isolation

Chain, quote, eligibility, review, and research errors produce `OPTIONS NO
TRADE`. They do not pause, rewrite, or replace the production equity automation.
