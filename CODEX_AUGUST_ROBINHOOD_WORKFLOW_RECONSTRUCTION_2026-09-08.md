# August Robinhood workflow reconstruction for PR #8 review

Prepared September 8, 2026 for review in `Harpcity33/trader-brain` PR #8.

This is a read-only historical reconstruction. It adds no execution authority,
does not change the broker adapter, automation, risk policy, configuration, or
deployment, and must not be used as permission to restore an old order path.
The account is identified only as ending 7153. Secrets and the full account
identifier are intentionally omitted.

Review package:

- this narrative reconstruction;
- [`validation/forensics/2026-09-08/AUGUST_ROBINHOOD_EVENT_TIMELINE.json`](validation/forensics/2026-09-08/AUGUST_ROBINHOOD_EVENT_TIMELINE.json), the machine-readable event and source-fingerprint ledger;
- [`validation/full-live/2026-09-08/ROBINHOOD_ROUTE_DECISION.json`](validation/full-live/2026-09-08/ROBINHOOD_ROUTE_DECISION.json), the separate current-route decision; and
- [`automations/robinhood-momentum-engine/automation.toml`](automations/robinhood-momentum-engine/automation.toml), the retained attended automation definition.

## Review conclusion

The last evidenced August workflow was mixed rather than fully autonomous:

- HSAI had a user-requested entry and a human approval for the preceding $19.05
  review. Codex then changed the executable tuple to $19.08 without redisplay
  or reconfirmation, placed the stop, and later exited during a scheduled run
  without a new human message.
- USDE had a broker-classified user entry, followed by a scheduled Codex exit
  without a new human message.
- GIPR had broker-classified user entry and exit orders while the automation was
  paused. No Codex mutation call exists in the retained task history for that
  interval.

The HSAI and USDE scheduled exits prove that the historical backend accepted
orders marked `placed_agent:"agentic"`. They do not prove that unattended
submission complied with the advertised tool contract or remains supported.
The same August records show review and cancel instructions requiring explicit
user confirmation. The contemporaneous automation's standing-authority clause
conflicted with those instructions.

The supported retained pattern is therefore:

```text
fresh broker evidence
  -> exact review
  -> complete preview, alerts, and disclosure displayed
  -> new explicit confirmation for that exact tuple
  -> place or cancel
  -> exact-reference and quantity reconciliation
```

The historical `scheduled review -> mutation without a new user message`
pattern must not be treated as supported or restored unless Robinhood supplies
a current, provider-supported contract that expressly authorizes it. Historical
acceptance alone is insufficient.

## Evidence method and limits

- Primary evidence is the retained JSONL history for the original Codex task.
  Its timestamps are UTC; all display times below are converted to
  America/New_York, which was UTC-04:00 for these dates.
- User-role heartbeat records are scheduler-generated inputs, not human
  messages. The retained task explicitly documents this distinction.
- Broker `placed_agent` provenance is used when available. A fill alone is
  never used to infer which actor submitted an order.
- The GIPR orders are available through a later authoritative broker order
  read. That proves IDs, tuple, fill, and broker actor classification, but it
  does not identify which Robinhood user interface or separate user channel was
  used.
- No live broker capability or authentication test was performed for this
  reconstruction. Current-route findings remain those already recorded in
  `validation/full-live/2026-09-08/ROBINHOOD_ROUTE_DECISION.json`.

## Contemporaneous instructions

The retained August 21 `robinhood-momentum-engine` definition instructed every
scheduled wake to reconcile account, positions, orders, protection, and P&L
before discovery; use Robinhood pre-trade review; protect fills; and avoid
overnight exposure. It also contained this incompatible automation-level rule:

> Standing autonomous authority is already present. Once an exact
> contemporaneous trigger fires and Robinhood review is clean, submit without
> asking the user for another confirmation.

The contemporaneous Robinhood review guide separately said that reaching the
review step required presenting the preview and obtaining explicit confirmation
before `place_equity_order`, even when `order_checks` was empty. The cancel tool
said to always confirm with the user before calling it. Those requirements are
present in the August 20 HSAI and August 21 USDE records, before the later
attended-policy rewrite.

