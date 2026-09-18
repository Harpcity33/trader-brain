# Google branding and Gmail consent progress

Google project: `titan-trade-alerts`. Owner requested continuing from the
approved public website publication. No new paid service or billing change.

## Verified Google configuration

- Homepage saved: https://harpcity33.github.io/titan-trade-alerts/
- Privacy saved: https://harpcity33.github.io/titan-trade-alerts/privacy.html
- Authorized hostname saved: `harpcity33.github.io`, as required by the
  branding form. No claim of ownership of `github.io` or completed DNS/brand
  verification was made.
- Google displayed `Branding changes saved!`.
- Audience changed from Testing to **In production**, then verified in the
  actual Google console. Production publishing is not verification of the
  app, a Gmail grant, a sent email, or trading activation.
- Google still displayed a 100-user cap; no users had granted access.

## Local preflight

Installed release:
`25f2671c80bf668c458b7b4c1efa8eab190b16450e334625a7589671ee43a2a0`.
Source commit: `836a9b7b3f2a8e45c37da1803ee5d9ff33a07226`.
Installed and reviewed Gmail helper SHA-256:
`ddb8a3cc6816298dba50a2b19ab3ec42515f7b983fe71b3ce0afb42643da6a2f`.

Metadata-only lookup of the five exact IBKR Gmail Keychain entries returned
MISSING. The selected profile is `ibkr_gmail`, namespace
`ibkr-live-ending-3103`. No Keychain secret values were read by this preflight.
The downloaded client file remained owner-only (mode 600).

## Declared scope and owner consent handoff

- Google saved exactly `https://www.googleapis.com/auth/gmail.send`; the
  non-sensitive and restricted-scope lists were empty. The scope remains
  unverified, and Google displayed its verification notice. No verification
  application, demo video, or assertion of Google approval was submitted.
- The installed Desktop OAuth helper was started once and its authorization
  URL opened in a separate Safari tab. Final consent belongs to the owner.
  No authorization URL, callback code, or token is retained in this report.
- Safari reached Google's "Google hasn't verified this app" warning for the
  expected developer. The warning was left intact and the window brought
  forward for the owner's review; no continuation or permission grant was
  clicked by the assistant. The callback has a five-minute expiry. Check the
  retained helper's terminal result before attempting any later enrollment.

The helper
must verify the returned exact scope and real refresh before create-only
Keychain storage. An uncertain or partial result requires reconciliation;
do not repeat an unknown grant or overwrite credentials.

This report does not authorize a trade, enable a notification route, start a
service, or treat Google acceptance as owner receipt of an alert.

## Reconciled result after owner consent and Keychain interaction

The original helper process exited with code 2 and the sanitized result
`GMAIL_SETUP_FAILED_REVIEW_PARTIAL_ENROLLMENT`. It explicitly reported
`message_sent: false` and `trading_activated: false`. The attempt was not
replayed, and no existing credential was overwritten or deleted.

At `2026-09-15T02:33:04.102184+00:00`, exact metadata-only lookups under
`ibkr-live-ending-3103` returned:

| Service | Metadata status |
| --- | --- |
| `titan-full-live-ibkr-ending-3103-gmail-desktop-client` | PRESENT |
| `titan-full-live-ibkr-ending-3103-gmail-refresh-token` | MISSING |
| `titan-full-live-ibkr-ending-3103-gmail-consent-status` | MISSING |
| `titan-full-live-ibkr-ending-3103-gmail-sender` | MISSING |
| `titan-full-live-ibkr-ending-3103-gmail-destination` | MISSING |

No secret values were read during reconciliation. Presence does not verify
the saved client contents. The installed helper performs both the exact-scope
token exchange and a refresh before writing the client entry; reaching that
write is consistent with successful Google exchange and refresh, but no
durable refresh credential or completed enrollment remains locally.

The first write's readback uses `/usr/bin/security` with a ten-second timeout.
A readback credential error is caught by the generic partial-enrollment
handler. The owner encountered a Keychain password prompt, making a readback
timeout plausible, but the sanitized process result does not establish the
specific underlying exception. Do not report a confirmed timeout or a usable
authenticated notification connection.

