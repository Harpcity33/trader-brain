# PR #8 follow-up: connect real providers and remove the blocking data hot path

Reviewed September 8, 2026 for Shian Harper.
Repository: `Harpcity33/trader-brain`.
Reviewed head: `9fdcab051f4b176dca10cd78c6e5c62fe2cfb669`.
Branch: `codex/full-live-2026-09-08`.

**Contribution type: targeted source review and implementation directions. This contribution does not change executable code, run the 368-test suite, install a release, authenticate an account, approve risk values, retire a writer, or activate trading.** The reported 368 offline passes belong to Codex's deployment evidence, not an independently repeated test run here.

The objective remains full-live autonomous operation within the existing approved financial scope, on the existing computer and subscriptions, with zero incremental paid services. This does not reinstate the rejected tiny pilot or add a paper-session waiting period. Actual broker/platform confirmation requirements remain binding.

## 1. What is now resolved, and what is not

The new deployment evidence reports fixes and regression coverage for the earlier readiness-clock, missing-legacy-TOML, and route-receipt findings. The independent notification worker, provider composition interfaces, exact-account bindings, order-family coverage model, and staged policy diff are meaningful progress.

The remaining core problem is concrete: the newly built adapters still need real provider implementations, authorization, and composition on the Mac. A class accepting an injected authenticated callback is not the callback, its credentials, or a working network connection. `scripts/titan-full-live` still enters the generic CLI; the release needs an actual reviewed provider-assembly entrypoint, not another undocumented requirement to inject objects externally.

Do not produce another release whose only progress is more interface declarations and unchanged unavailable provider bindings. The next review should contain authenticated read evidence from the exact executable provider objects used by the service, or a precise provider incompatibility and owner action.

## 2. Close the Robinhood capability investigation with a definite outcome

### Evidence from the latest audit

`AUTHENTICATED_MCP_INVENTORY.json` and `DEPLOYMENT_STATUS.md` report:

- Codex `0.151.0-alpha.7.2`, advertised Robinhood server `1.4.0`, OAuth.
- 44 advertised/client-visible tools; configured `get_advanced_orders` was not advertised.
- No divergence between the app-server inventory and the client-visible map.
- The app-server did not expose the Robinhood origin server's raw `tools/list` cursor. Synthetic/helper pagination tests are not an authenticated origin-pagination test.
- The advertised place/cancel/review contracts explicitly require user confirmation.

**The historical first-page bug is no longer an evidenced explanation for this installation. Do not keep pursuing it as if it were the established cause.** Equally, do not overstate the audit as directly proving every origin-server page was inspected.

### One remaining provider-contract check, not an endless discovery loop

Compare the configured origin URL with Robinhood's documented endpoint:

```text
https://agent.robinhood.com/mcp/trading
```

Through normal, provider-supported authorization for the intended local client, capture raw `initialize` and all `tools/list` pages, with server instructions and unchanged descriptions/schemas. Record the exact endpoint, client/server version, granted scopes, account binding, and whether metadata originated at Robinhood or a client-facing gateway. Keep secrets out of logs and GitHub. Use supported OAuth rather than copying credentials from Codex or ChatGPT. Do not invalidate a current session casually.

If this direct authorized path advertises the same per-mutation confirmation requirements, record **UNATTENDED_UNSUPPORTED_ON_VERIFIED_ROUTE**. Do not strip instructions, change approval modes to erase them, imitate owner confirmation, use an undocumented stock endpoint, or write a permissive proxy. A different supported route must have its own demonstrable provider/client contract; a new token alone does not change permission.

Robinhood's public documentation says agents can act without per-trade confirmation, while the observed route says otherwise. This discrepancy requires an authoritative provider answer or genuinely different supported route. A support question is supplied in section 8; it has not been sent.

### Supported alternative for owner consideration

If Robinhood does not supply the required unattended route, evaluate Alpaca's **Trading API** as the execution provider, keeping Massive, the Mac, strategy code, and notification worker. Its published API supports programmatic orders and exact client-order-ID lookup. Use a separately approved account and its supported credentials; do not silently switch accounts or transfer funds. Verify actual account features, fees, funding availability, and protection semantics before making deployment claims.

