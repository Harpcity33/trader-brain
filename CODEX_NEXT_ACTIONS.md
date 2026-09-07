# Current priority — Full-Live Autonomy for September 8, 2026

**Read [CODEX_FULL_LIVE_AUTONOMY_2026-09-08.md](CODEX_FULL_LIVE_AUTONOMY_2026-09-08.md) before beginning the next implementation task.**

Shian's latest direction is full-live autonomous operation, faster execution, and confirmed activity/exception updates instead of routine manual execution, using the existing stack with **zero incremental paid service spending**. The requested operating date is September 8, 2026, America/New_York.

The linked brief supersedes the earlier conversation attachment's tiny pilot-only scope, unapproved one-share/two-attempt/dollar caps, and arbitrary minimum paper-session waiting period. It does **not** supersede existing owner-approved risk limits, strategy/account scope, mandatory broker/platform controls, or protection/reconciliation requirements. Full-live describes the intended operating mode, not unlimited capital or proof that deployment has occurred.

Implement tested executable production components and provide a user-controlled activation/cutover path. Do not stop at another planning document. Reuse verified existing authorization instead of asking for known information; explicitly identify only genuinely missing permissions or policy fields. No live orders, transfers, account changes, or live scheduler activation are authorized as coding smoke tests. This instruction update does not itself enable trading.

Prioritize the protection, overnight-control, and revoked-token failures recorded in `reviews/weekly/2026-W36.md`. Preserve one account-level execution owner, durable unknown-order reconciliation, actual working protection, existing risk policy, and accurate closeout/notification state. Do not infer that an ACTIVE configuration label means a healthy live service exists.

The system-validation requirements below remain applicable wherever they do not conflict with the new implementation priority. Preserve their evidence, research/live separation, risk, and validation controls. Report actual implementation/deployment status and evidence under the paths specified in the full-live brief.

---

# Codex Next Actions: Trader Brain System Validation

## Operating principle

GitHub is the source of truth. Before every session, read the latest repository brain files, configuration, ledgers, prior reviews, and promoted edges. Do not rely on chat memory when repository state is available. After every session, write back the results, raw observations, decisions, lessons, and any validated rule changes. Preserve full version history and commit meaningful changes with descriptive messages.

## Objective

Validate the complete Trader Brain daily workflow end to end. Codex must leave reproducible evidence in the repository, not only report conclusions in chat. If an expected component does not exist, document the gap and implement or propose the smallest testable component needed to complete validation.

## Required workflow

### 1. Run the 3:55 a.m. premarket research pipeline

- Schedule or invoke the premarket research run at **3:55 a.m. America/New_York** on trading days.
- Use **Massive** as the primary market-data source.
- Build and evaluate the **full eligible ticker universe**. Do not substitute a hand-picked watchlist unless testing a clearly labeled dry run.
- Record the run timestamp, data timestamp, universe size, symbols accepted/rejected, exclusion reason for every rejected symbol, missing-data conditions, API failures/retries, and runtime.
- Prevent look-ahead bias: a premarket output may use only information available at its recorded decision time.
- If Massive is unavailable or incomplete, fail visibly or mark affected fields unavailable. Do not silently fabricate, forward-fill, or infer required observations.

### 2. Enforce the screener baseline

A ticker is eligible only when both conditions are satisfied at the documented screening timestamp:

- Price **> $5.00** (strictly greater than $5.00).
- Volume **>= 750,000** shares.

Fresh news is **not required**. News or another catalyst can improve context or confidence, but lack of fresh news must never reject an otherwise eligible ticker.

Keep these baseline filters explicit, centrally configured, and covered by boundary tests for exactly $5.00, just above $5.00, 749,999 shares, and 750,000 shares. Document exactly which price and volume fields/windows are used.

### 3. Calculate a reproducible 0–100 confidence score

Score every eligible ticker from **0 through 100** using documented, inspectable inputs. The score must include:

- Liquidity.
- Relative volume.
- Price action and technical structure, including VWAP relationship/behavior.
- Catalyst and broader context; neutral treatment is allowed when fresh news is absent.
- Sector and market sympathy.
- The ticker's prior 90-day move behavior.
- Historical and current gap behavior.
- Other relevant data available from Massive.

Define weights, normalization, caps, missing-data handling, and tie-breaking in version-controlled configuration or documentation. Ensure the components sum to 100 and save both the total and component-level score explanation for every candidate. Do not let missing news impose a hidden hard filter. Backtest or replay representative days to test calibration and guard against leakage.

A starting weighting may be used only if the repository has no established scoring model:

| Component | Points |
| --- | ---: |
| Liquidity | 15 |
| Relative volume | 15 |
| Price action / technical structure, including VWAP | 20 |
| Catalyst / context | 15 |
| Sector / market sympathy | 10 |
| Prior 90-day move behavior | 15 |
| Gap behavior | 5 |
| Other available Massive data | 5 |
| **Total** | **100** |

Treat this as an initial hypothesis, not a promoted rule. Preserve an existing documented model unless evidence supports a versioned change.

### 4. Maintain two separate experiments

Operate and evaluate these as independent strategies:

1. **Aggressive paper trader** — broader risk tolerance for experimentation and learning; paper execution only.
2. **Stricter live trader** — higher qualification and risk standards; never inherit aggressive paper rules automatically.

Use separate configuration, state, ledgers, performance summaries, risk limits, and experiment identifiers. A paper result must never be recorded as live, and paper eligibility must never authorize a live order. Do not place live orders unless live execution is already explicitly enabled and authorized by the repository's controls. Otherwise, produce live candidates/signals only.

### 5. Publish the daily premarket Top 5

