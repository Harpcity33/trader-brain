# Titan Continuous Market-Intelligence Runtime

This workspace now contains a dependency-free Massive market-data watcher for the Titan Momentum Engine. It continuously observes the U.S. equity market, persists decision-time evidence, and emits a small durable event queue for the scheduled Codex/Robinhood controller.

The runtime is **shadow-only**. It has no brokerage client, contains no order-submission code, and cannot place or modify trades. A market event is never trade authority. The live controller must independently verify Robinhood account state, catalyst, Level 2 depth, risk, score, broker warnings, and protective-order feasibility.

## What it watches

- `AM.*`: market-wide completed one-minute stock aggregates.
- `LULD.*`: market-wide limit-up/limit-down, halt, and resumption events.
- `A.<symbol>`: second aggregates for the strongest current candidates.
- `Q.<symbol>`: NBBO quotes for the strongest current candidates.
- Full-market REST snapshots every five minutes for previous-close baselines, accumulated volume, current quotes/trades, and prior-day volume.

Massive documents the production real-time endpoint as `wss://socket.massive.com/stocks`, with authentication followed by channel subscriptions. It permits one concurrent WebSocket connection per asset class by default, so Titan multiplexes all stock channels over one connection. See the [Massive WebSocket quickstart](https://massive.com/docs/websocket/quickstart) and [full-market snapshot reference](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot).

## Security boundary

The API key is loaded in this order:

1. `MASSIVE_API_KEY` environment variable, when explicitly provided.
2. macOS Keychain service `titan-massive-api`.

The key is never written to SQLite, JSONL, logs, configuration, prompts, or test output.

To add or replace the Keychain entry in a normal macOS Terminal:

```zsh
security add-generic-password -U -a "$USER" -s titan-massive-api -w
```

Enter the key privately when prompted. Do not paste it into Codex or any prompt.

## First-run verification

Run these commands from this workspace:

```zsh
scripts/titan-massive init
scripts/titan-massive doctor
scripts/titan-massive snapshot
scripts/titan-massive run --ignore-schedule --seconds 30
scripts/titan-massive status
```

The 30-second run tests TLS, WebSocket authentication, subscription acceptance, REST access, SQLite writes, and event handling. Running after market hours may produce few or no aggregate events, but authentication should still succeed.

## Automatic weekday startup

After the manual connectivity test succeeds:

```zsh
scripts/install-launch-agent
```

The LaunchAgent starts at 3:55 a.m. ET Monday through Friday so the database contains the full 4:00 a.m. premarket session. The runtime exits after 4:05 p.m. ET. It reconnects with exponential backoff after transient failures and uses an exclusive process lock to prevent overlapping watchers. The installer deploys a runtime-only copy to `~/Library/Application Support/Titan Momentum` because macOS may deny background LaunchAgents access to code stored in `~/Documents`.

To inspect it later:

```zsh
launchctl print "gui/$UID/com.titan.momentum-watcher"
scripts/titan-massive status
tail -f "$HOME/Library/Application Support/Titan Momentum/runtime/titan-massive.log"
```

## Event queue contract

Read pending events:

```zsh
scripts/titan-massive events --limit 20
```

After the Codex controller has fully processed an event:

```zsh
scripts/titan-massive events --ack EVENT_ID \
  --decision WATCH \
  --reason "Immediate entry rejected; retain for a fresh controlled base."
scripts/titan-massive decisions list --limit 20
```

The acknowledgement command persists the decision and reason first. This lets
the EOD review reconstruct what Titan knew and decided without hindsight.

Priority order:

1. `HALT`, `DATA_STALE`, `DATA_CONNECTION_LOST`, `DATA_ENTITLEMENT_MISSING`
2. `TRIGGER_CROSS`
3. `BASE_READY`
4. `LEADER_CANDIDATE`
5. `MOMENTUM_WATCH`

`signal_strength` is deliberately a market-data ranking score—not Titan's Acceleration Score. It does not include a verified catalyst, filings/dilution review, Level 2 depth, 90-day analogs, sector breadth, broker state, or position risk.

Every candidate and base event includes `session_phase`, `session_lane_eligible`,
`session_blockers`, and `next_eligible_window`. These fields are schedule/lane
annotations only; they never satisfy the remaining Titan gates. In particular,
an under-$5 premarket observation is retained for regular-session preparation but
is explicitly marked ineligible and receives reduced event priority. When an
existing candidate crosses into an authorized equity-entry window, the watcher
emits a fresh `LEADER_CANDIDATE` so the controller can require a new
regular-session structure instead of losing the name in an old premarket queue.

A `TRIGGER_CROSS` now requires a fresh NBBO ask at or above the trigger and a
spread inside the configured lane threshold. A one-second aggregate whose high
merely touched the level cannot create a cross while the live ask remains below it.

`MOMENTUM_WATCH` separates “do not enter now” from “discard the symbol.” A
three-expansion-bar move more than four short ATR from VWAP is rejected for an
immediate chase but remains subscribed when liquidity and market-data strength
are still adequate. It can return to `LEADER_CANDIDATE` only after a fresh valid
structure. Downside movers above $5 are surfaced on `DOWNSIDE_LONG_PUT`; that is
discovery for a separately qualified single-leg long put, never short-stock or
spread authority.

`DATA_ENTITLEMENT_MISSING` means the API key authenticated but Massive rejected the real-time channels. Confirm that the account has Stocks Advanced and complete the exchange agreements in the Massive dashboard. Titan deliberately does not fall back to the 15-minute delayed socket for live decisions.

## Daily research bridge

The external chat is a daily market-research source only. Its Top Five, trade-of-day, asymmetric candidate, entry ideas, and lessons are evidence for Titan to verify—not trade authority and not permission to change live rules.

Ingest a Markdown or structured JSON daily report:

```zsh
scripts/titan-massive lessons ingest PATH_TO_REPORT --date YYYY-MM-DD
scripts/titan-massive lessons latest --limit 3
```

Record a testable observation or hypothesis from JSON:

```zsh
scripts/titan-massive changes propose examples/strategy-hypothesis.json
scripts/titan-massive changes list --limit 20
```

Production rules are deliberately rejected by this research-ingestion path, even if an input claims approval.

To measure confirmation instead of assuming it is always helpful, freeze entry alternatives before the outcome is known and attach the outcome later:

```zsh
scripts/titan-massive research plan --file examples/entry-plan.json
scripts/titan-massive research outcome --plan-id PLAN_ID --file examples/entry-outcome.json
scripts/titan-massive research report --limit 20
```

The plan/outcome split prevents hindsight entry selection. Research configuration lives in `config/titan-research.json`; it cannot mutate production trading rules.

## Files

- `~/Library/Application Support/Titan Momentum/runtime/titan-intelligence.sqlite3`: persistent state and event queue.
- `~/Library/Application Support/Titan Momentum/runtime/events.jsonl`: append-only event export.
- `~/Library/Application Support/Titan Momentum/runtime/titan-massive.log`: rotating runtime log.
- `config/titan-massive.json`: non-secret thresholds and schedule.
- `config/titan-research.json`: paper/research hypotheses, comparison lanes, and experimental weights.
- `outputs/titan-continuous-runtime-addendum.md`: copy-paste instructions for the existing Titan automation.
- `outputs/trader-brain-codex-operating-contract.md`: authority and change-control boundary for daily research.

## Validation

The automated tests use only synthetic data and a temporary database:

```zsh
/Users/harp/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m unittest discover -s tests -v
```
