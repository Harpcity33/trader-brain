# IBKR Portal reporting setup — September 15, 2026

## Authorized scope and completed external change

The owner asked to automate step 2 in the open Safari Portal and the remaining
supported setup. This is not permission to manufacture broker evidence, bypass
precautions, or activate a blocked trading runtime.

Created **Titan Daily Accounting**, an Activity Flex query for the single live
IBKR account ending 3103. The Portal displayed its successful-save message;
the saved query's Info view independently showed the following persisted
configuration:

- Account Information: Account ID and Currency.
- Change in NAV: Account ID, Currency, From Date, To Date, Starting Value,
  Ending Value, Deposits/Withdrawals, Internal Cash Transfers, Asset Transfers.
- Cash Transactions: Account ID, Currency, FX Rate To Base, Date/Time, Amount,
  Type, **Report Date**; detail rows and all available transaction categories
  retained. Report Date was added after the first genuine report exposed the
  accounting-date distinction described below.
- XML; Last Business Day; whole-account scope; no model or symbol filters;
  date `yyyyMMdd`, time `HHmmss`, semicolon date/time separator; account alias
  substitution disabled.

The query reference was retained privately for continuation. No token, full
account identifier, raw financial report, or report byte hash is in this file.

## Real reporting exercise

The signed-in Safari Portal generated and downloaded the query at approximately
12:46 UTC. The owner-local file is
`/Users/harp/Downloads/Titan_Daily_Accounting.xml`; its permissions were tightened
to `0600`. It was not copied into Git or into the trading runtime.

A bounded XML shape check observed one Activity Flex statement, account suffix
3103, USD base currency, and period **2026-09-14 through 2026-09-14**. All three
configured sections were present. The NAV attributes included every selected
field, and Cash Transactions contained two rows. Financial values were not
printed. This is an observed Portal download and local structural inspection,
**not** an authenticated Flex Web Service probe, a live cash-flow completeness
watermark, or release-bound risk authority.

The initial report exposed a real compatibility bug: both cash rows had a
September 13 transaction Date/Time inside the September 14 statement. The
existing parser incorrectly required transaction Date/Time itself to fall
inside the statement period. No report value or timestamp was rewritten.

The query was edited to add IBKR's separate **Report Date** field, reviewed,
and saved successfully. A second Portal run produced
`/Users/harp/Downloads/Titan_Daily_Accounting-2.xml`, also made `0600`. Both rows
contained `reportDate="20260914"` while preserving transaction dates of
September 13. Exact-account, no-model, finite numeric fields, required row
attributes, and date/time format checks passed. The report-period and
transaction-time distinction is provider-observed, not inferred from local
receipt time. The original download is retained separately, not overwritten.

## New executable fixes and local exercise

1. The Flex parser now requires each cash row's valid `reportDate` inside the
   statement/query accounting period and retains the original valid Date/Time
   separately. Missing, malformed, or out-of-period Report Date still blocks.
   Report Date is private bookkeeping metadata, not a valuation timestamp or
   live-flow watermark. Synthetic regression tests reproduce the observed
   cross-date structure without copying private report values.
2. Gmail diagnostics preserve numeric HTTP status plus a strictly allow-listed
   OAuth error category. Malformed, duplicate-key, oversized, or unrecognized
   responses become `UNCLASSIFIED`; timeout, TLS, DNS, and network categories
   are separate. Raw bodies, descriptions, headers, URLs, and credentials are
   not put in the replacement exception or provider report. Transient and
   unclassified errors advise preserving credentials. Failures still block.

At 12:52:17 UTC, the corrected real Portal download passed the updated parser
in a local-only diagnostic. Its output explicitly remained
`response_origin=local_unverified_bytes`, `reporting_only=true`,
`daily_starting_equity_ready=false`, and
`live_cash_flow_complete_through=null`. This exercise used a non-transmitted
placeholder Query ID because no provider request was made; it did not create
an authenticated query/reference receipt. The original report without Report
Date was correctly rejected as `IBKR_FLEX_CASH_REPORT_DATE_INVALID`.

