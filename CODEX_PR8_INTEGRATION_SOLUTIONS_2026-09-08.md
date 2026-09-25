# Trader Brain PR #8 — Integration solutions and targeted code review

Prepared for Shian Harper for the September 8, 2026 operating target.
Reviewed PR head: `2817620ef0aa24182644012f907615d501837fae`.
Branch: `codex/full-live-2026-09-08`; repository: `Harpcity33/trader-brain`.

**Scope:** Complete the requested full-live system within the existing approved mandate and zero new paid service spending. This is not a reinstatement of the rejected tiny pilot. No arbitrary paper-session waiting period is added.

**Status of this contribution:** A read-only MCP discovery component and 17 standalone tests were implemented and tested. The original 255-test suite was NOT re-run here. No Mac deployment, authenticated Robinhood session, notification delivery, broker operation, permission change, account funding, scheduler activation, or merge was performed. The operational remedies below require implementation/integration and fresh evidence; they are not claims of resolved live capability.

## 1. The actual integration gap

`src/titan_brain/live/broker/robinhood.py` is a supplied-read-evidence adapter. Its default has no snapshot provider, and its review/place/cancel methods unconditionally raise `BrokerMutationBlocked`. That is an appropriate placeholder for the observed attended connector, but it is not a working unattended broker transport. `cli.py` constructs this adapter directly in readiness. Turning configuration booleans on cannot implement the missing transport.

Keep that attended adapter intact for its documented context. Add a distinct, supported production adapter only after the endpoint, authenticated client, scopes and broker/client approval rules have been established. Do not call the model-mediated connector from a shell by borrowing its credentials; do not strip mandatory confirmation requirements.

Robinhood's public documentation supports trading without per-order confirmation and documents an official Streamable HTTP MCP connection. The current audit describes a narrower attended connection. Neither fact disproves the other. The first implementation task is to determine exactly which layer imposes the observed restriction.

## 2. First task: complete, provenance-preserving MCP discovery

### New executable helper

- `src/titan_brain/live/mcp_inventory.py`
- `tests/test_mcp_inventory.py`

`discover_tools(rpc)` sends ONLY `tools/list` through an existing authorized session. It follows all opaque `nextCursor` values, including the valid empty string. It fails the entire collection on a later-page error, cursor loop, duplicate tool name, malformed schema, timeout, or limit breach. It preserves complete descriptions, annotations, and schemas, produces a deterministic inventory hash, and compares advertised names with client-visible names.

It never calls a discovered tool, logs in, loads credentials, grants authority, or infers permission from a tool name/annotation. Its report explicitly marks unattended permission and order completeness `NOT_ASSESSED`.