The current helper deliberately rejects any pre-existing enrollment item;
it cannot safely restart this state unchanged. Recovery needs an explicitly
reviewed path that validates and preserves the exact existing client,
requires the other four entries to remain absent, and obtains a fresh Google
grant. It must not replay the consumed authorization code, overwrite an
existing refresh token, weaken Keychain access controls, or mark a route ready.
Owner approval for that recovery is the next gate.

Read-only launchd checks still found neither the IBKR trading service nor its
notification service (both exited 113: service not found). No trading,
configuration, notification-route, or service changes were made this turn.
The existing Gmail OAuth setup suite was rerun: 26 tests passed in 0.173s.
These are offline regression tests, not evidence of successful enrollment.

## Authorized repair and successful real enrollment

The owner explicitly authorized repairing and preserving the partial
enrollment, and continuing the remaining setup. That authorization does not
establish missing broker facts, owner receipt of an email, or completion of a
personal legal declaration.

Recovery commit `4ed6ad175b92ce28926d3fab943f5ffc9889ab92` adds the narrowly
scoped matching-client-only recovery path, native owner-interactive Keychain
readback, and callback URL cleanup. Independent review found no blocking
issues. Forty setup tests passed. Combined tests on the clean commit passed:
1,188 in 60.328s with bundled Python (two SDK skips), and 1,188 in 60.547s
using the existing verified SDK (no skips). Two builds had identical archive
SHA-256 `51363eb4e2ea7cf1711af9a927b1e239c0dbea0998956beae9db5b56b725a889`.

That repair was installed PAUSED at `2026-09-15T02:42:38.579289+00:00`,
release `90029bdc0bf8534f732f923d96c457526305e4b32ab56594ddfe868b3ce3a853`,
generation 8. The installer verified/reused the existing 303-file official
IBKR SDK snapshot; no dependency download, spending, broker call, or service
start occurred during installation.

Exactly one new recovery enrollment was then performed. The native reader
verified the preserved client. Google's real renewal page showed the exact
owner account and existing access to only **Send email on your behalf**.
The assistant renewed that unchanged, already-authorized grant. The app
remains unverified by Google; no scope expansion, new legal declaration,
password retrieval, or Keychain ACL change was performed.

The installed helper exited 0 with `KEYCHAIN_ENROLLMENT_COMPLETE`, exact
`gmail.send` scope, and `oauth_refresh_verified: true`. All five credentials
were saved/verified; the existing client was not overwritten. It reported
`message_sent: false`, `route_ready: false`, and `trading_activated: false`.
The earlier partial-enrollment blocker is resolved, not silently discarded.

## Separately exercised background credential reader

Commit `cec218e4e495f997c1dcf83f10ca3895ee9d55f8` supplies a separate read-only
native custodian restricted to the five exact IBKR Gmail locators. It requests
authentication-UI failure on each query, without changing ACLs or global
interaction settings. Legacy and injected readers remain unchanged. Ten new
native-reader tests, 21 existing assembly tests, and 40 setup tests passed.

An actual bounded probe of that clean committed source succeeded at
`2026-09-15T02:45:33.717986+00:00`: all five native reads, the expected sender
and destination comparison, and a real OAuth refresh. The complete probe
returned in about 0.16s, with no authentication dialog or stderr observed.
No credential contents were printed, and no message was sent. This proves
this local probe worked; it does not guarantee access after a future Keychain
lock, credential revocation, or different execution identity.

Non-secret bindings obtained from that authenticated probe:

- Destination fingerprint:
  `79b18a332f22b118678e4ca252f25108d4661cfa4feb91cea5e9ae0cf43e4da4`.
- Authorization binding:
  `b7dcb3191222a85ad0d2cfccef9c777c6de74376187828a7ce4a52df31e711bc`.
- Proposed/current source route version: `ibkr-gmail-3103-2026-09-14-v1`.
- Required delivery assurance remains `OWNER_CONFIRMED`; provider acceptance
  is not owner receipt. Actual final installation/delivery is recorded below
  once exercised. These source bindings alone do not start a worker.
