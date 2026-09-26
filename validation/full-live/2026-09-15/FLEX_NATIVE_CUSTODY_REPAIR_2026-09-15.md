# Flex attended Keychain read repair — September 15, 2026

## Evidence and cause

The owner's latest Terminal screenshot showed enrollment refusing existing
or unavailable custody and a separate probe returning `PROBE_FAILED`. A fresh
installed `flex-setup-status` now reports **PRESENT**. This supersedes the
earlier MISSING state, but does not prove stored contents or authentication.
The owner also entered a private value at the ordinary shell prompt after
enrollment exited; it was interpreted as a command. Do not reproduce that
value or enter private values outside their designated hidden prompts.

An exact-item diagnostic using the installed production Keychain reader began
at **23:37:48.492090 UTC** and reproduced
`CREDENTIAL_KEYCHAIN_TIMEOUTEXPIRED`. It attempted no network request and
displayed no secret, raw exception, command output, or credential-derived
identifier. The stored contents could not be validated through that reader.

The mismatch was in code: enrollment used native Security.framework access
within Python, while the report probe used a separate `/usr/bin/security`
process with a ten-second timeout. A separate macOS permission dialog is a
plausible explanation for that timeout, but was not directly observed. The
probe's generic exception handler hid the custody failure as `PROBE_FAILED`.

## Repair and validation

Source commit `2c9ab8a5bc3f756f279e3377a3c7e0c770cb6da3` switches only the
owner-run probe to the same native read used at enrollment. It requires the
real owner terminal, reads only the fixed Flex item, preserves exact stored
credential validation, and never calls the creation method. Native access
leaves any access dialog under macOS/owner control. It may wait while holding
the existing probe lock; it does not change ACLs, suppress a prompt, or qualify
unattended access. Fixed allow-listed native custody errors now become Flex
`KEYCHAIN_*` codes without exception stringification. Cancellation, no-retry,
report pacing, account input, and reporting-only boundaries are retained.

Independent review found no actionable code/security issue. Focused suite:
**84 passed**. Exact detached-source full suite: **1,310 passed in 61.599 s**.
Structural validation and diff checks passed. These are synthetic tests and
do not establish live Flex authentication. Clean-source guards stayed on.

Two builds were byte-identical:

| Field | Value |
|---|---|
| Release | `5899bdcad2f78f27957acd126ea58c49d710c7fdc77c91c0db965bf983bf9b61` |
| Archive SHA-256 | `f519bcd61f393fb795da06795ec052a828f793638b6e853ce8bb58998daefca3` |
| Manifest-file SHA-256 | `8b85629483034843692f977060a741ecd16a529eeaa2aadbb82b68d65aae9901` |

The installer verified all 110 files against exact source. Only the Flex
setup module and guide changed in the release payload. Config, policy, SDK
inventory and Gmail implementation remained unchanged.

## Installation and current state

Installed **23:41:59.494616 UTC**. Runtime generation **14**, PAUSED, authority
0, no activation stamp or writer lease. Installed status verified release and
runtime bindings. Doctor verified the audit chain and remains
`ready_for_owner_activation=false`. Trading coordinator is not loaded.

The notification outbox was empty of pending/claimed rows before stopping the
existing worker. PID 93308 exited, with generation 6 released at
23:41:43.964915 UTC. The pinned Python's genuine read-only SQLite connection
verified the quiesced state. After installation, the existing notification
plist matched the staged plist and the notification-only worker was restored.
At 23:42:22.212319 UTC: PID **95219**, generation **7**, heartbeat
23:42:21.974246 UTC, unreleased, zero last-cycle failures. The outbox retained
the one original `DELIVERED / PROVIDER_ACCEPTED` message, with no backlog.
No message was sent, and the acknowledged test was not repeated.

Final Flex metadata is **PRESENT**. The next step is the owner's private
`flex-probe --date 2026-09-14` with the full account entered at its hidden
prompt, followed by any normal macOS access prompt. Enrollment should not be
repeated. No actual native read or authenticated Flex API probe was run by
this repair; historical reporting, once verified, still cannot issue live
starting-equity or cash-flow authority. Earlier broker connection observations
retain their old timestamps. No trading, policy change, Gateway reconnect,
token regeneration, paid service or Git push occurred.

## Later owner verification

The owner subsequently supplied a successful installed-probe result received
at 2026-09-16 00:35:15.952472 UTC (September 15, 20:35 EDT):
`IBKR_FLEX_SETUP_REPORT_RECEIVED`, `ok=true`, and
`response_origin=flex_web_service_response`, for the requested September 14
period and account suffix 3103. This supersedes the pending native-read/Flex
authentication status above. It remains attended historical reporting only;
the live-risk flags stayed false/null. See the latest section of
`PORTAL_REPORTING_SETUP_STATUS_2026-09-15.md` for the safe result summary and
fresh, still-blocked Gateway check.
