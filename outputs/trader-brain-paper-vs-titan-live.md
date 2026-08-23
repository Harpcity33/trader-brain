# Trader Brain Paper Project vs. Titan Live — PR #3 Ingestion and Adaptation

Version: `trader_brain_paper_2026-08-23_v1`  
Source reviewed: draft PR #3 at `7a893aceb02a4c2c37d8fb6f046924689d2b37ae`  
Status: paper-project adaptation only; no live authority and no automatic merge of PR #3

## Purpose

This document records the full architectural comparison between:

1. **Trader Brain aggressive paper trading** — a $500 settled-cash research experiment designed to discover, simulate, measure, and learn from intraday momentum opportunities; and
2. **Titan live** — the stricter Robinhood-connected execution system in draft PR #3, with broker truth, live account controls, atomic risk authorization, protection, and production change control.

The projects share market-intelligence concepts, but they are not interchangeable. Paper results may create hypotheses and validated research rules; they never authorize a live order or silently change Titan production.

## Governing distinction

| Area | Trader Brain paper | Titan live PR #3 |
| --- | --- | --- |
| Objective | Learn which momentum setups produce after-cost paper expectancy | Execute only independently qualified live setups inside broker and account controls |
| Capital model | $500 starting settled cash | Fresh Robinhood unleveraged buying power and equity |
| Daily deployment | At least $500 gross new-entry notional or label `DEPLOYMENT FAILURE` | No forced trade and no capital reserve for a hypothetical second play |
| Risk model | Target total stop-defined risk no more than $35 when technically possible; target is not a fabricated guarantee | Irreversible -$100 account-day lock, +$150 objective, and +$125 floor after first +$150 crossing |
| Settlement | Same-day sale proceeds cannot fund another entry | Governed by current live Robinhood account mechanics |
| Authority | Simulated paper orders only | Separately authorized Titan/Robinhood controller only |
| Data | Massive is primary; external catalyst and security-risk evidence may supplement | Massive is data-only; Robinhood is broker truth and Level 2 source |
| Position concentration | Full $500 may go to one materially best setup | 0%–100% of fresh unleveraged capital may go to one materially best setup |
| News | Optional ranking/context input | Optional ranking/context input; adverse filings and security risks remain mandatory checks |
| Score | Ranking and measurement, not a standalone entry veto | Ranking and measurement, not a standalone entry veto |
| Fills | Timestamped simulated fills using observable quote evidence and declared cost assumptions | Broker-confirmed fills only |
| Protection | Simulated structural stop and risk ledger | Broker-reviewed and broker-confirmed full-quantity protection |
| Learning | May test experimental rules rapidly but must preserve evidence and holdouts | Production changes require separate approval, durable guards, and live-safe validation |
| End-of-day | Paper ledger, deployment grade, P&L, MFE/MAE, missed opportunities, lessons | Broker-reconciled performance grade and immutable risk/order evidence |

## PR #3 concepts adopted now for the paper project

### 1. Full-universe Massive discovery

The paper desk will start with the complete eligible Massive universe rather than a hand-selected watchlist. The core paper lane retains the existing boundary of price strictly greater than $5 and at least 750,000 shares of volume. A separate $1–$5 experimental lane remains available under stricter liquidity and security-risk review.

The scanner must record the universe size, screening timestamp, accepted/rejected symbols, rejection reasons, missing data, API failures, and runtime. Missing required evidence is represented as unknown; it is never fabricated or silently forward-filled.

### 2. Causal same-minute relative volume

Relative volume must compare current cumulative volume with prior completed sessions at the same local minute. Prior full-day volume is not a valid fallback for a partial current session. When same-minute history is insufficient, RVOL remains unknown and its evidence quality is recorded.

### 3. Ranking without hidden zero penalties

Unknown catalyst, sector, float, dilution, or historical fields remain explicitly unknown. The weighted model normalizes across covered weight and publishes evidence coverage. Fresh news, a minimum score, and a momentum-state label are not hidden hard gates.

Hard rejections remain for stale/conflicting data, ineligible security types, unresolved halts, materially adverse financing/dilution, listing failures, manipulation/promotion evidence, and invalid structural geometry.

### 4. Remaining opportunity over raw percentage gain

Raw percentage change is not the primary ranking field. The paper desk will separately report:

- confidence/evidence quality;
- remaining opportunity;
- structural risk; and
- evidence coverage.