Do not add a paid data or AI plan merely for this change. Commission-free standard retail API trading is advertised, but regulatory/other applicable fees and market costs remain. Premium features are not automatically part of the zero-added-cost design. Account opening and funding are not guaranteed on the target date.

## 3. P1: the generic broker adapter requires unverified provider primitives

**File:** `src/titan_brain/live/broker/production.py`.

The `ProductionAccountBase` docstring requires a **provider-issued immutable snapshot token**. Order-family pages must match that token. Separately, `SupportedProductionBrokerAdapter.review_equity_order` and `place_equity_order` reject reviews without `broker_bound=True` and a broker review ID.

Those are strong guarantees when a provider actually supplies them. The inspected evidence has not established such primitives for the observed Robinhood route. Do not assume that switching to an otherwise supported Trading API will supply them either. The public Alpaca order API describes direct order submission; it does not establish these extra cross-endpoint snapshot/preview guarantees.

### Required correction: represent real provenance, without manufacturing stronger evidence

Separate two review types:

1. `BrokerNativeReview`: preserve actual required broker review, receipt, warnings, expiry, and confirmation semantics when exposed.
2. `LocalPreflightDecision`: a locally computed, policy-bound decision with exact tuple and current evidence, explicitly NOT a broker-issued approval or execution token. This type is usable only for a supported broker contract that permits direct API orders; it must never replace mandatory Robinhood review/confirmation.

Separate two account consistency models:

1. `ProviderSnapshot`: use genuine provider snapshot/version tokens when documented and present.
2. `CollectedObservation`: retain request start/end times, per-family completeness, provider timestamps, actual order IDs, position/order reservations, and event-journal watermarks. Re-read material state around critical boundaries, detect changes or incomplete collection, and reconcile again. A local hash identifies the collection; it does not make it an atomic broker snapshot.

Model the real provider behavior and reject unresolved contradictions. Do not invent immutable tokens, mark local checks broker-bound, or silently weaken a contract while leaving its strong label unchanged.

Add contract tests using redacted real response shapes, including providers with no preview ID or immutable multi-endpoint snapshot, moving orders during pagination, missing pages, and required review/confirmation preservation.

### Related P0: exhaustive history absence is not necessarily authoritative rejection

The exhaustive-history branch of `lookup_equity_orders_by_client_ref` currently sets `confirmed_absent_client_refs` for requested references missing from the collected records. That is only safe when the provider guarantees the relevant completeness and negative-lookup semantics. Repeating an eventually consistent empty response twice still does not prove that an order was never accepted.

Preserve exact-reference positive matches. For an unobserved ambiguous request, use `NOT_SEEN_YET` or the equivalent unresolved state unless authoritative negative semantics exist. Keep possible exposure reserved and do not resend. Test delayed publication after two empty reads; no duplicate submission or premature release of risk is allowed.

This is a source-level contract finding, not an observed live duplicate-order incident.

## 4. P1: stream processing currently waits behind full sequential REST hydration

**File:** `src/titan_brain/live/massive_adapter.py`, `MassiveRestStreamSource.hydrate_cache`.

For each candidate symbol, the method performs a historical-quote REST request and a full-session minute-bar REST request before moving to the next symbol. Only after all symbols does it drain the stream. With `max_active_candidates=64` in `config/full_live.json`, one full hydration call can therefore make **128 sequential REST requests**, plus tradability calls, before reading stream events.

At an illustrative 100 ms per REST response this alone is 12.8 seconds; this is arithmetic, NOT a measured broker/network benchmark. It conflicts with the intended fast execution and can age evidence past the configured five-second quote limit. The actual number depends on the candidate count, and the frequency of hydration must be measured rather than assumed.

### Concrete implementation

