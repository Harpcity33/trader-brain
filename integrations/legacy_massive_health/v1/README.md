# Legacy Massive collector health repair v1

Status: repository implementation and offline tests only. Application, restart,
and post-restart observation are separate deployment steps; this artifact does
not establish live feed readiness or any broker/trading authority.

## Exact provenance

The inspected installed `titan_runtime/massive.py` has SHA-256
`0be1d3f0d6be20f92cac97a913636e8299ba122300dcbfe244e003f17791b2e3`.
The older workspace-canonical file instead has SHA-256
`31dee996286adb3157b37a8d2b691c1a2ef4eb4569fcd5e884f63aeb53c06922`.
They are not interchangeable: installed code contains newer shadow attribution,
policy, instrument-universe and halt handling. This patch changes only the exact
installed preimage. Do not overwrite it with the older canonical collector or
apply the patch fuzzily to that version.

`manifest.json` binds the full target before/after hashes, standalone helper
bytes, and the exact minimal legacy seam used by hermetic tests. The versioned
unified patch uses paths relative to the parent of `titan_runtime`.

## Behavior and evidence boundary

Previously, `massive_websocket.checked_at` advanced only for connection,
authentication or subscription status messages. Fresh AM/A/Q data advanced a
different `market_data_freshness` row, while the adapter required both timestamps
within 15 seconds. Long-lived streams therefore falsely aged out between status
messages even while quotes and bars remained fresh.

The standalone, standard-library-only `massive_health.py` tracker now permits a
healthy write only after a market-data handler completes successfully and the
event passes conservative shape, finite-value and provider-time checks:

- The exact current socket must have completed a valid authentication handshake.
- Q uses its provider `t`; A/AM use provider `e` and a valid `s`/`e` window. Q/A
  must be at most 15 seconds old; AM at most 120 seconds; the existing one-second
  future tolerance is retained. These are health checks, not order eligibility.
- Prices must be positive and finite; quote sizes positive integers, bid/ask
  uncrossed, aggregate OHLC consistent, volume nonnegative, symbol well formed,
  and OTC data excluded from this heartbeat. Invalid data cannot pulse health.
- Provider timestamps must advance within each channel. Duplicate/reordered
  observations cannot keep health alive.
- The first qualifying event pulses immediately, then at most once per five
  seconds, on a later qualifying event. There is no timer inventing fresh data.
- Pings, empty messages, LULD-only traffic and successful status messages cannot
  pulse. Negative/unrecognized status anywhere in a frame rejects that frame
  before any data handler. A failed connection requires a new connection,
  authentication and qualifying data; later `success` cannot clear the failure.
- Reconnect begins degraded and cannot become healthy from authentication alone.
  Handshake failures close the exact attempted socket and invalidate its tracker.

The helper does not write or supply `checked_at`: the existing Store timestamps
the actual write using its clock. Existing quote/bar freshness checks, entry
limits, candidate interpretation, risk controls and configuration are unchanged.
The doctor's separate premarket calendar-default behavior is not changed here.

Provider field references: [Massive WebSocket authentication](https://massive.com/docs/websocket),
[stock quotes](https://massive.com/docs/websocket/stocks/quotes),
[second aggregates](https://massive.com/docs/websocket/stocks/aggregates-per-second),
and [minute aggregates](https://massive.com/docs/websocket/stocks/aggregates-per-minute).
Receipt pacing is local health policy; those docs do not promise a five-second
heartbeat or continuous trades on every symbol. No additional paid service or
credential is introduced.

## Controlled deployment checklist

1. Keep live entry authority paused. Record the running collector PID and exact
   launch mechanism without dumping environment variables or credentials.
2. Verify target SHA-256 equals `target_before_sha256`, and verify the helper
   against `helper_sha256` in the manifest. Any mismatch requires fresh review,
   not force/fuzz. Preserve a recoverable exact backup of both files before any
   replacement, including whether a helper previously existed.
3. Apply `installed-0be1d3f0-health-v1.patch` to that exact legacy target. Copy the
   repository `src/titan_brain/live/massive_health.py` byte-for-byte to the sibling
   `titan_runtime/massive_health.py`. Do not import the whole live runtime into the
   collector or modify its launch PYTHONPATH. Confirm the target-after and helper
   hashes before restarting; compile/check with the collector's own interpreter.
4. Restart only the existing collector through its supported launch mechanism.
   Do not run a parallel second collector or change subscriptions/configuration.
5. Read only the two health rows and newest quote/bar timestamps. During flowing
   validated data, verify several heartbeat advances over more than 15 seconds,
   with `source=authenticated_processed_market_data`, and independently fresh
   quotes/bars. Status-only traffic must not be counted as proof. Check for
   collector errors and use the installed doctor afterward. This repair clears
   only the demonstrated stale-health mismatch; all other gates remain required.
6. If verification fails, stop the attempted collector, restore the exact saved
   target/helper pair, and restart the prior collector. Keep entry authority
   paused and retain the failed-deployment evidence.

Offline regression command from repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest tests.test_live_massive_legacy_health
```

Tests execute patched legacy method bodies extracted from a versioned minimal
preimage fixture with fake sockets, handlers, clocks and Store. They do not import
the installed collector, fetch credentials, open a broker connection, or write
any production database. The full inspected installed source is separately
hash-checked and patch-applied in memory before handoff; it is not vendored here.
