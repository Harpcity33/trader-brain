# Titan EOD Review — YYYY-MM-DD

Status: immutable after publication

## Session and data-quality record

- Market regime:
- Broker reconciliation revision:
- Market-data gaps/errors:
- Operational errors:

## Prediction and candidate grading

For every candidate record `setup_id`, SETUP_SCORE, EXECUTION_SCORE, direction,
timing, entry, invalidation, targets, MAE_R, MFE_R, outcome, and rejection or
no-fill reason.

## Trade reviews — isolated by ledger

### Live equity

### Live options

### Aggressive paper lab

Never merge totals across these sections.

## Process/outcome quadrants

- GOOD_PROCESS_GOOD_OUTCOME:
- GOOD_PROCESS_BAD_OUTCOME:
- BAD_PROCESS_GOOD_OUTCOME:
- BAD_PROCESS_BAD_OUTCOME:

Profitable rule violations remain bad process and are not reinforced.

## Route comparisons

Compare actual and shadow routes using realistic spread/slippage assumptions.
For options include IV, theta, Greeks, implied/realized move, and full-premium
stress.

## Lessons and new observations

Write new hypotheses to `research/observations/`; do not edit promoted edges.

## Promotion status

No automatic promotion. State maturity stage, sample size, regimes,
out-of-sample result, drawdown, outlier dependence, execution quality, failure
conditions, and explicit human review status.

