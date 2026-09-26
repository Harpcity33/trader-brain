# Independent Gmail alerts: owner setup, no additional paid service

## Setup mechanism and connection boundary

The owner approved the existing Gmail address as both sender and destination,
and one clearly labelled alert test after local authorization. The address is
deliberately not reproduced in this public repository. `OWNER_EMAIL` below
means that already approved address; it is not permission to select another
recipient. The owner's connected chat Gmail account is not a credential for
the separately supervised Titan notification worker.

The new executable `titan_brain.live.gmail_oauth_setup` prepares the supported
Google Desktop OAuth flow. It requests only `gmail.send`, verifies PKCE/state,
exchanges the code, tests a real refresh, and creates only the five exact IBKR
Keychain entries. It does not enable a route, send an email, change a broker
control, start a service, or authorize trading. Current real enrollment and
deployment results are recorded separately in
`GMAIL_GOOGLE_PRODUCTION_AND_CONSENT_2026-09-14.md`; mocked tests alone are not
an authenticated connection.

## Owner steps in Google Cloud

1. Sign in to [Google Cloud Clients](https://console.developers.google.com/auth/clients)
   with the intended account. Create/select a dedicated project and enable the
   Gmail API. Do not attach billing, buy a subscription, request a paid quota
   increase, create a service account, or grant domain-wide delegation. Stop if
   the console requires payment or broader permissions.
2. In Google Auth Platform, review Branding, Audience and Data Access. Select
   the appropriate audience yourself. Request only
   `https://www.googleapis.com/auth/gmail.send`; no inbox, Drive or account-wide
   mail access is needed. Complete any required legal review yourself.
3. Create an OAuth client of type **Desktop app**, and download its JSON to a
   private local file outside the repository. Restrict it to your own user
   (`chmod 600 /absolute/private/path/client.json`). Do not paste its contents,
   authorization codes, or tokens into chat or GitHub. Supply only its local
   path when ready.
4. Check the app's publishing status. External **Testing** mode generally
   gives this scope a seven-day refresh token and is not durable production
   authorization. Use Production after reviewing Google's requirements, or
   Internal only if your Workspace organization actually permits it. The
   helper's `--consent-status` is an explicit owner attestation, not a remote
   check that Google approved or published an app. Do not claim Production
   merely to pass validation.

Google documents the [native Desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app)
and [refresh-token expiration](https://developers.google.com/identity/protocols/oauth2).
Use your system browser for consent, not an embedded webview. The helper prints
a Google authorization URL and listens only on a random localhost port for a
short-lived callback. Review the exact app, account and send-only scope before
you approve access.

## Local enrollment command — owner controlled

After verifying the installed release, use the pinned Python and the installed
source below. Substitute your actual private file path, already approved email,
and genuinely applicable publishing state. Run this interactively yourself;
`--authorize-keychain-create` explicitly authorizes creating those credentials.

```sh
PYTHONPATH='/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103/current/src' \
  /Users/harp/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B \
  -m titan_brain.live.gmail_oauth_setup \
  --source-root '/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103/current' \
  --client-json /absolute/private/path/client.json \
  --sender OWNER_EMAIL --destination OWNER_EMAIL \
  --consent-status production --authorize-keychain-create
```

The helper refuses existing or conflicting Keychain items rather than
overwriting them. A partial or uncertain enrollment remains route-not-ready;
credentials may have been saved even if final readback failed. It does not
automatically delete credentials. Review existing items before retrying.
Tokens never appear in process command arguments or reports.

### Narrow recovery after a client-only partial enrollment

Only after exact metadata reconciliation and explicit owner authorization,
append `--authorize-recover-client-only` to the command above. Both
authorization flags are required. The helper accepts only the single existing
Desktop client plus four missing entries. It compares the complete saved
client document with the authorized download in memory, then requires a fresh
Google grant. It repeats the checks before creating the four missing entries,
with consent saved last. It does not replay the old code, overwrite a token,
delete credentials, or repair any other partial/conflicting state.

Setup uses native Keychain reads so a normal owner authentication dialog is
not terminated by a ten-second subprocess timeout. It does not weaken access
controls or supply the owner's password. Successful interactive enrollment
must still be followed by a separate background-reader and delivery check.

## What remains after successful enrollment

The exact IBKR provider route must still be bound to the real authenticated
credential/destination evidence in a reviewed release. Rebuild/install PAUSED,
start only the independent notification worker through its supported procedure,
send the one approved clearly labelled test, and verify its actual receipt and
owner-visible acknowledgement. A Google message ID proves API acceptance, not
that the owner saw it. Do not enable autonomous trading from this setup helper.

No additional service spending is authorized. Gmail has
[usage quotas and a daily billing threshold](https://developers.google.com/workspace/gmail/api/reference/quota),
not an unconditional unlimited-free guarantee. A `messages.send` call currently
uses 100 quota units. Keep billing disabled and investigate any payment or quota
requirement before proceeding; this guide does not authorize an expense.
