# Trader Brain + Codex Operating Contract

This is the canonical bridge between the external daily-research chat and the Titan software stack. It is intentionally narrower than the full strategy narrative: it defines authority, evidence, change control, and the records the system must preserve.

## Roles

- Trader Brain/Sol is a daily market-research source focused on finding growth opportunities. It may supply theses, candidate rankings, catalysts, proposed entries/stops/targets, missed movers, and research observations.
- Trader Brain/Sol is not the portfolio manager, execution controller, risk authority, or production-rule authority. Its output is untrusted research input until independently verified.
- Codex is quant developer and trading-systems engineer. It builds data collection, scanners, replay, simulations, analytics, alerts, configuration, tests, and explicitly authorized broker infrastructure. The Titan controller independently applies the user's live operating rules.
- Massive is the primary structured market-data engine. Qualitative catalysts, filings, dilution, listing status, and manipulation risk still require independent primary-source verification.

Research may suggest what is worth testing. It cannot silently change what Titan believes, how much Titan risks, or what Titan trades.

## Shared loop

Observe → Hypothesize → Trade or simulate → Measure → Review → Learn → Code → Test → Repeat.

Before changing logic, Codex may read the latest research, cumulative rules, experiment register, and relevant ledger records. It must classify any proposed change as data collection, analytics, scanner logic, strategy logic, risk logic, or execution logic. Daily research is evidence, not authorization to implement a change.

## Change classes

1. Observation — interesting, insufficient evidence.
2. Hypothesis — defined and testable.
3. Experimental rule — paper/backtest/replay only.
4. Validated rule — supported by repeated evidence, still not live authority.
5. Production rule — separately approved for live use.

No daily observation becomes a live rule automatically. The required progression for material live changes is research → historical backtest → historical replay → paper trading → forward testing → human review → live trading.

## Calculated aggression

Risk management must measure both sides of confirmation:

- losses avoided by waiting;
- gains sacrificed by waiting;
- adverse excursion accepted by earlier entry;
- improvement or degradation in expected value after spread, slippage, and missed fills.

The system must never assume that “safer” means “wait longer.” It must also never reinterpret calculated aggression as permission to chase, average down, ignore liquidity, or bypass structural invalidation.

## Counterfactual entry protocol

For every research candidate, capture before the outcome is known:

- earliest reasonable entry and timestamp;
- conservative confirmation entry and timestamp;
- selected paper or actual entry and timestamp;
- structural stop/invalidation;
- proposed quantity and capital;
- setup, catalyst state, spread, volume, VWAP, and contemporaneous evidence.

Only after the observation window closes may the system attach session high/low, MFE, MAE, hypothetical lane results, hesitation cost, and confirmation savings. This separation is mandatory to prevent hindsight entries.

## Daily outputs

The research chat may provide a structured daily report containing candidate rankings, catalysts, proposed setups, missed opportunities, what worked, what failed, and new hypotheses.

When asked to ingest that research, Codex should respond with:

- what it learned;
- what it believes is being requested for testing;
- proposed implementation;
- affected files/modules;
- risks;
- whether backtest and paper testing are required;
- whether any production change is proposed.

## Safety boundary

The Massive watcher remains shadow-only and contains no broker client. Its score is market-data strength, not live trade authority. Research configuration is isolated from live trading rules. No research result, high-risk bucket, $500 paper-capital target, 4:00 a.m. collection start, or counterfactual entry may itself authorize a real order.

Live orders remain governed by the current Titan/Robinhood account, broker-review, catalyst, liquidity, Level 2, allocation, aggregate-risk, stop-protection, daily-loss, and no-overnight controls until an explicitly approved production change replaces them.