Run its tests with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_mcp_inventory.py' -v
```

Local result for this contribution: **17/17 passed**. Re-run these and the full original suite in the actual checkout before creating a new release.

### Why pagination must be checked

OpenAI Codex issue #28858 reports first-page-only MCP discovery in CLI 0.140.0 and remains open when reviewed. This is a reported upstream bug, not proof that Shian's installed version has it or that missing Robinhood tools exist on another page. Compare installed version, the complete authenticated server inventory, and the client-visible inventory before labeling features unsupported. Do not upgrade blindly during the session; version and test any change.

### Required evidence

Capture redacted metadata for the actual configured server: endpoint, client version, server version, negotiated protocol, full inventory hash, page count, namespace mapping, authenticated account binding, available scopes, and exact source of each confirmation requirement. Keep tokens, authorization codes and protected account payloads out of GitHub.

The official endpoint is:

```text
https://agent.robinhood.com/mcp/trading
```

Use Codex's supported MCP settings and OAuth login when applicable. Inspect the existing server first; do not create duplicate trading clients or invalidate an existing active session casually. A standalone process must obtain its OWN provider-supported authorization through normal OAuth, including supported renewal behavior. It must not reuse/extract the ChatGPT/Codex subscription token or copy another client's credentials.

Outcome classification:

1. Missing later-page tools: fix discovery and repeat capability evaluation without changing broker/client approvals.
2. App-maintained confirmation policy only: retain it until a separately authorized, provider/platform-supported unattended integration is demonstrated.
3. Broker or platform mandates per-action confirmation: preserve it. Report that exact constraint. No global approval-disable flag, fake confirmation string, proxy that drops restrictions, browser clicking, or undocumented brokerage endpoint is a solution.
4. Direct supported unattended transport works: implement its real reads, required reviews, order lifecycle, auth renewal, and account restrictions behind the existing interfaces. Obtain write-semantics evidence without placing financial smoke-test orders during development.

Do not assume a daemon can be authorized simply because the MCP protocol exists. MCP transport support and permission for this particular broker/account/operation are separate facts.

## 3. Blocker-to-solution map

### A. Attended-only connector and missing daemon client

**Files:** `broker/robinhood.py`, `broker/base.py`, `cli.py`, `lifecycle_actions.py`.

Build a named transport factory that is shared by readiness AND the actual running service. It must return the same configured, supported client; readiness cannot certify one client while execution uses a placeholder or a different identity. Resolve the exact broker account ID privately, not solely a suffix used for display.

Implement authenticated reads first. Add required reviews and mutations only according to the discovered and authorized contract. A broker review response is not automatically an execution token. Do not invent one where the broker uses different semantics; preserve warnings/checks without fabricating approval evidence.

If a usable unattended Robinhood integration is unavailable, the alternative is a broker-supported trading API, not more changes to the prompt. Alpaca documents programmatic orders, client IDs, order lookup, order updates and native equity bracket/OCO orders. It is an alternative for separate owner evaluation, not a silent account migration or a same-day funding guarantee. Retain Massive for data and avoid paid routing/data upgrades. No funds are to be moved by the coding agent.

### B. Advanced orders and whole-account reconciliation

**Files:** `broker/base.py`, `reconcile.py`, `service.py`, adapter snapshot assembly.

`BrokerCapabilities.can_prove_whole_broker_reconciliation` currently requires `supports_advanced_order_read`. Replace the assumption that a particular endpoint name is essential with an evidence-backed coverage contract, after review and regression tests.

The invariant is complete visibility of every order family that can change the selected account's exposure or encumber funds/shares. Obtain all pages of working orders across dates, including old GTC orders, stop/conditional parents, children and pending cancellations. Same-day-only history is insufficient. Resolve non-equity exposure in the target account without treating other personal accounts as liquidation authority.

Coverage states should distinguish:

- complete via a dedicated endpoint;
- complete via a general endpoint, with broker-backed evidence that it includes parent/child/conditional records;
- not applicable, with authoritative evidence that the account cannot contain the family;
- unknown or incomplete, which still blocks new exposure.

Zero results, no position, a manual statement, an endpoint name, and a public marketing page do not prove complete coverage. Do not set an unavailable family to zero. If the provider cannot prove coverage through any supported path, retain the blocker.

### C. Client-reference recovery

**Files:** broker adapter, `execution.py`, `reconcile.py`, `state.py`.

A special lookup endpoint is not intrinsically necessary if fully paginated order history exposes the exact original broker-preserved `ref_id`/client ID. Preserve the intent before submission and store the returned broker order ID as soon as known. Build an exact reference index from authoritative records, checking account, instrument, side and other immutable request fields.

Never match only on ticker, quantity, price, or time. Never infer safe non-submission from a briefly absent record in eventually consistent history. A local UUID does not provide broker-side idempotency.

After timeout, quarantine the intent, reserve possible exposure, reconcile, and send an exception. Only resume when the original request is conclusively resolved. If no supported exact-ID recovery is possible, report that concrete provider gap; do not retry an ambiguous buy to make the bot appear autonomous.

### D. No atomic bracket/OCO

**Files:** `protection.py`, `exits.py`, `lifecycle_actions.py`, `execution.py`.

Do not equate absence of native brackets with impossibility of regular-session automated trading. Conversely, do not equate a software stop workflow with atomic protection.

Design a separately reviewed `sequential_verified` protection mode: each confirmed fill delta creates a durable obligation; submit the supported broker-held regular-hours protective stop; verify actual accepted working quantity; block additional entries while quantity is uncovered; track a measured maximum uncovered interval and escalate/recover on failure. Record the non-atomic interval and residual outage/gap risk explicitly in the owner mandate. Keep premarket entry restrictions unchanged.

For target/time exits, coordinate cancellation of the protective sell, resolve any cancellation/fill race, refetch remaining position/reserved quantity, then place only the remaining permitted exit. No independently competing stop and take-profit sells unless the broker supports their linkage. Do not silently widen stops, oversell, or pretend a halt can be exited on a deadline.

Native brackets, when available, still require tests for partial entry, exit activation, and both-exit race behavior. They do not guarantee the stop price.

### E. Production notifications

**Files:** `notifications.py`, `cli.py`, `service.py`.

The JSONL outbox is storage, not an iPhone delivery path. Do not make a proprietary Codex push bridge the only acceptable transport when no supported posting interface is available.

Use an owner-approved existing channel. The concrete zero-new-subscription candidate is Gmail API through a locally authorized client. Implement `NotificationSink` with bounded timeouts, an outbox worker independent of order execution, bounded retries/deduplication, and a stored provider message ID. Standard Gmail API use is currently no additional cost within its quotas; disable paid expansion/fallback and keep rate limits low.

A Gmail send response is provider acceptance, NOT proof that an iPhone displayed the message or that Shian read it. Use receipt levels: `LOCAL_STAGED`, `PROVIDER_ACCEPTED`, and a one-time channel-test `OWNER_CONFIRMED`. Store provider, destination fingerprint, event ID/payload hash, notification-route version, receipt ID and timestamps. Test actual phone arrival once through the owner flow; ordinary trade notifications do not require approval.

After a route change, an old local JSONL hash must not satisfy delivery readiness. During notification failure, preserve exit/reconciliation work; queue and escalate, and apply the approved new-entry pause policy. Do not block exits on email or an LLM response.

### F. Risk, score and liquidity provenance

**Files:** `config/full_live.json`, `policy.py`, `risk_runtime.py`, `POLICY_MAP.md`.

Preserve the active strategy's established dollar loss lock and profit-floor rules. The percent-risk overlay is checked in but explicitly staged; its existence is not proof of prior user approval. Spread/depth and score cutoffs are genuinely null in the reviewed configuration. Do not import synthetic test values as approved thresholds.

Produce ONE readable effective-policy diff: account binding, usable-capital boundary, sizing formula, normal/A+ risk, open/pending/stress reserves, daily/weekly/drawdown behavior, sessions, spread/depth and score decisions. Trace each value to deployed/approved policy or label it a new proposal. Have the owner approve that consolidated version through the supported setup flow, not each subsequent trade. Reuse existing verifiable approvals instead of asking the owner to repeat known facts.

Optional strategy paths should not hold the entire implementation hostage: A+ sizing can be explicitly disabled rather than inventing its missing thresholds. Do not remove material entry standards just to eliminate nulls. Technical timeouts/telemetry defaults may be engineering choices when they do not silently expand financial authority. Maintain explicit policy-version provenance.

### G. Holiday data and missing production providers

**Files:** `massive_adapter.py`, `market_data.py`, `pipeline.py`, `cli.py`.

The existing adapter reads the local Titan SQLite producer; it does not establish that a direct Massive stream is running. Verify the producer remains alive through the old-writer handoff. Stopping a legacy trader must not inadvertently stop the data source the new trader needs.

Reuse existing Massive credentials and entitlements. Earlier connected tests established stock quote/bar access, not local streaming performance. Wire live quotes/bars and independently sourced broker tradability/instrument evidence. Top-of-book NBBO sizes are not full Level 2 depth; preserve source semantics and strategy-specific requirements.

Separate installation/service readiness from session entry eligibility. While the market is closed, run authorized read-only reconciliation/health checks and report `WAITING_FOR_SESSION`; do not claim live trade readiness. Once required live evidence and completed bars arrive, entry eligibility can be recomputed automatically inside the approved session. Do not falsify Friday's timestamps or loosen freshness to accept holiday snapshots. No data upgrade is justified solely by a closed market.

## 4. Additional source-level findings to fix

### P0/P1: readiness clock is sampled before blocking reads

`cli._machine_readiness` fixes `current` from the supplied `now`, then performs broker reads, persists/reconciles, queries the market store, and calculates ages against that same earlier time. A real response stamped at receipt 400 ms later can therefore have an age of -0.4 seconds. `ReadinessEvidence.blockers` rejects negative ages. The reviewed readiness tests use a static `NOW` and do not exercise this normal network-delay scenario.

Inject a clock. Record probe-start/probe-end separately. Re-sample UTC after each blocking read and again for the final assessment; reconcile using post-read time. Keep original provider event and receive timestamps unchanged. Use monotonic durations for timeouts and detect real clock jumps. Never clamp negative ages to zero as a shortcut.

Regression tests: a successful response received after probe start; a slow multi-page read whose earliest page becomes stale by probe end; a genuine future timestamp; clock rollback; reconnect with stale data. Freshness is checked again at the order boundary.

### P0: legacy config absence is treated as disabled

`cli._probe_legacy_heartbeat` returns `old_writer_disabled=True` when its TOML file is absent, and also relies on PAUSED/DISABLED text for positive evidence. A missing/edited configuration file does not prove that an already running legacy process or in-flight order has stopped.

Require a retirement record for the actual legacy writer: scheduler disabled, in-flight work drained/reconciled, writer process identity quiesced, and exclusive ownership at the shared order gateway. A file lock held only by the new code does not fence an older program that never acquires it. Make all automated broker writers go through one enforceable account-level boundary before cutover. Do not interfere with manual activity; detect and reconcile it.

Regression tests: missing TOML with a live old worker; paused TOML with an in-flight old submission; duplicate service; abandoned lease; restart after a partial fill.

### P1: notification receipt provenance is too weak

`cli._machine_readiness` currently checks for a nonempty recent READINESS delivery receipt after enabling a nonlocal route in config. It does not bind the receipt to that route/destination or prove the receipt's assurance level. An old locally staged hash could be misclassified after a route switch.

Apply the structured receipt design in section E and invalidate channel readiness on route/destination/config changes. A receipt hash proves integrity of a record, not provider acceptance or end-device delivery.

## 5. Implement in dependency order

1. Collect full, read-only authenticated inventory and exact permission provenance; compare client visibility and test the installed Codex version. Use the new helper. No broker writes.
2. Implement the supported broker client plus exhaustive account/order/ref-ID reads; use the same factory in readiness and service. Resolve the provider decision before further release packaging.
3. Integrate verified sequential/native protection and closeout, real market/tradability providers, a separate notification worker, and the three source-level corrections above.
4. Consolidate policy provenance and only genuinely new owner decisions. Keep no-extra-cost and existing financial-scope restrictions.
5. Re-run the original suite plus new regression/contract tests against captured, redacted real response shapes. Offline test success is not a live transport test.
6. Rebuild the release from the new exact commit; old release hashes and installed trees no longer cover new code. Stage without trading; perform the supported owner-controlled cutover only when operational evidence is real. Do not manually edit the installed database or flip boolean blockers.

Produce a concise result for each component: implemented, locally exercised, authenticated read verified, production integration verified, or unresolved with the exact provider/owner action. No fake flatness, no claimed phone delivery from a local file, no claimed daemon from a class name, and no claimed profitability from passing tests.

## 6. Sources and scope of confidence

Primary documentation checked for this review:

- Robinhood Agentic Trading overview: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Robinhood Trading with your agent: https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- Robinhood Stop order: https://robinhood.com/us/en/support/articles/stop-order/
- OpenAI Codex MCP configuration and OAuth: https://developers.openai.com/codex/mcp/
- OpenAI Codex issue #28858, reported CLI 0.140.0 pagination behavior (not a confirmed diagnosis of this Mac): https://github.com/openai/codex/issues/28858
- MCP tool discovery/pagination: https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- MCP authorization: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- Massive stock streams: https://massive.com/docs/websocket/stocks/overview
- Gmail send: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
- Gmail quota/pricing: https://developers.google.com/workspace/gmail/api/reference/quota
- Alpaca order semantics: https://docs.alpaca.markets/us/docs/orders-at-alpaca

The reviewed PR reports 255 offline tests passing and a paused installation. Those are reported results, not independently reproduced here. The 17 pagination-helper tests are the only tests independently executed in this contribution. The live broker connection remains unverified from this environment. These limitations define what still needs to be completed, not a requirement for an artificial pilot or indefinite delay.
