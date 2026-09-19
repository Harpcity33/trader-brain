# Gmail Desktop export compatibility and connection gate

Observed September 14, 2026, America/New_York. This is a new, narrow
compatibility fix, not a claim that the earlier deployment was newly built.

## Implemented and exercised

- Local source commit: `836a9b7b3f2a8e45c37da1803ee5d9ff33a07226`.
- Google's owner-downloaded Desktop client JSON used
  `https://accounts.google.com/o/oauth2/auth` as its `auth_uri` metadata.
  The previous enrollment parser required the v2 metadata value and rejected
  this real Google export.
- The parser now accepts only those two exact Google metadata strings. Actual
  authorization remains hard-coded to
  `https://accounts.google.com/o/oauth2/v2/auth`; the token endpoint, TLS,
  no-redirect transport, PKCE/state, send-only scope and confirmation gates
  are unchanged. Imported metadata never chooses the request destination.
- Two regression tests cover both export variants and exact v2 navigation,
  plus lookalike hosts, userinfo, HTTP, alternate paths, ports, query/fragment
  additions, and malformed types.
- Targeted enrollment suite: 26 tests, no failures or skips.
- Full offline suite: 1,174 tests run, 2 SDK-only skips, no failures,
  58.073 seconds. No new full SDK-enabled suite is claimed for this fix.
- Independent code review found no issue in the narrow patch.
- The real downloaded file was restricted to mode 0600. Both repository and
  newly installed parsers accepted it and confirmed the intended project;
  credential values were not printed, copied into the repository, or sent
  to another service. Validation did not start OAuth or contact Google.

## Built and installed, not running

Two clean-commit builds were byte-identical (`cmp` exit 0):

- Release ID: `25f2671c80bf668c458b7b4c1efa8eab190b16450e334625a7589671ee43a2a0`
- Archive SHA-256: `c2ae8229308276f5ec787887b706712e23ecec42476e3c58cef18f0fd6b4da28`
- Manifest file SHA-256: `93ca8a47b2f4dd3fba09fea5a5618ecb035b85672dadcc0d2ef3656675cac158`
- Installed at `2026-09-15T02:03:39.877449Z` (September 14 ET) using the
  provenance-checking PAUSED installer and the already authorized official
  SDK venv. All 303 SDK files matched the pinned inventory; no dependencies
  were downloaded. SDK remains ibapi 10.50.2 and protobuf 5.29.5.
- Fresh read-only runtime query: `PAUSED`, authority `0`, generation `7`,
  no activation timestamp, zero unreleased account-writer leases.
- Both exact IBKR trading and independent-notification launchd labels were
  absent (`launchctl print` exit 113). No service was started.
- No broker calls, orders, notification sends, OAuth exchanges, Keychain
  credential creation, publishing actions or policy/config changes occurred.
- Commits and report are local; this turn did not push a GitHub update.

## Exact remaining Google gate

The Google Cloud UI confirms the OAuth client was created. However, a fresh
Audience-page reload still shows `Testing` and a disabled `Publish app`.
Its accessible tooltip specifically requires valid app name, support email,
homepage URL, and privacy-policy URL before switching to external production.
The saved app name and support email are present; the homepage and privacy
policy fields are blank. No substitute URL or invented policy was entered.

Google's [Audience documentation](https://support.google.com/cloud/answer/15549945)
states that Testing authorization and offline refresh tokens for this scope
expire after seven days. The helper therefore remains blocked for durable
enrollment; it was not invoked with a false Production attestation.

The [personal-use verification exception](https://support.google.com/cloud/answer/13464323?hl=en)
must not be confused with this observed publishing prerequisite. No claim is
made that a paid service or full third-party security assessment is required.
The owner must choose an authorized homepage/privacy-policy location and
review the disclosures before publication; free hosting may be considered.
Publishing does not itself grant access to Gmail. Subsequent system-browser
consent and explicit local credential storage remain separate owner steps.

## After enrollment

Real grant/refresh verification, reviewed notification-route binding, one
already-approved labelled test, and actual owner receipt/acknowledgement still
remain. Read-only review also found that provider acceptance cannot by itself
satisfy `OWNER_CONFIRMED`: a supported persistence path for that acknowledgement
still needs implementation/verification before claiming end-to-end readiness.
The pre-existing financial, broker, daily-feed, and activation gates remain
unchanged. No trading readiness is established by this OAuth parser fix.