- Run a continuously draining, bounded stream consumer independently of candidate discovery and REST backfills.
- Separate cold initialization/resynchronization from steady-state incremental updates.
- Fetch initial history only for newly watched symbols or genuine gaps; request the missing range rather than the complete session on every hydration.
- Use bounded concurrency for independent reads within provider limits. Keep dependent broker mutations serialized and preserve priority/capacity for reconciliation and exits.
- Maintain per-symbol readiness, so one cold or unavailable symbol is skipped without stopping protection or every other candidate.
- Use actual post-response receipt timestamps. The method currently reuses one `current` value from the start for later REST results and drained stream events. Preserve venue timestamps separately; do not relabel them to make stale evidence look new.
- Preserve completed-bar causality. Do not mix per-second `A` events with per-minute `AM` events in a minute sequence without explicit aggregation. Both are currently accepted while `_bar` derives sequence from `start_at // 60`; add tests proving second bars cannot masquerade as completed minute bars.

Acceptance evidence: cold-start REST call count, steady-state calls, queue age/backlog, per-symbol freshness, gap/reconnect behavior, and p50/p95 local processing times. Show that an artificially slow REST source cannot stop stream ingestion or broker protection work. Do not reduce freshness standards to hide the delay.

### Data units

Massive changed stock quote bid/ask sizes from round lots to shares effective November 3, 2025. Do not multiply current Massive stock quote sizes by 100 based on older examples. Preserve source/version/unit evidence for historical replay. NBBO/top-of-book shares remain different from full order-book depth.

## 5. Replace injected placeholders with a concrete local provider launcher

**Files:** `composition.py`, `discovery_composition.py`, `massive_adapter.py`, `notifications.py`, `scripts/titan-full-live` and the installed launchd definitions.

Implement a reviewed, release-included local assembly module. Both readiness and the actual coordinator/notification worker must instantiate the same provider classes and immutable nonsecret binding profile from it. It must include concrete implementations of each required protocol, not synthetic lambdas or a test-only injection path.

Responsibilities:

- Massive: read the user's existing provider-supported API credential through an explicitly configured private credential store, construct real REST and streaming clients, and prove the permitted feeds locally. A credential loader is a legitimate missing implementation; it is not the user's job to write a Python callback.
- Broker: use the verified supported client/contract from section 2. Map real response fields rather than synthesize strong broker guarantees to satisfy the adapter.
- Tradability: use the selected broker's actual instrument/account eligibility endpoint. Massive quotes do not establish broker tradability.
- Quality: wire existing deterministic scoring and structure calculation to fresh data, preserving strategy/risk boundaries. Do not require the owner to supply an imaginary external quality provider, nor fetch another model opinion for every tick.
- Gmail: authorize a locally owned desktop client, use least-required send scope, secure credentials outside the repository, implement supported renewal, and wire the existing sender/worker to that client.

A connected ChatGPT Gmail/Massive tool is not automatically a credential for an independent local service. Never export connector tokens or subscription credentials. The normal browser consent/keychain step may require the owner once; routine trading must not depend on continued owner interaction.

Record a redacted connection result for each actual component: implementation identity, credential source label (not credential), provider/account or destination binding, last successful authenticated read/health check, and exact error/action needed. Do not claim a live connection from a constructor succeeding.

## 6. Gmail OAuth: avoid a predictable week-later outage

Google's Python quickstart is a testing-oriented setup. External OAuth apps left in **Testing** receive refresh tokens that generally expire after seven days for Gmail scopes. Implement the documented deployment/consent configuration appropriate to personal use; do not assume the quickstart token is durable production authorization.

Use a supported desktop OAuth flow and the minimal scope required by the selected send operation, preserve client metadata and refresh state securely, and test refresh without logging tokens. Surface revoked/expired authorization as an incident. A renewal error must not silently stop exits.

The standard Gmail API currently has no additional charge within its free limits; sending/account limits still apply. No paid quota expansion or fallback is authorized.

Keep the existing distinction between provider acceptance and actual phone display. One owner-confirmed channel test can establish the destination once; normal fill/exit alerts should require only the configured delivery handling, not owner acknowledgment of every alert. Destination changes invalidate previous route evidence.