Validation on the combined source: **1,303 tests passed in 63.257 seconds**.
The repository structural validator passed (175 Python, 33 JSON, four TOML
files), and `git diff --check` was clean. Focused independent runs passed
37 Flex/setup tests and 95 notification/local-assembly tests. Tests are
synthetic unless explicitly labeled as the local real-report exercise above;
none placed orders, sent email, or created authentication evidence.

## Actual connection observations

Earlier on this date at 12:18 UTC, Gateway, the whole-account read, and contract
details had succeeded. A strictly newer normal installed provider probe at
**12:44:04 UTC** showed:

| Component | Latest result |
|---|---|
| Massive REST | Authenticated market-status read succeeded |
| Massive WSS | Authenticated handshake succeeded |
| IBKR Gateway local API | Managed-account discovery succeeded |
| IBKR whole-account collection | Blocked: `LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_SDK_CALLBACK_2110` |
| IBKR contract details | Not reached after the account-read failure |
| Gmail | OAuth refresh succeeded; no email sent |

The native Gateway window then showed **Existing session detected**, explaining
that reconnecting this session would disconnect another login using the same
username. No reconnect was clicked. The specific session-displacement approval
was requested. The observation does not establish which other client owns that
login. IBKR documents 2110 as a TWS/Gateway-to-server connectivity failure:
<https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/error-codes>.

The earlier generic Gmail refresh failure was not reproducible: both an
installed direct refresh diagnostic and the normal provider probe succeeded.
Its original cause cannot be recovered from the generic error that was retained.
Existing credentials and the acknowledged delivery test were preserved. No
re-enrollment or resend was performed.

## Reporting-access state changed during setup

A later fresh Safari observation showed Flex Web Service enabled and the
Portal's token-update success message. The displayed activation period was
2026-09-15 08:52:22 EDT through 2027-08-17 08:52:22 EDT. The agent did **not**
click Enable/Save or generate a token: this was an observed external UI change
while other work proceeded. No duplicate token was generated. The current
token was not printed or written to a file/repository.

Automated access to the Terminal application was then explicitly denied by the
computer-use tool. No alternative route was attempted to bypass that denial,
no hidden-input safeguard was patched out, and no credential was passed in a
shell argument or tool message. The supported owner-terminal `flex-enroll`
and `flex-probe` remain the continuation for private credential entry.

## Explicitly incomplete

- Flex Web Service is now visibly enabled, but local token enrollment and
  endpoint authentication are separate and remain incomplete.
- The exact local Flex Keychain item remains `MISSING`; enrollment and the
  authenticated `flex-probe` have not run.
- Gateway reconnection remains pending the separate session-displacement
  approval. No current flatness or complete P&L/order snapshot is claimed from
  the failed account collection.
- No live cash-flow completeness provider, exhaustive historical order/reference
  coverage, coherent remaining-position risk evidence, or account-specific
  no-bypass transmission/protection contract was established by this setup.
- The measurement boundary proposal is distinct from factual readiness. No
  financial policy, broker precaution, or trading activation was changed.

## Installed/running boundary at 12:49 UTC

Installed executable source remained `1ca93e6e3b668e3531e0acaf7fc0f323bb33502b`,
release `1f6cc34ff5b1877572d07d31382f6d4326d7ec7543be4887c28dddb982ee9abf`.
`status` verified release and runtime bindings: generation 11, **PAUSED**,
authority 0, no activation stamp and no service writer lease. Local database
position/intent counts are not a fresh broker reconciliation. Data ingestion
and a notification worker do not mean autonomous trading is active.

## New paused installation and final verification

The fixes were committed as `a590e5ed0fdc06c9d769f12538b460fcc2183915`.
The initial build refused existing ignored bytecode caches in the working
repository. Those files were preserved. A fresh detached worktree of that
exact commit was used instead; no clean-source guard was disabled.

Two clean builds were byte-identical (`cmp` passed):

