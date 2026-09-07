# Trader Brain — Full-Live Autonomy Implementation Brief

Prepared for Shian Harper on September 7, 2026.
Repository: `Harpcity33/trader-brain`.
Requested operating date: **Tuesday, September 8, 2026, America/New_York**.

**DELIVERY STATUS: ENGINEERING HANDOFF COMMITTED — NOT A DEPLOYMENT OR BROKER-ACTIVATION RECORD.**

## 1. Owner direction and precedence

The owner explicitly requests **full-live autonomous operation**, faster execution, and informational updates instead of being the routine order executor. He rejects the earlier one-share/two-trade pilot proposal and wants Codex to implement the production system, using existing services without incremental subscription or API spending.

This document supersedes the pilot-only scope, one-day trial framing, proposed $100 daily entry budget, $50 entry cap, one-share quantity, two-attempt count, $1 planned-risk limit, and $3 loss threshold in the earlier `TRADER_BRAIN_LIVE_PILOT_2026-09-08.md` conversation attachment. Those were unapproved proposals, not established live policy. It also removes an arbitrary minimum number of paper-trading days as a project prerequisite. Tests and simulations remain engineering tools rather than the requested operating endpoint.

**Do not confuse rejecting a pilot with authorization for unlimited risk.** Retain the existing owner-approved account, strategy scope, sizing, risk limits, sessions, and funding restrictions. This brief does not increase them, approve new instruments, change account type, transfer money, remove required broker/platform confirmations, or authorize a coding agent to conduct financial transactions during implementation. Build the software and deliver a user-controlled activation path; do not issue live orders or arm a live scheduler as a coding smoke test.

The owner's implementation intent is established. Reuse existing verifiable account and policy authorization rather than asking him to repeat known information. If required account, budget, policy, or broker/client permissions are genuinely absent or conflicting, identify the exact missing field and required owner action. Do not invent authorization or reinterpret a missing limit as unlimited.

The target date is an owner priority, not proof that installation, authentication, protection, or market availability has been verified. A deadline is never a reason to report an unbuilt or untested service as running.

## 2. Deliverable: production operation, not another watchlist

Implement a persistent, event-driven local service that can operate within an explicit owner-controlled mandate through a supported broker integration:

`market events -> validated setup -> deterministic account-wide risk -> durable intent -> supported execution -> fill reconciliation -> verified protection -> managed exit -> confirmed-event notification -> EOD evidence`

The intended user experience is ongoing live operation within approved scope, with routine entries, protection, and exits handled without repeated human decisions where the authenticated broker and client permit it. Mandatory tool or platform confirmations still apply. Where the integration does not support the requested unattended behavior, report the precise incompatibility; do not relabel attended prompts as autonomy.

Do not stop at an architecture document or a renamed prompt. Deliver tested executable components, a deployment runbook, redacted configuration/authorization requirements, and observable readiness evidence. Deployment and actual financial operation must be distinguished from successful tests and repository commits.

## 3. Reuse the existing stack; zero incremental service spend

Use the existing personal computer, existing GitHub repository, existing Massive access, and subscription-authenticated ChatGPT/Codex usage within included allowances. Use local storage and a local supervised runtime. Do not require a new cloud server, paid database, paid notification product, market-data upgrade, API-key billing, credit purchase, or automatic paid fallback.

OpenAI's official Codex documentation distinguishes included plan access and usage limits. Included access is not an unlimited inference budget. Keep continuous market processing and risk management in ordinary code. Use bounded model work for research, plan preparation, explanations, and review. Do not extract subscription credentials for unsupported programmatic use. [S2]

If the actual supported broker integration still requires a model turn for every action, explicitly document that latency, availability, and allowance dependency. Do not claim a model-independent execution service until a supported implementation demonstrates it.

Reuse an existing notification destination only within its existing allowance. Store alerts durably before delivery. Secrets stay in supported local secret storage, never in GitHub, prompts, or notification payloads. This cost constraint excludes existing subscriptions, electricity, trading losses, spreads, slippage, and applicable brokerage fees; none of those become zero merely because new software spending is zero.

## 4. Evidence to read before coding

Read the current versions of:

