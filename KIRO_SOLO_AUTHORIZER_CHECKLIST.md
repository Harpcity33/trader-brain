# Solo-authorizer: authorization record template + attended-activation checklist

Author: Kiro, for the owner acting as SOLE author/approver. This is a template
and a checklist, not authority itself. Kiro does not fill in the binding
receipts, sign the record, build/install a live release, or run activation.
Those are yours. Everything below is designed so the receipts are GENUINE
records you can produce, not filled-in blanks.

Account: ibkr-live-ending-3103. Scope: ATTENDED (you confirm every order),
regular hours, equities. Not autonomous; not options.

---

## Part A — the authorization record (whose hash becomes authorization_binding_id)

Create a plain-text (or JSON) document you keep OUTSIDE the repo (it is your
signed record, not source). Suggested name:
`~/titan-authority/attended-authorization-<date>.txt`. It must state, in your
own words and values:

```
TITAN ATTENDED-EXECUTION AUTHORIZATION (SOLE AUTHORIZER)

Authorizer:            <your name>
Date (UTC):            <ISO8601, e.g. 2026-09-22T13:30:00Z>
Account:               ibkr-live-ending-3103  (masked ****3103)
Scope authorized:      attended equity orders, regular hours, long only,
                       human confirmation required per order; NOT autonomous,
                       NOT options, NOT extended hours.

Release authorized:
  release_manifest_hash: <from the helper, run against the ATTENDED release you built>
  config_hash:           <from the helper, ATTENDED config>
  policy_hash:           <from the helper, ATTENDED config>

Evidence I reviewed and relied on (attach or reference each):
  - reproducible-build proof of the release named above (two identical builds)
  - offline suite green + validate_repository PASS on that release
  - IBKR account/API state: API order permission enabled, Read-Only API OFF,
    market-data entitlements for the instruments, port 4001, account 3103
  - live readiness output (after P8/P9/P10) showing the gate passing
  - the account-identity capture and IBKR terms referenced below

Bindings I am asserting (see Parts B and C for how each was produced):
  account_binding_fingerprint: <64-hex = sha256 of the account-identity capture>
  provider_contract_id:        <64-hex = sha256 of the reviewed IBKR terms doc>

Statement:
  I, as sole authorizer and account owner, authorize the exact release and
  config named above to place ATTENDED equity orders on account 3103, subject
  to per-order confirmation, for the validity window below. I have reviewed the
  evidence above and accept the risk. This authorization is void if the release,
  config, account, or permissions change.

Validity:              issued <ISO8601>  expires <ISO8601, short window>
Signature:             <your name> / <date>
```

`authorization_binding_id` = **sha256 of this exact file's bytes** (see Part D
for the command). It is genuine because the document really exists, states a
real decision, and references real evidence — not a random value.

## Part B — account-identity capture (whose hash becomes account_binding_fingerprint)

From an AUTHENTICATED IB Gateway session on account 3103, capture the broker's
own account-identity material (e.g. the managed-accounts response confirming the
account, with a timestamp) into a file you keep outside the repo, e.g.
`~/titan-authority/account-identity-<date>.json`. Then
`account_binding_fingerprint` = sha256 of that file. Genuine because only a real
authenticated session on account 3103 can produce it. (Do NOT commit it; it
identifies your account.)

## Part C — reviewed IBKR terms (whose hash becomes provider_contract_id)

Capture the actual IBKR API/trading-permission terms in effect for the account
into a file, e.g. `~/titan-authority/ibkr-terms-<date>.txt`, and set
`provider_contract_id` = sha256 of it. Genuine because it records the specific
terms you reviewed; if the terms change, the hash changes.

## Part D — computing the three receipt hashes (a real sha256 of a real file)

On macOS, for each file:

```sh
shasum -a 256 ~/titan-authority/account-identity-<date>.json    # -> account_binding_fingerprint
shasum -a 256 ~/titan-authority/ibkr-terms-<date>.txt           # -> provider_contract_id
shasum -a 256 ~/titan-authority/attended-authorization-<date>.txt  # -> authorization_binding_id
```

Each output's 64-hex is the receipt. Put the SAME three values into
`config/full_live_ibkr_attended.json` (replacing the OWNER_PROVISIONED_*
sentinels) AND into the authority-evidence file (Part F) — the loader requires
them to match.

---

## Full solo checklist — here to attended activation

Legend: [YOU] = only you can do it; [KIRO] = Kiro can do it as reviewed code.

1. [KIRO] Reviewed code merged: PR #9 (base), #12 (options analysis/paper),
   #13 (attended scaffolding). [YOU] review + merge in the order you accept.
2. [YOU] IBKR Layer-1 on account 3103: enable ActiveX/Socket clients; DISABLE
   Read-Only API; confirm equity trading permission + market data; Gateway on
   port 4001, logged in.
3. [YOU] Provision the three binding receipts (Parts A–D) as genuine records,
   and put the three 64-hex values into config/full_live_ibkr_attended.json.
4. [KIRO or YOU] Build the reviewed ATTENDED release reproducibly from the
   merged source + provisioned attended config. [KIRO] can run the build
   (offline). Record its release_id.
5. [YOU] Install it PAUSED to the account-3103 install root (owner install
   step). Create control/ibkr/attended-command-authority.json (Part F) with the
   real hashes + the three receipts.
6. [YOU] Re-run the helper against the INSTALLED attended release to get the
   real release_manifest_hash/config_hash/policy_hash; confirm they match the
   authority record and config.
7. [YOU] Gather P8/P9/P10 live evidence against the Gateway (Kiro interprets
   the outputs you paste).
8. [YOU] Run readiness; it must genuinely PASS (BrokerFactoryError gone,
   integration gate cleared for the attended lane). Kiro reads the output.
9. [YOU] prepare-activation -> review the phrase -> activate (moves PAUSED ->
   RECONCILING; cannot place an order). Start the supervised service.
10. [YOU] Place the first order via attended-review and confirm it yourself
    with the exact phrase. You approve every order. pause-new-entries is your
    always-available stop.

## Part F — the authority-evidence file (control/ibkr/attended-command-authority.json)

14 fields, all required (from the loader):
```json
{
  "schema_version":             "<AUTHORITY_SCHEMA value>",
  "artifact_hash":              "<sha256 of this file's canonical content>",
  "release_manifest_hash":      "<helper, attended release>",
  "config_hash":                "<helper, attended config>",
  "policy_hash":                "<helper, attended config>",
  "account_key":                "ibkr-live-ending-3103",
  "account_masked":             "****3103",
  "authorization_binding_id":   "<Part A hash>",
  "account_binding_fingerprint":"<Part B hash>",
  "provider_contract_id":       "<Part C hash>",
  "environment":                "live",
  "client_id":                  19736,
  "issued_at":                  "<ISO8601>",
  "expires_at":                 "<ISO8601, short window>"
}
```
The loader cross-checks release/config/policy hashes against the installed
release and the loaded policy, the account fields against the policy, the three
receipts against the config, and issued_at <= now < expires_at. Any mismatch or
expiry fails closed.

## Lines Kiro holds throughout

Kiro writes reviewable code, templates, and the read-only helper, and can build
the release offline. Kiro does NOT: create the three binding receipts, write or
sign the authorization record, author the authority-evidence file with real
attestations, run activate/serve/place/confirm, or lift the readiness gate.
Attended means a human confirms every order — that human is you.