A stock already up 200% may be an important market event but a poor current entry. A smaller mover beginning a fresh causal expansion may be the better paper trade.

### 5. Reject entry, keep watch

Immediate entry rejection is separated from permanent deletion. Extension, a vertical candle, or a temporary exhaustion lock may produce `ENTRY_REJECTED_KEEP_WATCH`. The symbol may requalify only after a fresh controlled base, reclaim, higher low, opening-range structure, or renewed expansion.

Hard failures remain disqualifying until independently resolved.

### 6. Exact quote-confirmed triggers

A completed bar whose high touched a trigger does not prove that a paper order filled. A simulated fill requires:

- a decision and paper submission record created before the fill;
- a fresh observable ask at or through the trigger;
- a fresh quote no older than the configured limit;
- quoted spread inside both the absolute lane limit and 15% of entry-to-stop structural risk;
- price inside the reviewed limit and 0.625-short-ATR chase ceiling; and
- renewed volume pace or other exact setup confirmation documented at the decision time.

The reviewed entry ceiling, not a more favorable trigger price, drives quantity and 1R/2R/3R reference arithmetic.

### 7. Counterfactual entry protocol

For every serious candidate, the system freezes three lanes before the outcome is known:

1. earliest reasonable entry;
2. conservative confirmation entry; and
3. selected paper entry.

Only after the observation window closes may the system attach MFE, MAE, lane P&L, hesitation cost, and confirmation savings. Counterfactual entries are never fills and never count toward official paper P&L.

### 8. Simulated lifecycle integrity

Paper orders will use an explicit simulated lifecycle:

`PLANNED → REVIEWED → SUBMITTED → PARTIAL/FILLED → PROTECTED → CLOSING → CLOSED`

Terminal alternatives include `CANCELED`, `REJECTED`, and `NO_FILL`.

A paper fill cannot exist before a paper submission. Quantity may only increase by a recorded simulated fill increment. Original entry, initial quantity, stop, decision timestamp, and evidence references are immutable. Corrections require a superseding record rather than rewriting history.

### 9. Notional and downside are separate

Using all $500 of paper notional does not mean risking $500. Proposed risk is the worse of:

- stop-defined loss including modeled execution cost; and
- conservative stress/tail loss.

The existing paper target remains no more than $35 total stop-defined risk when technically possible. Exceeding the target must be visible and explained; the target is not a claim that gaps, halts, spread expansion, or slippage cannot produce a larger loss.

### 10. Concentration without forced diversification

The full paper account may be allocated initially to one materially best setup. Capital is not reserved for a second trade merely for diversification or activity. Adds are optional, cannot be averaging down, and require a profitable strengthened thesis with explicit revised risk accounting.

### 11. Halt and data-health controls

A halt disarms every pre-halt base. Resumption begins a new structure. Stale Massive data, lost entitlement, a broken connection, or materially conflicting quotes suspend new paper entries. Existing simulated positions remain subject to the documented safe-exit assumption rather than invented stop execution.

### 12. Process-heavy daily grading

Paper performance is graded 80% process and 20% outcome. A profitable hindsight fill is a hard failure, not a success. A losing trade that followed a causal plan may have a stronger process grade than a profitable fabricated trade.

Paper hard failures include:

- future data or hindsight reconstruction;
- reusing unsettled same-day sale proceeds;
- live/paper ledger contamination;
- entering without decision-time evidence;
- rewriting or deleting append-only history; and
- representing a counterfactual or backtest result as an actual paper fill.

## PR #3 concepts retained as experiments, not promoted paper rules

### Position Continuation Score

The PR #3 continuation model is useful for measuring whether strength remains sponsored, but its thresholds are not automatically promoted to the paper strategy. The paper ledger may record structure, participation, level control, liquidity, catalyst integrity, sector confirmation, and remaining capacity as an experimental continuation score.

### Core-plus-runner management

A 20%–30% runner and conditional trims may be tested. They are not a universal exit rule. PR #3's full-candle study did not validate a blanket later-exit amendment after realistic costs. One hard invalidation or multiple independent exhaustion signals remains the preferred evidence for discretionary exit testing.

### Earliest-entry aggression

Earlier participation is measured against confirmation savings and hesitation cost. The system must not assume that waiting is automatically safer or that earlier is automatically better. Both lanes require prospective evidence and after-cost comparison.

### Evidence thresholds for rule promotion