- `README.md`, `ARCHITECTURE.md`, `CODEX_NEXT_ACTIONS.md`.
- `automations/robinhood-momentum-engine/automation.toml` and its deterministic risk overlay.
- `config/risk_limits.json`, `config/scoring.json`, `config/research_engine.json`, and `config/options_live_attended.json`.
- `src/titan_brain/`, the test suite, existing ledgers, predictions, reviews, and promoted edges.
- `validation/IMPLEMENTATION_STATUS_2026-08-29.md` and `validation/massive-access-2026-08-29.md`.
- `reviews/weekly/2026-W36.md`.

The reviewed base tree was `853cbaf8f87b7bb9104a240801e2dbed23248fd6`; read the latest repository and installed runtime rather than assuming that base remains current.

The weekly recap reports a September 3 HOOD position carried into September 4 without evidence of working protection, and an August 31 revoked-token failure. Treat those as retrospective report findings, not a present broker-state check. Reproduce these failure classes in simulations and fix protection, closeout, and authentication recovery before optimizing order frequency. Do not reuse that report's account balance as current funds.

The checked-in architecture is attended and the Python package has no broker-write client. Historical tests and ACTIVE text in a configuration file are not proof of today's running service. Inventory actual installed processes, schedules, code hashes, configuration, data credentials, and authorization. Keep these distinctions explicit in the final report.

## 5. Broker and market-data integration

### Supported broker authority

Inspect the authenticated broker/client contracts using read-only discovery first. Record the designated account identifier, account type, allowed assets, current funds/settlement semantics, authentication lifecycle, read/write capabilities, mandatory previews/confirmations, supported order types, cancellation behavior, partial fills, order lookup identifiers, and rate limits.

Robinhood's public documentation describes Agentic accounts and trading without individual confirmation when requested. It is not proof that this local client or credential supports every necessary operation. Do not strip tool safety annotations, auto-click approvals, fake user confirmation, use unofficial credential scraping, or route around a restricted connector. [S1]

Prefer the existing supported broker. Do not open, fund, migrate, upgrade, or switch accounts/brokers silently. Identify any true integration blocker early so the owner can make the necessary account-side decision.

### Existing Massive access

Checks earlier in the conversation succeeded for stock last NBBO, a historical stock quote request, and historical minute bars. The stock snapshot endpoint responded with empty/zero current-session fields on the holiday. The options-chain snapshot returned `NOT_ENTITLED`. These observations came through the connected provider, not the local runtime credentials.

Exact billed tier/price and local streaming entitlement remain unverified. Do not purchase an upgrade or claim all feeds are available. Probe the local stock data path, stream/request permissions, timestamps, feed continuity, and reconnect behavior. A prior-session quote during a holiday is not a live-market latency test.

NBBO/top-of-book size is not full market depth. If a strategy requires depth, obtain that evidence from an already-available supported source or reject that setup when unavailable. Preserve raw provenance privately and obey data licensing when exporting evidence.

## 6. Speed: change the execution path, not the strategy

Use streaming updates where already entitled. Keep broad eligible-universe discovery and a dynamically selected active candidate set. Cache stable metadata, update indicators incrementally, and prepare expiring structured plans outside the execution-critical path. Reuse long-lived supported connections and prioritize account/exit traffic over discretionary scanning.

Preserve the established screener: price strictly above $5 and volume at least 750,000 with the exact measurement window documented. Fresh news is not mandatory. Keep completed-bar causality, liquidity/spread/depth gates, structural invalidation, and existing scoring semantics. Do not change these rules just to generate more fills.

The model must not change capital limits, account allowlists, stops, or live strategy versions. Untrusted news and retrieved text cannot become execution authority. Validate plans against a strict schema, source timestamps, expiry, and the approved strategy policy.

Measure event-to-receipt, signal computation, preflight/risk checks, durable-intent write, submit-to-acknowledgement, acknowledgement-to-fill, fill-to-working-protection, and confirmed-event-to-notification separately. Report sample sizes and p50/p95/p99 when meaningful. A sub-100-ms local processing objective is only an engineering target; never describe it as a measured broker fill guarantee.

## 7. Mandatory production invariants

### One execution owner