The historical automation prompt and connector guidance therefore did not form
one internally consistent authorization contract. This report records what
happened without converting that conflict into current authority.

## HSAI — August 20, 10:17–10:25 ET

### Approval and actor sequence

1. **10:17:44 — human instruction.** The user said, `Buy HSAI at $1000 make an
   exit plan for me`.
2. Codex called account, portfolio, equity/option position, equity/option order,
   and P&L reads, followed by HSAI quote, price book, tradability, earnings,
   fundamentals, technicals, and historical-bar reads.
3. **10:20:28 — review.** Robinhood reviewed a 52-share regular-hours BUY LIMIT
   at $19.05, GFD. `order_checks` was empty. The response nevertheless required
   preview presentation and explicit confirmation.
4. **10:21:05 — presentation.** Codex displayed the quantity, limit, maximum
   cost, intended $18.78 stop, estimated risk, catalyst, exit plan, and exact
   market disclosure, then asked for confirmation.
5. **10:21:23 — scheduled heartbeat.** The scheduled run did not place the
   order and said confirmation was still required.
6. **10:22:06 — human approval.** The user said, `Submit the trade`.
7. Codex refreshed portfolio, orders, positions, quote, and book. Robinhood then
   reviewed a changed 52-share BUY LIMIT at $19.08. The changed review was not
   separately displayed or reconfirmed before placement.
8. A first local wrapper attempt failed with `crypto is not defined` before a
   broker invocation. A later call placed the order and an exact broker read
   confirmed the fill.
9. Codex reviewed and placed a 52-share $18.78 GTC stop-market. There was no
   separate human confirmation for that protection order.
10. **10:24:23 — scheduled heartbeat.** With no intervening human message,
    Codex reconciled the position and stop; refreshed quote, book, bars,
    technicals, VWAP, and ATR; detected a high-volume VWAP/structure failure;
    canceled the stop; reviewed a 52-share $18.87 sell limit; placed it; and
    reconciled the account flat.

### Broker identifiers

| Lifecycle | Client reference | Broker order ID | Execution ID | Broker result |
|---|---|---|---|---|
| Entry | `ddea0252-97c4-4e5e-ae14-a166dc84ccf2` | `6a870dad-f824-4c41-963e-2875790ad9d7` | `6a870dad-216e-4de0-9d82-d9d8ed522248` | 52 at $19.08, 10:22:37.569 ET |
| Protection | `232f799c-6d34-4a82-8c51-2a2298a165ba` | `6a870dbc-2e05-43ce-be7e-ab35169e2e17` | none; canceled | 52-share stop-market at $18.78 |
| Exit | `ed347704-2650-4d43-93e4-34e8162eec37` | `6a870e53-cf13-48e9-838a-ec6494f66d6d` | `6a870e53-a022-40d4-8c62-00b4a81ad9a8` | 52 at average $18.8701, 10:25:23.212 ET |

### Finding

HSAI does not prove autonomous discovery or autonomous entry. The user chose the
symbol and sent a placement instruction. It does prove scheduled, agent-mediated
position management and exit without another human message. The changed entry
tuple, protection placement, stop cancellation, and exit would each require a
new exact approval under the current policy.

Primary JSONL anchors: 10973, 10980–11060, 11065–11095, 11101–11146,
11155–11213.

## USDE — August 21, 15:15–15:19 ET

### Approval and actor sequence

1. **15:15:59 — scheduled heartbeat.** Initial account reconciliation found
   the account flat with no position or working order.
2. **15:16:28 — external/user entry.** A $900 market buy filled 118.655240 USDE
   shares at $7.585. Later exact broker reads classify it as
   `placed_agent:"user"`. No Codex placement call precedes it in the scheduled
   run.
3. **15:18:29 — new scheduled heartbeat.** Its only user-role input was the
   scheduler heartbeat. Codex reconciled account, portfolio, positions, orders,
   and P&L; identified the unprotected USDE position; read the exact entry
   order; and refreshed quote, book, bars, tradability, and fundamentals.