Before the market session, save a ranked Top 5 prediction list derived from that day's eligible universe. Each selection must include:

- Rank, ticker, timestamp, and confidence score.
- Component score breakdown and data provenance.
- Concise thesis.
- Entry zone.
- Invalidation condition/level.
- One or more targets.
- Why it may move.
- Relevant catalyst/context, including an explicit note when no fresh news is present.
- Key risks and assumptions.
- Intended experiment: aggressive paper, stricter live candidate, or both under their separate rules.

If fewer than five names qualify under the documented selection policy, publish the smaller list and explain why; do not lower standards or invent candidates merely to reach five. Freeze or version the list at publication time so later edits are auditable.

### 6. Log every trade separately

Log every paper trade and every live trade in separate append-only ledgers. Each record must include, where applicable:

- Stable trade ID and experiment ID.
- Prediction/watchlist link or ID.
- Ticker, side, quantity, order type, and timestamps.
- Planned and actual entry, stop/invalidation, targets, and exit.
- Fees/slippage assumptions or actuals.
- Realized and unrealized result.
- Maximum favorable/adverse excursion when data permits.
- Reason for entry, exit, modification, rejection, or no-fill.
- Whether execution followed the plan.
- Source data references and any operational errors.

Never merge paper and live performance totals.

### 7. Run an end-of-day review

After each market session, write a dated review that:

- Grades every Top 5 prediction against a predefined, versioned rubric.
- Reviews paper and live results separately.
- Identifies missed opportunities and why they were missed.
- Identifies false positives and the signals that caused them.
- Evaluates entry, sizing, stop, exit, slippage, and overall execution quality.
- Compares predicted thesis, levels, and timing with actual behavior.
- Records data quality or pipeline failures.
- Extracts concise lessons and creates specific follow-up tests.

Use honest null/no-trade outcomes and distinguish bad process from bad outcome.

### 8. Promote only validated, repeatable edges

Write an edge into `knowledge/promoted-edges/` only after repeatable evidence supports it. Every promoted edge must include:

- Clear rule and scope.
- Hypothesis origin.
- Test period and sample size.
- Paper and live evidence kept distinct.
- Metrics, baseline comparison, and known failure regimes.
- Data/source assumptions.
- Validation date and version.
- Promotion decision and rollback/deprecation criteria.

One strong anecdote, one profitable trade, or an unreviewed correlation is not sufficient for promotion.

### 9. Separate observations from validated rules

Store raw observations, hypotheses, candidate patterns, and session notes outside `knowledge/promoted-edges/` in an explicitly labeled observations/research area. Raw notes must never be consumed as production rules merely because they exist in the repository. Promotion must be an explicit, reviewed, versioned action supported by evidence.

### 10. Preserve history and commit meaningful changes

- Never erase or rewrite prior ledgers, prediction snapshots, reviews, observations, or promoted-edge history.
- Prefer append-only records and new versioned artifacts for time-dependent outputs.
- Commit cohesive changes with descriptive messages explaining what changed and why.
- Do not combine unrelated work in one commit.
- Never commit Massive credentials, brokerage credentials, tokens, or other secrets.
- Record configuration and schema migrations so prior runs remain interpretable.

## Minimum repository outputs

Use existing repository conventions when present. If none exist, establish and document a simple structure equivalent to:

- `config/` — screener, score, schedule, experiment, and risk configuration.
- `predictions/premarket/YYYY-MM-DD.*` — immutable daily Top 5 snapshot.
- `ledgers/paper/` — aggressive paper-trader ledger.
- `ledgers/live/` — stricter live-trader ledger.
- `reviews/eod/YYYY-MM-DD.md` — daily grading and lessons.
- `research/observations/` — raw observations and unvalidated hypotheses.
- `knowledge/promoted-edges/` — validated repeatable rules only.
- `validation/` — run manifests, test results, data-quality reports, and replay evidence.

Do not rename established paths solely to match this example; map existing paths to these responsibilities instead.

## First execution sequence

1. Inventory the repository and read all current brain, configuration, ledger, review, and promoted-edge files.
2. Document the existing architecture and map each requirement above to code and output paths.
3. Run automated tests, then test the screener boundary cases and scoring range/component totals.
4. Execute a Massive-backed dry run or historical replay across the full eligible universe and save its manifest.
5. Verify that no-fresh-news candidates remain eligible when the price and volume baseline passes.
6. Verify paper/live isolation through configuration, storage, reporting, and execution guards.
7. Generate a sample or current-day Top 5 artifact with every required field.
8. Verify separate trade logging and produce a sample end-of-day review from replay data if a live session is unavailable.
9. Audit `knowledge/promoted-edges/` and move or flag any item that lacks repeatable validation evidence; preserve its history.
10. Commit each cohesive change and write a validation summary listing passes, failures, evidence paths, unresolved risks, and next actions.

## Definition of done

Do not mark validation complete until repository evidence demonstrates that:

- The 3:55 a.m. New York workflow can process the full eligible universe with Massive and produces an auditable run manifest.
- The price and volume boundaries are tested and fresh news is not a requirement.
- Every eligible candidate receives an explainable 0–100 score containing all required factors.
- Paper and live experiments cannot contaminate one another.
- A versioned Top 5 output contains all required decision fields.
- Every trade is recorded in the correct separate ledger.
- The end-of-day review grades predictions, opportunities, errors, execution, and lessons.
- Raw observations remain separate from promoted rules.
- Every promoted edge has repeatable validation evidence.
- Meaningful changes and session outputs are committed, with secrets excluded.

If any item fails, leave the system status as **not yet validated**, record the exact evidence and remediation, and continue from the repository state in the next Codex session.
