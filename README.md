# Titan Momentum Live Upgrade v1

Additive deterministic risk, daily research, instrument routing, R-based
metrics, and a separate live-attended options lane for Trader Brain.

The established `robinhood-momentum-engine` remains the production equity core.
The new code contains no broker-write client. Options mutations remain behind
Robinhood's exact attended review and explicit confirmation; daily research and
the aggressive lab have zero broker authority.

## Components

- `config/`: central risk, scoring, setup, research, options, and aggressive-lab
  policy.
- `src/titan_brain/`: pure eligibility, risk, scoring, routing, options review
  binding, research artifacts, ledgers, learning, and metrics.
- `automations/`: independent definitions for the daily study and attended
  options lane plus the additive equity risk overlay.
- `ledgers/`: isolated live-equity, live-options, and paper-aggressive stores.
- `research/`, `predictions/`, `reviews/`, `knowledge/`: immutable evidence and
  explicit observation/promotion boundaries.
- `validation/`: eligibility snapshot, failure contract, test evidence, and
  rollback.

Run locally with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

No profit is promised or guaranteed. Optimization targets positive net
expectancy in R with survivable drawdown and disciplined execution.


## Paper runtime data providers

The Mac paper runtime uses Massive for stocks (bars, movers, and market calendar)
and Robinhood for options chains, contracts, quotes, and Greeks. It requires no
Massive options entitlement. Broker-write authority must remain false.

The official Codex CLI at `~/.local/bin/codex` supplies the authenticated MCP
transport for `https://agent.robinhood.com/mcp/trading`; Codex stores and refreshes
OAuth credentials. There are no model turns or paid OpenAI API calls. The adapter
permits only `get_option_chains`, `get_option_instruments`, and `get_option_quotes`.
The app-server interface is experimental: connection or protocol failures block
paper entries rather than falling back to another options provider.

Options screening is bounded: three strikes near the signal's underlying price
across available expirations in the configured 7–21-day window, then at most 20
contracts per direction. Strike spacing is $5 above $200, $1 from $25, and $0.50
below $25. This can miss suitable contracts; it does not claim complete chain
coverage. Candidates with no returned quote are excluded. Exact selected contracts are rechecked, and the existing 30-second
quote-age, depth, multiplier, risk, and liquidity gates still apply. Existing
OCC identifiers in the private paper ledger remain usable across providers.

Use `python3 scripts/paper_options_runtime.py check` for configuration and
`python3 scripts/paper_options_runtime.py doctor` for stock, options, and Gmail
access. The doctor does not send email unless explicitly run as `doctor-email`.
A successful weekend access check does not establish market-open quote freshness.
Install/restart with `deploy/install_paper_options_macos.sh`, which validates all
connections first and preserves the contents of the local secrets file.
