# Connection setup and remaining-risk diagnostic deployment

September 14, 2026 America/New_York. Installed at **2026-09-15 01:37:44 UTC**.
**New executable work is committed and installed PAUSED. Not activation-ready;
neither autonomous service is running.** No order, broker setting, credential,
paid subscription, service start or trading activation occurred in this update.

## New work, not the preceding package

- Real optional TWS `reqPnLSingle`/`cancelPnLSingle` collection on the existing
  authenticated read client, using exact `reqPositions` conIds, currencies and
  whole-share quantities. Normal account reads do not start subscriptions.
  Explicit `runtime.probe_position_valuations()` collects and tears them down.
- Generation, account, request, inventory, quantity, finite-value, receipt-age,
  reentrancy, disconnect, uncertain-dispatch and cleanup checks. Original
  position receipts must still be fresh when collection completes. A local
  receipt timestamp is never promoted to a broker valuation timestamp.
- Tested remaining mark-to-working-stop arithmetic with exact quantity
  coverage and positive execution/remaining-fee reserves. This is not yet an
  authorized production risk provider: coherent NLV epoch, source timestamps,
  complete coverage and remaining-fee evidence are still missing. The existing
  open-position entry blocker remains; no field was toggled to pass it.
- Owner-run Gmail Desktop OAuth enrollment: fixed Google endpoints, send-only
  scope, PKCE/state, bounded loopback callback, code-bound one-shot exchange,
  real-refresh verification path, strict token validation, create-only scoped
  native Keychain storage and redacted errors. Existing OAuth client loading
  now shares exact loopback URI validation instead of accepting a
  `localhost`-prefix lookalike.
- [Free owner setup procedure](GMAIL_FREE_OWNER_SETUP_2026-09-14.md) and a
  [source-grounded daily-provider contract review](DAILY_PROVIDER_CONTRACT_REVIEW_2026-09-14.md).
  No fabricated daily-feed producer was added to disguise an unavailable
  upstream contract. No financial-policy values were changed.

The prior 15%/10% policy, historical stream-ingestion work, protection/exit
engine and preceding package are not claimed as newly implemented here.

## Implemented, exercised, authenticated, running

| Component | Actual evidence | Limit |
|---|---|---|
| IBKR Portal | Authenticated Market Data Subscriptions UI: API access enabled; acknowledgement signed September 14; Non-Professional; subscriptions total USD 0 | The owner signed. No declaration was submitted by the agent. This does not prove fresh API quotes or consolidated-market entitlement. |
| Massive REST | Actual authenticated market-status read succeeded at 01:31:47 and 01:33:38 UTC | Not a completed live strategy run or a fresh quote/depth/bar joined to broker risk. |
| Massive WebSocket | Actual authenticated WSS handshake succeeded at both probes | No autonomous ingestion service was started in this update. |
| IBKR Gateway | Existing loopback endpoint authenticated the exact account ending 3103 | Authentication alone is not reconciliation or mutation authority. |
| New position diagnostic | One bounded base-account collection, including a received daily-realized callback, succeeded at 01:32:54 UTC; zero scoped equity positions and cash-plus-position value equalled NLV | No open position meant **no live per-conId `pnlSingle` subscription**. The per-position path is exercised in tests, not on live exposure. Full all-client coverage, current whole-broker flatness and coherent valuation were not proved. |
| Full installed provider probe | Two probes authenticated Gateway but timed out awaiting daily realized P&L; contract probe not reached | Availability is intermittent/unverified. The single successful diagnostic does not erase those failures. |
| Gmail setup | 24 new offline tests, synthetic loopback tests, mocked native Keychain calls; installed module `--help` exercised | No Desktop client supplied, Google OAuth performed, credential saved, route activated, test email sent or delivery observed. |
| Gateway precautions | API detail pane remained blank during fresh read-only inspection | Fresh unchanged-precaution verification was not obtained. No checkbox or Apply action was taken. |
| Autonomous coordinator / notification worker | Both launchd lookups returned service absent; database authority 0, PAUSED | Installed files and staged disabled plists are not running services. |

The raw bounded results, failures and exact installation receipt are in
[TEST_AND_DEPLOYMENT_CONNECTION_SETUP_2026-09-14.json](TEST_AND_DEPLOYMENT_CONNECTION_SETUP_2026-09-14.json).
Local database zero-counts are not broker flatness evidence. No command-client
connection or order operation was used for these diagnostics.

