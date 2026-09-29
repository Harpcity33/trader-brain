# Trader Brain Control v1 — no-additional-spend build

## Scope and honest release status

This release is an independent Python service with an iPhone-sized, Home Screen-capable web dashboard. It is not an App Store binary. It reuses the repository's existing 5-minute/15-minute paper strategy but runs it against a **separate** `control-center/portfolio.json` ledger. It does not overwrite, migrate, reset, pause or replace the baseline paper runtime or its recorded results. Do not compare the separate account's initial $1,000 against today's baseline results as if they were the same ledger.

Default new entries are paused. Resume explicitly in the dashboard only after local data readiness has been checked. Pausing entries preserves exit management. Closing paper positions is a separate confirmed request, not an immediate-fill promise. Runtime commands apply on the worker; a network operation already in progress can delay them. Liquidation retries occur on subsequent heartbeat cycles, using fresh quotes, not stale marks.

This release does **not** activate real-money trading. It does not bypass platform confirmations, broker approval, exchange entitlements or risk rules. No live broker-write transport is included. There is no autonomous strategy-rewriting or self-promotion mechanism.

## Components

- `apps/control_center/transport.py`: bounded HTTPS and Streamable HTTP MCP; exactly three permitted Robinhood tools (`get_option_chains`, `get_option_instruments`, `get_option_quotes`). Every other tool is blocked before authentication or HTTP.
- `auth.py`: app-specific local OAuth discovery, authorization-code PKCE S256, browser callback, refresh and private token storage. The app enforces read-only tool use; it does not claim that Robinhood granted a separately read-only OAuth scope. It never reads Codex auth files, browser cookies or ChatGPT connector tokens. Robinhood must advertise compatible discovery/public-client registration. An unsupported login flow stops with an explicit diagnostic, never a credential workaround.
- `providers.py`: Massive stocks/calendar plus Robinhood option metadata, quotes, Greeks, activity and source timestamps. No paid Massive options fallback. Source times remain unchanged and baseline freshness gates remain in force. Candidate discovery samples the first two eligible expirations and up to 100 contracts nearest the prior stock close; it is not an exhaustive option-chain ranking.
- `bridge.py`: independent worker around the existing tested strategy. No LLM, chat timer or Codex executable is required. Target heartbeat is 120 seconds, not an operating-system scheduling guarantee. The Mac must stay awake and logged in.
- `store.py`: SQLite commands, idempotency, control state and bounded event history. Existing strategy file-locking and private atomic portfolio persistence are preserved.
- `server.py`: private dashboard, long random pairing token, HttpOnly/SameSite sessions, CSRF, origin and Host validation, body bounds, no account-data caching. Plain HTTP is restricted to loopback. Binding to a LAN address requires TLS.
- `alpaca.py`: optional **paper-only transport benchmark**, not the selected engine broker. It uses only `https://paper-api.alpaca.markets`, verifies an explicitly configured paper account, reserves exposure before sends and reconciles ambiguous sends using client order IDs. It does not buy data, request an OPRA upgrade, or treat indicative quotes as executable. Filled exposure remains conservatively reserved; an unattended round-trip Alpaca strategy and reconciliation of its closing fills are **not implemented in this release**. Do not activate it as a live-execution replacement.
- `web/`: responsive mobile dashboard and offline application shell. API responses, credentials and trading commands never enter the service-worker cache. Disconnected screens are explicitly marked stale and controls disabled.

## Costs and authority

No new subscriptions, cloud servers, paid AI calls, domains, App Store fees or exchange packages are created. Existing Massive and Gmail settings are reused locally. New resources are files and processes on the owner's existing computer. Hardware, electricity, internet and existing subscriptions are not claimed to be free.

The optional Alpaca transport is unconfigured by default. Its credentials, funding and entitlements have not been inspected by this release. Alpaca Basic's indicative feed is not actual OPRA bid/ask and is never an execution substitute here.

## Local installation

Run from a checked-out, tested revision on the actual Mac:

```sh
zsh deploy/install_control_center_macos.sh
```

