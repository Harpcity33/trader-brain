# titan-daily-market-study automation

This is the repository-side definition for one standalone daily research run, not a trading heartbeat.

- Schedule: 04:00 America/New_York, Monday–Friday, with an authoritative trading-calendar guard.
- Deadline: the generated strategy must be ready by 06:30 America/New_York.
- Output: one create-only `research/daily-strategy/YYYY-MM-DD.md` artifact per valid trading day.
- Broker authority: none.
- Live equity dependency: none; failure cannot halt or alter `robinhood-momentum-engine`.

The checked-in definition remains `PAUSED` until deployment resolves the saved Codex project, validates the connected Massive and GitHub access in a manual run, and creates the standalone cron through the Codex automation interface. The deployment must not modify the existing equity heartbeat or its target task.

The weekday recurrence does not assert that every weekday is tradable. The prompt and research engine both require authoritative holiday and unexpected-closure evidence before doing trading-day work.