## Verification and release identity

- **1,172 tests in each environment:** bundled Python 61.245 seconds, PASS with
  two SDK-only skips; official IBKR SDK environment 61.196 seconds, PASS with
  zero skips. This is 40 additional tests, not just a rerun of the old package.
- Repository validator and `git diff --check`: PASS. Independent source review
  completed; the final position-receipt aging fix is included in the full rerun.
- Local executable commit: `b603bb2c08b549dcc6aae7dd766bcd4315116fec`.
- GitHub executable mirror: `3649cce9483a20f14bcb893770c86376abedde65`.
- Identical Git tree: `d7f13d1456cdd3201892bd887180a4871c48f3c6`.
  Published as a fast-forward without replacing the existing remote history.
- Two clean builds produced identical archive SHA-256:
  `13655b22b783eab36f039297418f66bf30a39457599c19fb09e26dbb852b2359`.
- Installed release:
  `a416164093b6bb70e12d21dd83aabdbb7e7050555831d572984c74c9c6cf650d`.
- Manifest file SHA-256:
  `c9d63fccd9a29609ee89b616a083d4b43bf0a7e7dda4c8e430880da8e3f29c6c`.
- Pinned official ibapi 10.50.2 / protobuf 5.29.5; installer verified 106 release
  files. Runtime generation **6**, activation null, authority **0**, no writer
  lease; audit chain valid. Risk ledger remains **not configured**.

Install root:
`/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`.
Later report-only commits do not change these installed executable bytes.

## Exact unresolved gates and owner steps

1. **Daily balance and flow contract:** current code demands midnight New York
   whole-account NLV and exhaustive flows aligned to a current valuation within
   five seconds. Documented TWS/Flex/transaction-history sources examined do
   not establish that combination. The owner's explicit instruction selected
   daily starting balance and the percentages; these timing details are the
   implementation's interpretation, not an IBKR promise. Either identify and
   verify a no-additional-fee source with those exact semantics, or obtain a
   specific owner amendment to a proved broker-supported measurement boundary
   and flow policy. Do not request approval of 15%/10% again, silently substitute
   previous-day loan value, infer transfers from cash changes, or assume zero
   flows. This is not solved by a new HMAC key or a credential alone.
2. **Remaining position risk:** real per-position callbacks still lack a
   coherent account-NLV epoch/source time and all remaining-fee evidence.
   Additional entry risk with existing positions stays blocked until a genuine
   producer and final gate prove those quantities. A read-only diagnostic and
   pure arithmetic are not that completion.
3. **Gmail owner setup:** create the free Google Desktop client and provide
   only its private local JSON path; complete consent in a system browser.
   The approved sender/destination need not be selected again. Five exact
   account-scoped Keychain items are still absent. After enrollment, bind the
   real route, install its reviewed configuration, start the independent
   worker, send the one approved labelled test, and confirm actual receipt.
   Publishing state and chosen Google account remain owner-verified; a login
   hint does not attest token identity. No paid service or billing enablement
   is authorized.
4. **Broker facts, not policy yes/no switches:** reliable P&L callbacks,
   no-borrow/same-day-proceeds capacity, fresh session API quote entitlement,
   all-client execution/order coverage, unchanged precautions, the supported
   no-bypass unattended transmit/cancel contract, and an all-in fee bound still
   need evidence. Fresh API acknowledgement/classification is now observed in
   Portal, but is not a signed production capability receipt. `doctor` still
   exposes inherited staged-config blockers, including the old acknowledgement
   label; this report distinguishes that stale label from the newly verified
   UI fact instead of deleting blockers to imply readiness.
5. **Final integration and activation:** production composition, plan/control
   evidence, orphan recovery, real protection/closeout verification, current
   market-data/risk joins and independent delivered notifications must pass
   before cutover. Today's installed `doctor` is **not ready for activation**.
   Follow the evidence-bound procedure in [OPERATIONS](../2026-09-08/OPERATIONS.md)
   only after those gates pass. The owner performs the final activation; do not
   edit the database, copy stale receipts, remove confirmations or start an
   autonomous account writer to bypass a failed gate.

No heartbeat or scheduler was modified in this update. Existing analysis-only
premarket / attended regular-hours scheduling is distinct from the dormant
autonomous services and was not newly installed or verified here.