4. Codex called `review_equity_order` for a full-quantity regular-hours market
   sell. The returned guide required presentation and explicit confirmation.
   No presentation or human approval followed.
5. A first local wrapper call failed before broker invocation. At **15:19:42**,
   a later call placed the sell, classified by Robinhood as
   `placed_agent:"agentic"`. Exact-ID polls confirmed the fill and later reads
   confirmed position zero and realized net P&L of +$34.21.

### Broker identifiers

| Lifecycle | Client reference | Broker order ID | Execution ID(s) | Broker result |
|---|---|---|---|---|
| Entry | not exposed in retained Codex record | `6a88a40c-2633-4744-a2fa-2a02b923ab96` | `6a88a40c-e998-4f42-ba86-78d037f9f177` | `placed_agent:"user"`; 118.655240 at $7.585 |
| Exit | `1ded7b5a-064e-4606-9515-b7f316fa46d0` | `6a88a4ce-c9b4-412e-807d-38d5813a266c` | `6a88a4ce-b5ba-4671-ac4c-98a1d069b0c4`; `6a88a4ce-d96c-4bef-b7a3-4b93335449dd` | `placed_agent:"agentic"`; average $7.8733 |

### Finding

USDE's entry was not submitted by Codex. Its exit was submitted by Codex in a
scheduled run without an intervening human message, despite the review guide's
explicit-confirmation requirement.

Primary JSONL anchors: 27993–28005, 28048–28092, 28096–28115, 28141–28149.

## GIPR — August 24, 15:40–15:45 ET

### Automation and actor sequence

1. At **10:50:06**, the user instructed Codex to stop the automation until it
   was fixed.
2. The installed automation was changed from ACTIVE to PAUSED and the pause was
   completed at **10:55:57**. Repeated readbacks before and after the GIPR
   window showed PAUSED with the same installed-file hash.
3. There is no scheduled Robinhood review/place/cancel sequence in the original
   task during 15:40–15:45. The only nearby Robinhood call was a later read-only
   account request.
4. A later exhaustive broker order read records both GIPR sides as
   `placed_agent:"user"`.

### Broker identifiers

| Lifecycle | Broker order ID | Execution ID | Broker result |
|---|---|---|---|
| Entry | `6a8c9e1c-1ee4-4120-b27f-eb2c93a75366` | `6a8c9e1c-e58f-43cd-abbb-35e0fba3275c` | `placed_agent:"user"`; 1,200 at average $0.8474; created 15:40:12.681904 ET |
| Exit | `6a8c9f41-ec8a-48f1-b9c5-b509074f64fe` | `36304c1b-b2b4-4eae-a22c-de3fbb24dd19` | `placed_agent:"user"`; 1,200 at average $0.7714; created 15:45:05.294372 ET |

### Finding

Nothing in the retained original-task history supports attributing GIPR to the
Codex scheduler. The automation was paused and both orders were broker-classified
as user-originated. The exact user interface cannot be recovered from these
records.

Primary JSONL anchors: 32207–32347, 35777, 35959, 36648–36765, 36898, 46036.

## First evidenced behavior change

The transition was not first caused by the file carrying an August 27 23:20 ET
metadata value:

1. **August 24, 10:55:57 ET — first operational change.** The live autonomous
   heartbeat was actually paused. This is the first evidenced change from the
   HSAI/USDE scheduled-risk-action behavior.
2. **August 25, 20:05 ET — first retained attended-only policy artifact.** V8
   explicitly rejected silence, standing authority, prior requests, and another
   automation as confirmation. It remained staged and PAUSED.
3. **August 27, approximately 10:54 ET — first installed ACTIVE attended
   version.** `titan_live_attended_compat_2026-08-27_v1` required an exact review
   and explicit confirmation for entries, protection, cancellation, and exits.
4. **August 27, 17:20 ET — premarket V2 was copied live.** It retained the same
   per-mutation attended requirements.
5. The currently retained file's `updated_at = 1787887228000` resolves to
   August 27 23:20:28 ET, but task history shows that value being assigned in a
   later August 29 scheduling-representation patch. It is not reliable as the
   onset time for attended behavior.