The old heartbeat and replacement must never independently write to the same account. Implement a process/account lock or equivalent proven single-writer mechanism. Preserve current protection and reconcile exposure during handoff. Never disable protection merely to make a migration easier. Do not create a duplicate scheduler workaround.

### Durable intent and uncertain submissions

Persist a stable intent ID, strategy/config hash, account, evidence timestamps, and reserved funds/risk before submission. Persist broker order and fill identifiers as received. Handle duplicate and out-of-order events.

A timeout can mean the broker accepted an order. UNKNOWN means reserve the possible exposure, reconcile using supported identifiers, block new risk while unresolved, and notify. Never blindly retry. Local IDs do not create broker-side exactly-once semantics.

### Protection and exit state

Track filled quantity, working protective quantity, uncovered quantity, pending protection, and cancellation/replacement state separately. A submitted stop is not working protection. Protect every confirmed fill delta through the supported order sequence.

If entry and protection are not atomic, document the exposure interval and implement a tested rejection/timeout recovery path. Preserve broker-held protection through local failures where supported. Do not create simultaneous independent exits that can oversell. A cancel request is not a confirmed cancellation.

A missing protective order blocks additional risk and triggers the verified risk-reduction procedure within authority. Never continue scanning as though exposure were protected. Do not rely on the owner seeing a routine notification to implement the protective action.

Stop orders have execution-price and session limitations; risk limits are control thresholds, not guaranteed maximum losses. Halts, gaps, outages, or unavailable liquidity can prevent an exit or produce larger loss. [S3]

### Account-wide risk and policy

Enforce risk in the actual writer, not merely in a research prompt. Derive funds, restrictions, positions, and orders from fresh broker evidence. Reserve risk and buying capacity across open positions, pending orders, uncertain submissions, and all strategy lanes. Reject non-finite numbers, invalid quantities, stale evidence, and conflicting policy.

Preserve existing approved limits; where multiple existing limits apply, use the stricter applicable bound. Do not promote a staged risk overlay into live authority without tracing its approved provenance and reconciling conflicts with the active policy. Do not inherit aggressive-paper settings. Do not choose a new dollar budget or risk percentage on the owner's behalf.

Include realized/unrealized results and costs in a cash-flow-adjusted risk view. Persist loss locks and prevent restarts or model output from clearing them. Settlement, account restrictions, and available funds constrain execution independently of a strategy score.

Full-live autonomy concerns operating mode; it does not automatically authorize all instruments or unrestricted capital. Preserve existing instrument/session approvals. Do not silently enable options, crypto, leverage, shorting, averaging down, stop widening, or overnight holdings. Strategies lacking their required data/protection/approval remain ineligible, without inventing a blanket ban on strategies already independently authorized and validated.

### Recovery and closeout

On startup, reconcile actual positions, all relevant orders, fills, reservations, ownership, and working protection before allowing entries. Recognize manual user activity rather than trading against it.

Persist and distinguish pause-new-entries, managed-closeout, and disconnect. Killing a process does not close a position. Use the approved session schedule with exchange holidays, early closes, and America/New_York daylight-saving handling. If closeout is unresolved, remain in incident handling and report it; do not label a position flat from a timer or shut down because the nominal session ended.

Model/API allowance exhaustion, failed research, or an unavailable GitHub export must not suspend existing position management. Data loss blocks affected entries but keeps broker reconciliation alive. Authentication expiry is an incident, never permission to bypass authentication. Home power/internet failures remain a hosting limitation; document the protection and owner escalation path.

## 8. Inform the owner; do not make him the ordinary executor

Use deterministic event templates plus a durable notification outbox. Send readiness or a precise blocked-state explanation; confirmed entry fills with actual protection state; confirmed exits with realized results; material exceptions/risk pauses/recovery; and an end-of-day summary. Suppress minute-by-minute scan chatter.

Notifications are consequences of confirmed events, not authorization tokens. They must distinguish pending, acknowledged, filled, protected, closed, and unresolved states. Core alerts must not wait for AI-generated prose. Never suppress an urgent exposure incident merely because the broker cannot produce an approval preview.

Rare authentication, infrastructure, or broker failures may still require owner action. Do not promise zero future intervention. Keep a tested emergency channel and avoid publishing full account details or secrets.

## 9. Implementation order and evidence