This installs the separate `com.harpcity.traderbrain.control` LaunchAgent at loopback port 8765. It preserves `~/.config/trader-brain/paper-options.env` and does not edit the old LaunchAgent. Re-running the installer resets the new service's network binding to loopback intentionally; custom TLS deployments require their own reviewed service arguments.

The installer validates Python 3.11+, runs offline tests and records the requested service state. **No code in this chat has remotely installed it on the owner's Mac.** A running PID alone does not prove market-data readiness.

Configure Robinhood from the local project directory with the owner present:

```sh
python3 -m apps.control_center login-robinhood
python3 -m apps.control_center doctor
```

The browser displays Robinhood's actual consent screen. Credentials are written only to `~/.local/state/trader-brain/control-center/robinhood-oauth.json` with owner-only permissions. Failure codes such as `OAUTH_CLIENT_REGISTRATION_REQUIRED` mean a compatible public OAuth client must be supplied using the provider's supported process. Do not copy ChatGPT tokens or use a password scraper.

The server generates a private `control-center/dashboard-token`. Open it locally, copy only into the dashboard's pairing form, and close it. Never paste it into chat or commit it. Broker keys and OAuth refresh tokens must never enter the dashboard. Initial entries stay paused until the owner chooses **Resume paper**. That switch cannot override the strategy's weekly loss lock, stale-data rejection or closed-session gates.

## iPhone access

The phone is a control surface, not the trading host. Serve the app over an existing private HTTPS endpoint with a certificate the phone trusts. Start the service with explicit `--host`, `--origin`, `--cert` and `--key`; the default loopback address is deliberately **not** reachable from an iPhone. No router port is opened, tunnel provisioned or TLS trust changed automatically. A certificate, trusted private route and on-device connection remain deployment prerequisites—not completed by repository tests.

After private HTTPS is operational, open the dashboard in Safari and use **Add to Home Screen**. The service must continue working when Safari/the phone is closed. No native push entitlement is assumed; the existing server Gmail notifications remain the delivery route. No push or email test is performed by automated tests.

## Verification and cutover gates

1. All legacy tests and new control/transport/UI tests pass on the exact revision.
2. The actual host log contains `CONTROL_LISTENING`, a PID and control version. The dashboard shows a recent completed heartbeat.
3. Local Robinhood OAuth succeeds; exact-contract quote timestamps/Greek fields are returned during market hours; stale/unknown data cannot create a paper entry.
4. Pause, resume and explicit paper-close requests appear in the private command audit. A close remains pending when the market is closed or quotes are stale.
5. Gmail delivery is tested on the actual host; delivery failures preserve the portfolio and risk reservations.
6. Private HTTPS and Home Screen launch are verified on the actual iPhone, including disconnect/reconnect. No public unauthenticated interface.
7. Only after comparison with the preserved baseline should any cutover be proposed. Live-money promotion is a separate project and authorization.

## Research limits preserved

The existing deterministic premarket screen, not an hour-long AI research process, is displayed. Lane summaries report observed paper outcomes; quality grades remain unassessed without a validated rubric. Exhaustive missed-opportunity replay, catalyst research, tick-level MAE/MFE, subscription-cost allocation, statistical strategy advantage, event-driven streaming execution and a native App Store package are not completed by this code. A passing engineering test is not evidence of profitability.

## Primary references checked for this build

- Robinhood agent connection and MCP endpoint: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Model Context Protocol Streamable HTTP: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- Model Context Protocol authorization: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- Alpaca paper authentication: https://docs.alpaca.markets/us/v1.1/docs/authentication-1
- Alpaca options support: https://docs.alpaca.markets/us/docs/options-trading
- Alpaca quote feeds: https://docs.alpaca.markets/us/reference/optionlatestquotes
- Apple background timing limitations: https://developer.apple.com/documentation/backgroundtasks/bgtaskrequest/earliestbegindate
- GitHub standard public-repository runner cost: https://docs.github.com/en/billing/concepts/product-billing/github-actions