| Field | Verified value |
|---|---|
| Release | `96844a26795ae676676c57b44a35473d01d2ec16ad592a2b4de129eb71ff3a3d` |
| Archive SHA-256 | `29e3b1b1e22c873aff195bf9a68158d4df676202ad6a936f4d629953db3cc006` |
| Manifest-file SHA-256 | `c3daf30757e1e603a3c44f7c5fabfa760188e15c0b4c7f3c227f7ede3b6329f4` |
| Unchanged config hash | `c756c6f75c895d14b6e3b4b3349c6655bbf50d1d39001ab385b233fc67c61cdd` |
| Unchanged policy hash | `f54292e461f5c27f721b9ffe939a267539af0358e8d43a994520ac94ee7e984e` |

The combined suite was rerun on the exact detached build source:
**1,303 tests passed in 63.107 seconds**. The original 63.257-second run above
also passed; this repeat removes working-directory cache ambiguity.

After confirming no pending notifications, the existing notification worker
was stopped. PID 79576 exited; its lease was verified released at
12:56:42.656646 UTC. The system `sqlite3 -readonly` utility could not open the
quiesced database, so the installed status command and a genuine read-only
connection through the pinned Python SQLite runtime verified state; no
immutable-mode/stale-WAL shortcut, database edit or manual lease clearance
was used.

The source-verified installer validated all 110 release files and installed
the new release at **12:57:14.528016 UTC**. Runtime generation is now **12**,
PAUSED, authority 0, no activation stamp or writer lease. The existing SDK
snapshot remained ibapi 10.50.2 / protobuf 5.29.5 with the same pinned file
inventory. The installer did not access the broker or start a service.

The unchanged notification LaunchAgent matched its newly staged counterpart
and was restored separately: PID **84274**, worker lease generation **5**,
heartbeat **12:58:05.390647 UTC**, unreleased. The outbox still has one original
`DELIVERED / PROVIDER_ACCEPTED` test and no pending rows. No test was resent.
The trading coordinator label was verified **not loaded**.

The post-install doctor returned `ready_for_owner_activation=false`, retaining
the existing broker, risk, order-coverage, authority and notification-evidence
gates. Staged labels about declarations or destination evidence must not be
interpreted as contradicting the owner's earlier declaration completion or
email acknowledgement; authenticated release-bound checks remain separate.

A new normal provider probe at **12:57:53.627557 UTC** authenticated Massive
REST/WSS, local Gateway managed-account discovery, and Gmail OAuth refresh.
Whole-account collection remained blocked, now by
`LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_SDK_CALLBACK_2103`; contract details were not
reached. The native Gateway session-conflict dialog remained present. No
successful current account reconciliation, flatness, or protection is claimed.
The probe reported empty broker-mutation and email-send lists. A final
`flex-setup-status` still reported exact local custody `MISSING`.

No orders, warning suppression, precaution bypass, paid services, or GitHub
push were performed during this setup. The code is implemented, tested and
installed; the historical Portal query is real and working; automated Flex
authentication and full-live trading are **not** running.

## Later input-diagnostic installation — 23:20 UTC

The owner's private enrollment attempts produced local format errors; they
did not establish authenticated reporting. A small diagnostic repair was
subsequently installed, with per-field empty/format errors and unchanged
credential, policy and hidden-input protections. Latest installed source is
`bce53544069b0b4a7480c6fd2ebad645f1495126`, release
`b23e7c3fca276f50ad92bebdf6833b4e27f818a275b5fd737813d36a2e04263b`,
runtime generation 13, PAUSED, authority 0. Flex metadata remains MISSING.
See `FLEX_ENROLLMENT_INPUT_REPAIR_2026-09-15.md` for the later build,
installation, notification-worker and verification evidence. The earlier
broker connection observations above were not refreshed by this repair.

## Latest custody-read repair — 23:41 UTC

Flex metadata now reports PRESENT following the owner's private work, while
the old report reader's exact-item Keychain read was observed timing out.
The attended probe now uses the same native Keychain read as enrollment.
Latest installed source is `2c9ab8a5bc3f756f279e3377a3c7e0c770cb6da3`, release
`5899bdcad2f78f27957acd126ea58c49d710c7fdc77c91c0db965bf983bf9b61`,
generation 14, PAUSED, authority 0. Actual Flex authentication remains
unverified. See `FLEX_NATIVE_CUSTODY_REPAIR_2026-09-15.md` for the reproduced
timeout, verified repair, current worker state and remaining private probe.