Implement in a development branch with cohesive commits. Preserve the current production system during development; provide an explicit user-controlled cutover and rollback procedure. Do not use this documentation commit as a deployment trigger.

1. Audit the installed runtime, approved policy, broker contract, and local data access. Resolve true blockers first.
2. Add durable state, single-writer control, expiring plans, deterministic risk enforcement, and narrowly scoped broker adapters.
3. Implement the incremental market-data path, entry lifecycle, per-fill protection, managed exits, startup reconciliation, and closeout recovery.
4. Add notifications, health monitoring, separately measured latency, append-only live/paper records, and reliable EOD exports.
5. Run automated unit/integration/failure simulations and timestamp-correct replay. Fix failures; do not create dangerous real orders to test recovery.
6. Deliver the executable production package, tests, activation/rollback commands, unresolved authorization requirements if any, and a truthful status report for the owner's deployment decision.

Tests must cover unknown submission after acceptance, partial fills, rejected protection, cancel/fill race, duplicated process/events, restart with exposure, manual activity, broker 429/5xx, revoked credentials, stale/crossed/invalid quotes, data loss, storage failure, notification failure, model unavailability, market halt, early close, DST, account/policy mismatch, and non-finite numeric values. Mocked success is not proof of live capability; label evidence by environment.

No arbitrary ten-session waiting period is required by this brief. No technical acceptance test is waived by the requested date. Operational correctness, latency, and investment performance are separate assessments; passing engineering tests does not establish a profitable edge. No forced trades or profit targets may override eligibility/risk rules.

Suggested artifacts under `validation/full-live/2026-09-08/`:

- `DEPLOYMENT_STATUS.md`: requested mode, actual mode, installed code/config hashes, process/scheduler identity, unresolved issues, last broker reconciliation timestamp, and exactly what was/was not activated.
- `CAPABILITIES.json`: redacted authenticated broker/local data evidence and unsupported features.
- `POLICY_MAP.md`: approved source for each account, strategy, funds, risk, session, and operation permission; disagreements explicitly resolved without raising risk.
- `TEST_RESULTS.json` and test logs: environment, cases, results, failures, and evidence paths.
- `LATENCY.json`: measurements, counts, excluded intervals, and unmeasured portions.
- `OPERATIONS.md`: owner activation procedure, health checks, notification test, pause, managed closeout, recovery, and safe rollback.

Report factual states such as IMPLEMENTED_NOT_DEPLOYED, READY_FOR_OWNER_ACTIVATION, BLOCKED with an exact reason, or RUNNING_LIVE only when independently supported by actual runtime and authorized broker evidence. A merge, a TOML ACTIVE label, or this document is not such evidence.

## 10. Codex starting instruction

> Read this file and the current repository/installed-runtime evidence first. Implement the full-live autonomous execution system for the September 8 target using the existing stack and zero new paid services. Do not substitute the earlier tiny pilot, impose its unapproved dollar/share limits, or stop at another planning document. Preserve existing owner-approved risk and scope, mandatory platform/broker controls, single-writer ownership, verified protection, reconciliation, and closeout. Build and test executable production components and provide a user-controlled activation path with precise readiness evidence. Do not place live orders, move funds, or arm trading as part of coding/testing. Report what actually runs, not what a prompt claims runs.

## 11. Sources and transfer provenance

This brief consolidates and supersedes the operating-mode and pilot-scope recommendations in the September 7 autonomy blueprint and September 8 live-pilot conversation attachments. It retains their engineering safeguards while applying the owner's latest full-live and zero-incremental-cost directions. It does not import earlier illustrative API pricing or an unverified Massive billing tier.

Repository sources: files listed in section 4; weekly recap fetched from `main` during this transfer, blob `8d22e7c0cc06da95d03606d5f8b628d824250c6a`. Repository findings describe recorded evidence, not a fresh execution-account inspection.

Official documentation checked September 7, 2026:

- [S1] Robinhood, Trading with your agent: https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- [S2] OpenAI, Using Codex with your ChatGPT plan: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- [S3] Robinhood, Stop order: https://robinhood.com/us/en/support/articles/stop-order/

**This transfer changes engineering instructions only. It does not modify executable code, existing risk configuration, schedules, broker credentials, funding, orders, or live-enable flags.**