## 7. Close the remaining owner setup in one deliberate step

Use `CONSOLIDATED_POLICY_DIFF.md` as the source of the actual missing choices. Do not ask again for established constraints, and do not interpret full-live intent as approval of the staged percentage overlay or synthetic score/liquidity values.

Prepare ONE completed proposed policy with a plain-language diff and rationale, clearly separating established values from new recommendations. Include exact executable values for normal sizing/aggregate limits, score floors, spread definition, displayed-liquidity units and multiple, treatment of A+ sizing, sequential-protection risks if used, and the selected notification destination/failure policy.

The owner can approve or modify that version once. Optional A+ sizing can be explicitly disabled rather than hold the entire standard strategy hostage to missing optional thresholds. This is a suggested implementation simplification, not approval to remove required strategy filters or change financial risk.

Do not add the earlier rejected one-share/two-trade/$100 pilot proposal. Do not choose a new account, fund it, increase exposure, or apply a policy on the owner's behalf. Current financial authority and mandatory provider controls still apply.

Retire and drain the old writer only in the actual authorized cutover. Keep its independent market-data producer alive when required by the replacement. Do not retire it early to improve a readiness checklist.

## 8. Provider-support question (draft only; not sent)

> I use a Robinhood Agentic account with Codex CLI 0.151.0-alpha.7.2. The authenticated client reports Robinhood server 1.4.0. Its place and cancel tool descriptions and review output guide require explicit user confirmation for each mutation. Your Agentic Trading documentation describes operation without per-trade approval. Which supported endpoint, client authorization, or account setting provides that behavior for a locally running client? I am not requesting a bypass of confirmations. Please identify the supported contract, authentication/renewal process, complete order-history coverage including conditional orders, and client-reference lookup/negative-result semantics. Is `get_advanced_orders` expected on this version or is equivalent complete coverage supplied elsewhere?

Do not send account secrets in a support ticket. Attach a redacted tool inventory and exact wording if the owner opens the support request.

## 9. Next Codex deliverable

Work in this order rather than building more generic wrappers:

1. Resolve the origin/contract outcome for the current Robinhood route, with one supported direct read-only inventory/auth check or authoritative support clarification. If the route remains attended, report that exact provider decision rather than repeat the pagination hypothesis.
2. Correct the broker evidence model to fit the selected provider's documented capabilities, including conservative unknown-submission recovery. Preserve mandatory approvals.
3. Build the actual credential-backed local assembly module and exercise authenticated reads plus owner-approved notification delivery. Implement the hot-path decoupling concurrently; it does not depend on financial write permission.
4. Present the completed consolidated owner setup once. Apply only actually approved values through the supported procedure.
5. Re-run all affected and full tests. Rebuild/reinstall only after the real integration changes exist; publish what actually connected, not just a new hash. Financial smoke-test orders or scheduler activation are not authorized by this review.

The target remains a functioning full-live system. A provider integration result, bounded setup action, or owner broker decision is useful progress. Another paused release with all external callbacks still absent is not completion.

## Primary references checked

- Robinhood Agentic Trading overview and documented connection endpoint: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Robinhood Trading with your agent: https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- Alpaca Trading API order semantics: https://docs.alpaca.markets/us/docs/orders-at-alpaca
- Alpaca exact client-order-ID lookup: https://docs.alpaca.markets/us/reference/getorderbyclientorderid
- Alpaca retail API pricing/disclosures: https://alpaca.markets/algotrading
- Massive stock quote unit change: https://massive.com/blog/change-stocks-quotes-round-lots-to-shares/
- Gmail Python quickstart and deployment distinction: https://developers.google.com/workspace/gmail/api/quickstart/python
- Google OAuth refresh-token expiration: https://developers.google.com/identity/protocols/oauth2
- Gmail API quota/pricing: https://developers.google.com/workspace/gmail/api/reference/quota

Repository findings are pinned to the reviewed head above. All proposed refactors need implementation and regression/contract tests. No live latency measurement or independently executed full-suite result is claimed in this review.
