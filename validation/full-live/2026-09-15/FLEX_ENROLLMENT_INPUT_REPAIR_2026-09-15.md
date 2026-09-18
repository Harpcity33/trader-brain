# Flex enrollment input diagnostics — September 15, 2026

## Observed problem

The owner supplied Terminal screenshots showing repeated
`IBKR_FLEX_SETUP_CREDENTIAL_INVALID` results and an earlier
`IBKR_FLEX_SETUP_ACCOUNT_INVALID` result. These are local validation failures,
not IBKR authentication rejections. The screenshots contain hidden prompts but
cannot establish what was entered. The old credential error conflated empty
input, whitespace, labels, non-ASCII digits and invalid lengths. The earlier
explanation that an input necessarily contained non-digits was too specific.

Fresh metadata checks still returned `IBKR_FLEX_SETUP_MISSING`. No credential
contents were read. No Terminal automation, alternative GUI credential entry,
fake TTY, credential injection or hidden-input bypass was attempted.

## Implemented and verified

Commit `bce53544069b0b4a7480c6fd2ebad645f1495126` adds immediate validation after
each existing hidden enrollment prompt. It returns only static per-field
codes: `QUERY_ID_EMPTY`, `QUERY_ID_FORMAT_INVALID`, `TOKEN_EMPTY`, or
`TOKEN_FORMAT_INVALID`, under the `IBKR_FLEX_SETUP_` prefix, plus static
instructions. An invalid Query ID stops before requesting the token. Values,
value-derived lengths, prefixes, hashes and raw exceptions are not displayed.

Exact format requirements, getpass warning guard, terminal requirement,
create-only Keychain custody, generic stored-credential validation, and
reporting-only authority are unchanged. Input is not normalized or retried.
No change was made to the probe account validator or the reporting protocol.

Independent review found no actionable correctness or security issue.
Focused tests: 41 passed. Exact detached-source combined suite: **1,307 tests
passed in 61.160 seconds**. Structural validator and diff checks passed.
Tests used synthetic input; they did not enroll or authenticate real secrets.

Two builds from the clean exact-source worktree were byte-identical:

| Artifact | Value |
|---|---|
| Release | `b23e7c3fca276f50ad92bebdf6833b4e27f818a275b5fd737813d36a2e04263b` |
| Archive SHA-256 | `8cba550d69901493041196c3ae6c1708c7e44f24ffc1487e4b20202fe0dffe2d` |
| Manifest-file SHA-256 | `cd4286d41580c7e26882decda9af20ed457f4c59a9c7db824e5098a83ef7b086` |

All 110 release files were verified by the source-verified installer. Only
the Flex setup module and setup guide changed in the release payload; config
and policy hashes and the SDK inventory were unchanged. The original
checkout's ignored bytecode was preserved; the clean-source guard stayed on.

## Installed and running

Installation completed at **2026-09-15 23:20:37.239238 UTC**. Runtime generation
13 is **PAUSED**, authority 0, with no activation stamp or service writer
lease. Release and runtime bindings verified; doctor reports a valid audit
chain and `ready_for_owner_activation=false`.

Before installation the notification outbox had no pending/claimed rows.
The existing notification worker exited, releasing generation 5 at
23:20:14.833236 UTC. System SQLite could not open the quiesced database; a
genuine read-only connection through the pinned Python runtime verified the
release without stale immutable mode or database edits. Its existing plist
matched the newly staged plist and was restored separately after installation.

At 23:21:09.802613 UTC, the notification worker was running as PID 93308,
lease generation 6, heartbeat 23:21:09.071401 UTC, unreleased, zero last-cycle
failures. The outbox retained the one original `DELIVERED / PROVIDER_ACCEPTED`
message and no backlog. No email test was sent. The trading coordinator was
verified not loaded. Automations were not changed.

## Remaining owner step and boundaries

The final Flex metadata check still reported **MISSING**. The owner must run
`flex-enroll` privately and enter the actual numeric Query ID and existing
Current Token at their respective hidden prompts. Run enrollment alone and
wait for `IBKR_FLEX_SETUP_ENROLLED` before the separate historical probe.
If input still fails, the new static error identifies the field. No real
enrollment, Flex API request, or new broker reconciliation occurred here.

Earlier broker/session observations retain their original timestamps; they
were not rechecked during this input-diagnostic repair. All earlier live
readiness, policy-boundary, cash-flow, coverage, fee, protection and authority
gates remain unresolved. No financial policy or activation changed, no token
was regenerated, no paid service was added, and no Git push was performed.