## Successful owner Flex probe — September 15, 20:35 ET

The owner supplied a Terminal screenshot of the installed `flex-probe --date
2026-09-14` returning `IBKR_FLEX_SETUP_REPORT_RECEIVED`, `ok=true`, and
`response_origin=flex_web_service_response`. The report receipt time shown is
**2026-09-16 00:35:15.952472 UTC** (September 15, 20:35:15 EDT). It identifies
account suffix 3103, USD, period September 14 through September 14, a present
Cash Transactions section and two cash rows. This is the first successful
authenticated historical Flex probe evidenced in this task. Evidence is the
owner-supplied terminal result, not an agent rerun or retained raw XML. No
token, Query ID, full account ID, raw report or report digest is recorded here.

This supersedes the earlier unverified-authentication status. The native
Keychain read repair has now been exercised by the owner's successful probe.
No further enrollment or token generation is needed. The result explicitly
retains `reporting_only=true`, `daily_starting_equity_ready=false`,
`midnight_valuation_at=null` and `live_cash_flow_complete_through=null`.
Historical report authentication does not establish the missing live baseline,
cash-flow completeness, or unattended custody/activation evidence.

Fresh installed status after receipt still verifies source
`2c9ab8a5bc3f756f279e3377a3c7e0c770cb6da3`, release
`5899bdcad2f78f27957acd126ea58c49d710c7fdc77c91c0db965bf983bf9b61`,
runtime generation 14, PAUSED, authority 0, no activation stamp or service
writer lease. No executable source, policy or installation changed here.

A separate normal read-only provider probe at **2026-09-16 00:36:18.094737 UTC**
authenticated Massive REST/WSS, local Gateway managed-account discovery and
Gmail refresh. Whole-account collection remained blocked, now by
`LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_SDK_CALLBACK_2110`; contract details were not
reached. Its broker-mutation and email-send lists were empty. A fresh native
Gateway UI observation still showed **Existing session detected** and the
warning that **Reconnect This Session** disconnects another same-user session.
No reconnect was clicked. Specific owner approval remains required for that
session displacement. Current broker flatness/exposure is not established.

## Owner-approved Gateway reconnect — September 15, 20:37–20:40 ET

After being told that reconnecting would disconnect the other same-username
session, the owner explicitly replied **Reconnect**. The agent freshly
inspected the same session-conflict dialog and clicked **Reconnect This
Session** once. The dialog disappeared and Gateway displayed **Interactive
Brokers API Server connected**. A later UI observation showed the market-data
farm ON. No trading or account-setting change was requested or performed.

The normal installed provider probe at **2026-09-16 00:37:58.434441 UTC**
authenticated managed-account discovery, Massive REST/WSS and Gmail refresh.
The whole-account read returned the generic
`LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_PROBE_FAILED`, with no contract read reached.
Broker-mutation and email-send lists remained empty. This proves connection
recovery, not a complete account/order/P&L reconciliation.

A separate read-only diagnostic used the same verified installed release and
SDK, observing/rethrowing account-reader exceptions without changing their
behavior. It emitted only fixed classifications and the runtime's existing
sanitized error code; private callback data, account values and exception
text were not printed. The later attempt started at **00:40:13.294988 UTC**
and returned `IBKR_RUNTIME_READ_DAILY_REALIZED_PNL_RETRY_EXHAUSTED_NO_CALLBACK`
in the WAIT_CALLBACKS phase. Account and contract collection both remained
incomplete. This later failure does not retroactively identify the earlier
generic failure, nor establish that closed hours are its cause.

Fresh installed status continued to verify generation 14, PAUSED, authority
0, no activation stamp and no service writer lease, on the unchanged release
`5899bdcad2f78f27957acd126ea58c49d710c7fdc77c91c0db965bf983bf9b61`.
This was a reconnect and diagnostic follow-up; no executable repair, new
installation, credential change, new report request, email or trading order
was performed. The Gateway session-conflict approval is now fulfilled. The
missing current daily-P&L callback and all other readiness gates remain open.