## Relationship to the current PR #8 route decision

This reconstruction narrows the interpretation of the current route evidence;
it does not overturn it:

- Historical broker acceptance proves that agentic order placement existed for
  this account and connector in August.
- The retained August tool descriptions already required confirmation for the
  exact operations that were submitted unattended.
- The current authenticated inventory records materially the same explicit
  confirmation requirement for review, placement, and cancellation.
- Therefore the historical calls are evidence of observed capability, not a
  provider-supported authorization contract for an unattended daemon or
  scheduler.

`UNATTENDED_UNSUPPORTED_ON_VERIFIED_ROUTE` should remain scoped to the currently
verified route and contract. It should not be broadened to claim that Robinhood
has never supported agentic execution, and it should not be weakened based only
on historical fills. A different conclusion requires current authoritative
provider documentation or a separately supported contract, not instruction
stripping, automatic confirmation, or restoration of old code.

## Requested Chat review

Please review this package against the current PR #8 implementation and answer:

1. Does any current, documented Robinhood contract distinguish account-level
   agent authority from the per-call confirmation language observed here?
2. If yes, what supported authorization artifact, endpoint, client, order-family
   coverage, exact-reference recovery, cancellation, and renewal semantics prove
   it without a live mutation experiment?
3. If no, preserve the attended connector path and ensure no strategy or
   scheduler interprets the August standing-authority prompt as current
   permission.
4. Correct any architecture or documentation that describes the entire HSAI
   lifecycle as attended. Only the initial displayed $19.05 review received a
   later human `Submit the trade`; the changed $19.08 tuple, stop, cancellation,
   and exit did not each receive current exact confirmation.
5. Keep the current financial restrictions and fail-closed reconciliation,
   protection, and closeout invariants unchanged. Do not clear a blocker by
   changing readiness booleans or bypassing provider confirmation.

## Local source manifest

The raw original-task logs are intentionally not copied into GitHub because
they contain unrelated private task content and encrypted model records. These
hashes allow the owner to verify the retained sources locally.

| Source | Relevant scope | SHA-256 |
|---|---|---|
| Selected records from original Aug. 18 task JSONL, `rollout-2026-08-18T20-21-07-01a01764-...jsonl` | HSAI, USDE, automation pause, GIPR later broker read, attended-v1 installation; exact selected-record list is in the structured ledger | `e9186399ebdd89d5ac0d7ba77baf5efc9629643b17fae275a20a96d91cb57982` |
| Aug. 21 Sol Ultra live-prepromotion `automation.toml` | Contemporaneous standing-authority instructions | `1eb17de6ad2eb572a8ce1e10ff9ced748921ce398ba376425a7f26bd53aa1d1f` |
| V8 attended-review `automation.toml` | First retained attended-only staged policy | `888ac7c6edf155bb890a6b2522aa3ee259639ea64dd3a73157765a663cf5aacc` |
| Aug. 27 v1 installed-backup `automation.toml` | First active attended compatibility policy | `9e2a20f6591fb99ca9b4f23e611bfc51136b5e3c0b8c79b3badff2f65c142a08` |
| PR branch `automations/robinhood-momentum-engine/automation.toml` | Retained V2 attended policy | `ea039335e94daf48f9c4bf5f03063609797cbe40540921de473a818a2bd220a6` |
| Selected record 40527 from Aug. 29 task JSONL, `rollout-2026-08-29T11-35-59-01a04e29-...jsonl` | Later metadata rewrite evidence | `7fe18daca4db9077b148d35ed2ae7878443488b2773e724424575d076a1de982` |

## Change and deployment status

- Repository change: the narrative report and machine-readable forensic ledger
  only.
- Executable code changed: no.
- Tests required: document/JSON/TOML repository validation and diff checks only;
  no runtime behavior was changed.
- Installed release changed: no.
- Automation/configuration changed: no.
- Broker review, placement, cancellation, or authentication invoked: no.
- Notification sent: no.
- Full-live coordinator or notification worker started: no.
- Trading authority changed: no.