The paper project adopts the PR #3 governance floor as a starting standard: at least 100 distinct prospective ticker-sessions, 15 sessions, chronological holdout testing, at least 30 holdout opportunities across five holdout sessions, and an after-cost confidence interval whose lower bound exceeds zero. These thresholds govern evidence status; they do not create live authority.

## PR #3 concepts deliberately not imported

The paper project does not inherit:

- Robinhood account ending identifiers or account-specific state;
- the +$150 objective, -$100 lock, or +$125 post-objective floor;
- broker-only buying-power, order-review, Level 2, fill, or protection claims;
- live risk authorization leases or broker reconciliation semantics;
- margin, PDT, or Limited Margin rules in place of the $500 cash-account model;
- automatic production changes after a daily grade;
- a live-order submission path;
- short stock, option spreads, leverage, or overnight exposure; or
- any superseded fixed premarket, campaign-risk, or forced-tranche limit from older playbook versions.

Massive top-of-book data must never be described as Level 2 depth. When genuine depth data are unavailable, depth is unknown rather than inferred.

## Preserved paper-project rules that differ from Titan

### Mandatory deployment scorecard

Trader Brain retains the user's requirement to record at least $500 gross new-entry notional per trading day. Failure to meet it is labeled `DEPLOYMENT FAILURE`.

This requirement never authorizes a fabricated fill, a hindsight entry, reuse of unsettled proceeds, or a knowingly invalid setup. The ledger must show the failure honestly when the requirement is not met.

### Cash settlement

Same-day sale proceeds do not become settled cash for another entry. A $500 purchase followed by a same-day sale normally consumes the day's entry capacity unless other settled cash existed before the trade.

### Paper schedule

Trader Brain retains its established reporting cadence:

- 7:00 a.m. ET intelligence;
- 9:15 a.m. ET final plan;
- hourly desk from 10:00 a.m. through 4:00 p.m.; and
- 4:20 p.m. ET dated GitHub handoff.

Titan's 4:00 a.m. watcher may supply market intelligence, but it does not change the paper report schedule or create an earlier paper fill.

## Paper operating sequence after this adaptation

1. Load the latest GitHub paper policy and prior append-only state.
2. Record the run and data timestamps.
3. Scan the full Massive eligible universe.
4. Apply the core and under-$5 lane filters.
5. Calculate causal same-minute RVOL where evidence exists.
6. Rank by weighted opportunity, remaining capacity, liquidity, structural risk, and evidence coverage.
7. Publish and freeze the Top 5 or the smaller qualified list.
8. Preserve earliest, conservative, and selected entry plans before outcomes.
9. Create a simulated order only after an exact causal trigger and paper review.
10. Apply cash-settlement and stop/stress-risk checks.
11. Record the full simulated lifecycle and source quote assumptions.
12. Manage positions using structural evidence; do not force later exits or early trims from a fixed percentage alone.
13. Close intraday exposure by 4:00 p.m.
14. Reconcile settled cash, unsettled proceeds, notional deployed, P&L, MFE/MAE, and deployment status.
15. Grade process and outcome separately.
16. Save observations and hypotheses outside promoted rules.
17. Publish the dated 4:20 p.m. GitHub handoff without rewriting prior history.

## Known PR #3 inconsistencies and limitations

- PR #3 remains draft and unmerged. This paper adaptation is based on its head commit and must be re-reviewed if that head changes.
- Some narrative files mention a 3:55 a.m. start, while the committed Massive config and LaunchAgent use 4:00 a.m. The paper project relies on the recorded data timestamp rather than assuming either time.
- PR #3's tests validate Titan's live/shadow invariants, not this $500 cash-account paper model. A separate paper-policy validator and tests are required.
- PR #3's historical studies demonstrate that movement scores and selected leader cohorts can look stronger than prospective after-cost results. The paper project therefore treats scores as ranking tools and requires chronological, cost-aware validation before promotion.
- The paper risk target is not a hard realized-loss guarantee. A separate hard paper loss lock has not been authorized and is not invented here.

## Implementation paths

- Canonical paper policy: `config/paper-trading/trader-brain-paper.json`
- Paper policy validator: `titan_runtime/paper_policy.py`
- Paper validator tests: `tests/test_paper_policy.py`
- Adaptation decision record: `knowledge/adaptations/2026-08-23-pr3-paper-project.json`
- Existing live/shadow policy remains isolated under `config/live-automation/` and `config/titan-massive.json`

This adaptation changes the paper research and simulation contract only. It grants no broker authority and makes no claim that the paper strategy has validated positive expectancy.
