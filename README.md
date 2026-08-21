# Trader Brain

**Chat to Codex, Codex to Chat.**

Trader Brain is a version-controlled research and trading journal for Massive-driven premarket discovery, ranked daily predictions, and separately governed paper and live Codex experiments.

## Daily operating loop

1. Run the 03:55 ET research prompt using timestamped Massive market data and public context.
2. Apply the hard eligibility gates: **price > $5** and **daily volume >= 750,000 shares**.
3. Score eligible candidates from **0–100** using reproducible component scores.
4. Freeze exactly five ranked predictions before the opening bell.
5. Record paper and live decisions in separate ledgers.
6. Complete the end-of-day review; capture misses, rule deviations, and lessons.
7. Promote only repeatable edges backed by linked evidence.

## Repository map

- `BRAIN.md` — shared doctrine and daily workflow
- `config/` — screener, scoring, risk, and experiment rules
- `prompts/` — deep research, Top 5, live trader, and review prompts
- `templates/` — prediction, trade, and review formats
- `knowledge/` — market patterns, lessons, and promoted edges
- `predictions/daily/` — immutable morning Top 5 records
- `paper/` — paper experiment trades and ledger
- `live/` — live experiment trades and ledger
- `reviews/daily/` — end-of-day reviews

## Start here

1. Read `BRAIN.md` and all files in `config/`.
2. Run `prompts/0355-deep-research.md`.
3. Save the Top 5 using `templates/daily-predictions.md`.
4. During the session, keep paper and live activity separate.
5. Run `prompts/end-of-day-review.md` and link all evidence.

## Guardrails

This repository is a decision journal, not a promise of returns or personalized financial advice. Live execution always requires the controls in `config/risk-rules.md`. A research score never overrides position, loss, liquidity, authorization, or stop limits.

## Record integrity

Use `YYYY-MM-DD.md` for daily records and `YYYY-MM.csv` for monthly ledgers. Never revise a premarket prediction after the opening bell; append timestamped corrections. Never commit API keys, brokerage credentials, account numbers, or personal secrets.
