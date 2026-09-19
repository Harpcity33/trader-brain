# IBKR Flex reporting ingestion — September 15, 2026

## Implemented result and remaining boundary

`src/titan_brain/live/broker/ibkr_flex.py` now implements the official two-step
Flex reporting protocol and exact-account Activity XML ingestion. No existing
Flex reader was present. The reporting-only `flex-setup-status`, `flex-enroll`
and `flex-probe` commands now wire that reader to exact, account-scoped Keychain
custody. They do not discover arbitrary credentials, create broker queries,
modify an account, purchase anything, sign daily-risk receipts, or enable
trading. No live Flex request has been made.

The parser preserves reported period starting/ending NAV and each available
NAV transfer component separately. It retains cash-transaction currency,
amount, FX rate, provider type and original date/time separately. It does not
sum dividends into contributions, infer transfers from cash changes, deduplicate
unidentified transactions, or turn an absent field/section into zero. A missing
transfer component remains `None`; an empty cash section is not current-flow
completeness. It rejects wrong accounts, multiple-account reports, model scope,
wrong periods, non-USD base currency, malformed values, unsafe XML and oversized
responses. Full account IDs, tokens, URLs and raw response bodies are absent
from diagnostics and normal representations.

Transport uses only IBKR's fixed HTTPS endpoints, default certificate checks,
no environment proxy and no redirects. The returned legacy URL is ignored.
Each request has a socket timeout and response-size limit; this is not a
process-level wall-clock timeout against a slow-drip server. One reader spaces
attempts at least six seconds apart, fails immediately when pacing would be
violated, and never automatically regenerates or retries a report. Sharing a
token with other processes still requires coordinated per-token pacing.
Provider errors return stable numeric error codes without `ErrorMessage` text.
[Generation protocol and pacing](https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service/generate-the-report),
[retrieval protocol](https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service/retrieve-the-report),
[provider error codes](https://www.interactivebrokers.com/docs/web-api/flex-web-service/error-codes).

Every result has a SHA-256 of the exact response bytes for private byte-
integrity and equality checks. The digest is linkable and is not anonymization,
redaction, proof of account scope, or proof of origin; keep it private with the
report. Online results additionally retain the generation-response hash under
the same limitation. No deterministic full-account scope fingerprint or hash
of an account-containing cash row is exposed. Cash rows retain only their
one-based source ordinal for bounded within-report provenance. Local parser
input is explicitly `local_unverified_bytes`; an injected custom/test transport
is `injected_transport_unverified_bytes`. Only the built-in fixed-endpoint HTTPS
transport labels bytes `flex_web_service_response`, and that label still does
not make the report a current economic valuation. `daily_starting_equity_ready`
is always false, and `require_live_daily_evidence()` always fails closed.

The supported field semantics are period-level starting/ending NAV, distinct
deposit/withdrawal, internal-cash-transfer and asset-transfer components, and
cash transactions which also include dividends. None of those definitions
promises a midnight valuation or an exhaustive five-second complete-through
watermark. [NAV field definitions](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm),
[cash-transaction field definitions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm).

## Owner setup — existing IBKR reporting, no new paid service

1. In the owner's existing IBKR Client Portal, select only the exact account
   ending 3103, then Reporting / Flex Queries. Enable Flex Web Service and
   create a reporting token with an intentional expiry and optional source-IP
   restriction. Keep it out of chat, shell arguments, logs and checked-in files.
   There is no OAuth scope string in this protocol: account/query selection
   controls report inclusion, and the token may access linked-account reports.
   A TWS/Gateway login does not supply this token. Do not enable a subscription
   or paid third-party service for this step.
   [Official token setup and account scope](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/enable-and-create-access-token).

2. Create an **Activity** Flex query, not Trade Confirmation. Select the one
   account, whole-account/no model scope, XML output, date format `yyyyMMdd`,
   time `HHmmss`, and semicolon date/time separator. Include Account Information
   (Account ID, base Currency), Change in NAV (Account ID, From/To Date,
   Starting/Ending Value and all three transfer components), and detailed Cash
   Transactions (Account ID, Currency, FX Rate to Base, Report Date, Date/Time,
   Amount, Type).
   Exclude account alias substitution and account consolidation. No symbol or
   transaction-type filter should hide account activity. Capture the Query ID
   from the query's information panel.
   [Activity query configuration](https://www.ibkrguides.com/complianceportal/activityflexqueries.htm),
   [format options](https://www.ibkrguides.com/reportingreference/reportguide/delivery%20configuration%20and%20general%20configuration.htm),
   [date/time defaults](https://www.ibkrguides.com/releasenotes/archive/statements-and-reports-archive.htm),
   [Query ID](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/create-a-flex-query).

3. Before production wiring, validate one actual completed-period response
   against the configured query. The unit fixtures are synthetic, not an
   account-specific XML certification. The reader currently expects
   `FlexQueryResponse type="AF"`, one `FlexStatement`, `AccountInformation`,
   `ChangeInNAV`, and optional `CashTransactions/CashTransaction`, with exact
   account/period attributes and the selected fields. Different or missing
   required fields must be investigated, never silently substituted. If no
   cash section appears, retain that fact as unknown. Missing optional NAV
   transfer fields remain unknown and cannot support a complete flow total.

4. Run the installed launcher's `flex-setup-status` to check metadata for only
   service `titan-full-live-ibkr-ending-3103-flex-reporting`, account
   `ibkr-live-ending-3103`. Presence is not authentication. From a private owner
   terminal, `flex-enroll` accepts hidden Query ID and reporting token prompts;
   it creates that exact Keychain item only when absent, verifies readback,
   never overwrites an existing item, and never changes an access-control list
   or suppresses macOS prompts. Secrets are not command-line arguments. No
   enrollment has been performed by this implementation. A failed readback
   requires custody inspection rather than automatic retry.

   Run enrollment on its own and wait for `IBKR_FLEX_SETUP_ENROLLED` before
   starting the probe. At each hidden prompt, paste the requested value once
   and press Return once; characters and asterisks intentionally do not appear.
   Query ID is the numeric ID in Titan Daily Accounting's Info panel. Token is
   the existing Current Token in Flex Web Service Configuration. Labels, query
   names, quotation marks and spaces are not accepted. Do not regenerate the
   token to resolve a local input-format error.

   The input-diagnostic update distinguishes `QUERY_ID_EMPTY`,
   `QUERY_ID_FORMAT_INVALID`, `TOKEN_EMPTY` and `TOKEN_FORMAT_INVALID` after the
   `IBKR_FLEX_SETUP_` prefix. It validates each input before asking for the next
   one, with static instructions and no automatic retry. The accepted formats
   remain 1-32 ASCII digits for Query ID and 6-128 ASCII digits for token;
   values are never trimmed or otherwise rewritten. These errors occur before
   storage and do not establish that IBKR rejected authentication. Older
   releases report only `IBKR_FLEX_SETUP_CREDENTIAL_INVALID`, which cannot
   distinguish the failing field, blank input, whitespace or incorrect length.

5. Run `flex-probe --date YYYY-MM-DD` for an actual completed historical date.
   It requires a hidden exact account-ID prompt at runtime: the full account
   identifier is **not persisted**, consistent with the existing provider
   profile. The actual authenticated XML must match that exact ID; a last-four
   match alone never accepts a report. The probe serializes local invocations
   with an owner-private lock under the existing install root's `control`
   directory. It waits six seconds before one generation request and before
   each of at most three retrievals, retrying only provider 1019 using the
   **same ticket**, never regenerating on an unknown result. Other clients
   sharing this token still require coordination. The ticket and raw report
   stay in memory; public output also omits linkable report hashes. The probe
   cannot run noninteractively, qualify unattended custody, issue live risk
   receipts, or activate anything. Historical dates do not prove final
   accounting. Use the installed launcher with `--install-root` if it is not
   resolved automatically; no token-bearing URL belongs in a shell.

   The attended probe now reads the saved item through the same native macOS
   Security framework and Python application used by enrollment. The earlier
   subprocess reader timed out after ten seconds on this item on September 15.
   It runs as a different application from enrollment and may require a
   separate macOS access dialog; that dialog cause was not directly observed.
   Native access lets macOS own the access dialog's
   lifetime and can wait for the owner while holding the local probe lock.
   It does not change an ACL, suppress a prompt or qualify unattended access.
   The full account prompt and real-terminal requirement remain mandatory.
   Native custody failures return only fixed `IBKR_FLEX_SETUP_KEYCHAIN_*`
   codes; malformed stored data still fails before any report request.

   `flex-setup-status` showing `PRESENT` means an item exists, not that its
   contents or IBKR authentication have been verified. Do not rerun enrollment
   to overwrite it. At the normal shell prompt ending in `%`, enter only the
   command; enter private values only after the helper displays its specific
   hidden prompt. After an error the helper exits and no longer accepts data.

## Proposed measurement amendment — not approved or activated

One broker-aligned proposal is to define each day's fixed whole-account
starting balance as the **ending NAV of the preceding completed broker daily
accounting period**, with its exact report date and provider receipt frozen
before any entries. This deliberately measures from a broker reporting boundary,
not an invented midnight NLV and not the first convenient later snapshot.
The owner would need to approve that boundary explicitly and understand that
post-boundary overnight accruals/FX changes are included in that interval's
performance. The +15% aspiration and irreversible −10% daily entry lock remain
unchanged; deposits, withdrawals and asset transfers must not masquerade as
profit or loss or reset an already-triggered lock.

That proposal only addresses a supportable baseline source. It **does not**
authorize stale or incomplete intraday transfer treatment. New entries remain
blocked whenever whole-account external cash/asset flows through the current
valuation cannot be authenticated and reconciled, including corrections,
cancellations, FX treatment and activity between reporting close and trading
start. An owner's promise not to transfer, an empty report, or an HMAC over
guessed zero flows is insufficient. No documented no-new-fee live route has
yet been established that closes this gap. Changing the baseline alone must
not activate the present daily gate.

IBKR documents a fixed three-minute account-summary update cadence, and its
previous-day Equity With Loan Value is securities-segment marginable equity,
not whole-account NAV. These cannot be relabeled as synchronized five-second
whole-equity/flow proof. [Account-summary timing](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-summary/introduction),
[account-value definitions](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-updates/account-value-keys).

## Verification

Hermetic command (no broker, Keychain or network calls):

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/harp/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12 -B -m unittest tests.test_live_ibkr_flex tests.test_live_ibkr_daily_starting_equity tests.test_live_ibkr_risk_evidence
```

Result: 50 tests passed on September 15, 2026. Existing midnight/five-second
checks, policy bindings, HMAC verification and ledger invariants are unchanged.
The executable reader is implemented; live account availability and daily
trading readiness are not claimed.
