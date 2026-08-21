# Risk Rules

## Universal

- Define entry condition, invalidation, stop, target logic, and maximum holding premise before entry.
- Never average down unless a separately documented experiment explicitly permits it.
- Do not trade through a known halt, broken data feed, or unbounded spread.
- Stop trading when data integrity or ledger capture fails.

## Paper Trader

Paper risk exists to produce useful evidence, not fantasy fills. Use observable bid/ask conditions, include slippage assumptions, and reject impossible fills. Daily and per-trade paper limits must be written in the daily ledger before the session.

## Live Codex Trader

Live execution requires explicit user authorization and configured broker controls. Before any order:
- Confirm symbol, side, quantity, order type, limit/stop, and time in force.
- Check maximum position size, maximum risk per trade, daily loss limit, open exposure, liquidity, and duplicate orders.
- Require a human approval checkpoint unless a separately authorized automation defines exact limits.
- On daily loss-limit breach, cancel pending entry orders and permit only risk-reducing actions.

Research confidence does not increase an established risk ceiling.
