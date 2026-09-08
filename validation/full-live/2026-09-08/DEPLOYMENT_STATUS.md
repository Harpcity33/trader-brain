# Full-live deployment status — 2026-09-08

Evidence recorded through `2026-09-08T01:00:37Z` for account `ending-7153`.

## Source synchronization

- Requested upstream: `Harpcity33/trader-brain`, branch `main`.
- Latest upstream commit observed through the connected GitHub source:
  `485345d99e339c5c65efbc1e9375b76c90b0b451`.
- Upstream tree: `3c78af6f5af157f838bc4f957cd6af2324f5aa69`.
- Native private-repository fetch credentials were not available in this
  process. The connected GitHub source materialized all 67 upstream files;
  local import commit `9f4dfb661f630bde5cb89ab52cb834460f37b5c3`
  has exactly the same tree hash. This is a content-exact main snapshot, not a
  claim that native upstream commit ancestry was fetched.

## Built

- Implementation commit:
  `4e6af86c8e506c0ac588d5d8d03d3d33eee93677`.
- Implementation tree:
  `98683229d64113ce4aece5a503503b28999e7311`.
- Release ID:
  `d892737a13335df7ba06b16041f8ae4b9d59675e7bacb7c37f48bfe84a7ac407`.
- Release archive SHA-256:
  `1c07a62822667ec9a26385adccc411888772780101eb9d19ab6602fdbf73b0c5`.
- Release manifest-file SHA-256:
  `b10b7dcab13714705ef436b8959c53e8a5dafae11007944f074f7892dc0116d3`.
- Two independent builds from the implementation commit produced byte-for-byte
  identical archives and identical hashes.
- Full offline suite: 255 tests passed, 0 failures, 0 errors, in 7.753 seconds
  under Python 3.12.14 on Darwin 25.6.0 arm64. See `TEST_RESULTS.json`.

## Installed

- Install root:
  `/Users/harp/Library/Application Support/Titan Momentum/full-live`.
- Installed at: `2026-09-08T00:57:17.986263+00:00`.
- `current` points to the content-addressed release ID above.
- Durable state initialized at `2026-09-08T00:57:33.425422+00:00`.
- Runtime ID: `titan_full_live_autonomy_2026-09-08_v1`.
- Runtime mode: `PAUSED`.
- Authority enabled: `0`.
- Activation generation: `0`.
- Activation timestamp: `null`.
- Release manifest hash:
  `d892737a13335df7ba06b16041f8ae4b9d59675e7bacb7c37f48bfe84a7ac407`.
- Config hash:
  `a2eeacfc974a1bb51f1d98b25d510d91b5c7ff06f4d62b9c56c5832a49d2f629`.
- Policy hash:
  `7fd45013ce88e7f0a060475c0ce6404cf1583669a5ec797232865b911e39895e`.
- Installed release integrity check: valid.
- Installed audit chain after the readiness writer lease was released: valid,
  length 3.
- Installer broker access: false.
- Installer legacy-runtime modification: false.

## Actually running

- Full-live service process: **not running**; no matching process was present
  in the read-only process audit.
- Full-live launchd service: **not loaded**; `launchctl` reported no
  `gui/501/com.harpcity.trader-brain-full-live` service.
- Staged full-live plist: present only inside the isolated install subtree,
  with `Disabled=true`, no `RunAtLoad`, and
  `KeepAlive.SuccessfulExit=false`.
- Full-live broker authority: **not active**.
- Full-live live-order/cancel calls during build, installation, initialization,
  and readiness verification: **zero**.
- Legacy Robinhood heartbeat `robinhood-momentum-engine`: **ACTIVE** and not
  modified.
- Legacy `com.titan.momentum-watcher`: registered with launchd but reported
  `state = not running` at the audit time.
- Pre-existing `titan_runtime.mcp_server` processes remained present at PIDs
  21587, 21588, and 28041. Their existence is not treated as proof of an
  account writer or full-live service.

## Machine readiness

The installed readiness probe at `2026-09-08T00:57:52.720121+00:00`
produced evidence hash
`8273b2cc83ff26e3e8fb3ac4d5cb884163a6e267d1538287d09835f98a33be0e`
and `ready_for_owner_activation=false`.

The checked-in release deliberately blocks activation because the current
Robinhood surface requires per-mutation confirmation, is not a supported
daemon write transport, does not expose complete advanced-order
reconciliation or client-reference lookup, and cannot prove whole-broker
flatness. Signed policy provenance, numeric spread/depth and setup/execution
thresholds, production tradability/quality providers, and an end-user
notification bridge with delivery receipt are also unresolved. The legacy
heartbeat remains active, and the holiday market feed was stale from the last
trading session. These blockers are enforced by both configuration and
machine-collected activation evidence; there is no force or acknowledge-
blockers path.

The last attended broker audit, retained as redacted local evidence, observed
nonnegative cash and unleveraged buying power with the latter retained as the
sizing ceiling, no standard-equity or option positions, no same-day
standard-equity or option orders, and no realized P&L for the audited day.
Exact point-in-time balances are not committed. The audit explicitly did **not**
prove whole-broker flatness because advanced orders were unreadable.

## Owner activation status

No activation record was prepared or consumed. The installed system cannot be
activated from its current release because readiness is blocked. The exact
owner-controlled cutover procedure—and the gates that a reviewed replacement
release must satisfy—is in `OPERATIONS.md`.
