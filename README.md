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

