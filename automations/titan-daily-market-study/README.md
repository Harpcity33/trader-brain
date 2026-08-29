# titan-daily-market-study automation

This is the repository-side definition for one standalone daily research run, not a trading heartbeat.

- Schedule: 04:00 America/New_York, Monday–Friday, with an authoritative trading-calendar guard.
- Deadline: the generated strategy must be ready by 06:30 America/New_York.
- Output: one create-only `research/daily-strategy/YYYY-MM-DD.md` artifact per valid trading day.
- Broker authority: none.
- Live equity dependency: none; failure cannot halt or alter `robinhood-momentum-engine`.

Deployed automation ID: `titan-daily-market-study-04-00-06-30-et`. It is
`ACTIVE` as a projectless local cron created through the Codex automation
interface. The first scheduled trading-day run is the live provider/access
validation; any provider failure remains isolated and must be reported without
touching the equity heartbeat.

The weekday recurrence does not assert that every weekday is tradable. The prompt and research engine both require authoritative holiday and unexpected-closure evidence before doing trading-day work.
