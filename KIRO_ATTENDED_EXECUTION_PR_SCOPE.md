# Attended-execution enablement — reviewed PR scope (DRAFT for owner review)

Author: Kiro. This is a **scope proposal for a reviewed change**, not the change
itself. It describes exactly what a PR must do to enable ATTENDED (human-
confirms-each-order) live equity execution for the account-3103 install, why
each part is required, and how it is proven. It authorizes nothing: the owner
reviews/merges, provisions the authority evidence, installs paused, gathers live
evidence, and activates. Kiro will implement the reviewable code parts as a PR;
Kiro will NOT provision the live authority evidence, flip the gate, or activate.

## Goal & non-goals

- GOAL: a signed, reviewed release in which `execution_authority_mode` is
  `attended_only` on the SUPPORTED production transport, so that
  `titan-full-live attended-review` / `attended-confirm` can place a regular-
  hours equity order **only after an explicit owner confirmation phrase**.
- NON-GOALS: no unattended/autonomous execution; no options live wiring
  (options stay analysis/paper-only); no change to the readiness gate's meaning;
  no bypass of P8/P9/P10. Extended-hours, atomic protection, replacement, and
  authoritative negative lookup remain denied by the attended descriptor.

## Why the current install cannot do this (baseline)

`config/full_live_ibkr.json` `execution` today: `broker_adapter=
ibkr_local_gateway_staged`, no `production_transport_id`, no
`production_authorization_binding_id` / `production_account_binding_fingerprint`
/ `ibkr_provider_contract_id`. `build_release_bound_ibkr_command_inputs`
(`ibkr_command_inputs.py:612`) refuses unless the adapter is
`supported_production_transport` AND those bindings are present AND a matching
`control/ibkr/attended-command-authority.json` write-evidence file exists. The
broker factory raises `BrokerFactoryError` ("supported production broker
selected without an injected authorized transport") until the supported
transport is wired. This PR closes exactly that gap for the ATTENDED lane.

## Part 1 — config delta (reviewed; a new signed release)

In a new `config/full_live_ibkr.json` revision (do NOT hand-edit an installed
config; this is a source change producing a new signed release):

- `execution.broker_adapter`: `ibkr_local_gateway_staged` -> `supported_production_transport`
- KEEP `execution.execution_authority_mode`: `attended_only`
- KEEP `execution.per_mutation_user_confirmation_required`: `true`
- KEEP `execution.supported_unattended_mutation`: `false`  (attended never sets this true)
- KEEP `execution.local_mutation_interlock_enabled`: `false`
- ADD `execution.production_transport_id`: `"ibkr-tws-api-10.50.2-v1"`  (IBKR_TRANSPORT_ID)
- ADD `execution.production_authorization_binding_id`: `<sha256>`  (owner-provisioned; see Part 3)
- ADD `execution.production_account_binding_fingerprint`: `<sha256>`  (owner-provisioned)
- ADD `execution.ibkr_provider_contract_id`: `<sha256>`  (owner-provisioned)

These sha256 binding values are placeholders in the reviewed source ONLY as
schema; their real values are the owner's attended-authority attestation and are
installed as write-evidence (Part 3), not committed. Acceptance: `policy.py`
supported-transport validation (`policy.py:673`) passes; attended-confirmation
requirement stays asserted (`ATTENDED_CONFIRMATION_NOT_REQUIRED` must NOT fire).

## Part 2 — transport wiring (reviewed code)

The attended transport already exists (`attended_ibkr_descriptor` in
`ibkr_transport.py`, `IbkrProductionTransport` in `local_assembly.py`). Wiring
work:

- Ensure the supported-attended assembly path composes `attended_ibkr_descriptor`
  (which sets `supports_equity_review/place/cancel=True`, marks writes attended,
  denies unattended writes / extended hours / atomic protection / authoritative
  negative lookup). No new capability is introduced.
- Confirm `RuntimeComposition` binds the production transport for the attended
  command lane only (not `serve`), and that the equity STK gates in
  `ibkr_orders.py` / `ibkr_sdk.py` remain the order path (options excluded).
- No change to discovery/session/options modules.

Acceptance: golden descriptor test that the attended descriptor denies
unattended writes and extended hours; a factory test that
`supported_production_transport` + attended bindings builds a client whose
`descriptor.transport_id == "ibkr-tws-api-10.50.2-v1"` and whose capabilities
have `supports_unattended_writes=False`.

## Part 3 — attended authority evidence (owner-provisioned, NOT committed)

At install time the owner creates `control/ibkr/attended-command-authority.json`
under the install root, carrying the write-evidence the command inputs verify
(`_load_write_evidence`): `authorization_binding_id`,
`account_binding_fingerprint`, `reviewed_contract_id`, bound to the release
manifest hash and validated against the signed policy. Requirements:

- values must equal the config's `production_authorization_binding_id` /
  `production_account_binding_fingerprint` / `ibkr_provider_contract_id`
  (else `IBKR_COMMAND_SIGNED_EXECUTION_BINDING_MISMATCH`);
- account binding must resolve to the live managed-account last-4 (3103) via
  `managed_accounts_runtime_last4_match` — verified at runtime, not asserted;
- this file is the owner's attested authorization for the attended lane. It is
  install-time control state, NEVER committed to the repo. Kiro documents its
  exact required shape; the owner generates it.

Acceptance: a test that a missing/mismatched authority file fails closed with
the exact codes; a test that the account last-4 mismatch is refused.

## Part 4 — tests (all offline)

- config validation: the new revision loads, is supported-transport + attended,
  and requires confirmation; golden hashes recorded.
- factory/transport: builds attended client; denies unattended; STK-only order
  path intact; options excluded.
- command inputs: authority-evidence match required; mismatch/missing/last-4
  failures fail closed; writer + coordinator locks distinct.
- attended flow (mocked broker, no socket): review -> owner confirmation phrase
  -> confirm; a wrong/absent phrase is refused; regular-hours-only enforced;
  reconcile-without-retry on ambiguous submission preserved.
- reproducible build of the new release; full suite + validator green.

## Owner-only steps AFTER this PR merges

1. Set IBKR Layer-1: Gateway on port 4001, "ActiveX and Socket Clients" enabled,
   "Read-Only API" DISABLED, client ids 19735/19736 permitted, account 3103
   logged in, equity trading permissions + market data enabled.
2. Build the reviewed release; install PAUSED; create the Part-3 authority file.
3. Run readiness; gather P8/P9/P10; confirm readiness passes.
4. Place the first live order via `attended-review` and confirm it yourself with
   the exact phrase. There is no autonomous order path; you approve each one.

## Lines Kiro holds

Kiro implements Parts 1 (schema), 2, 4 as a reviewed PR. Kiro does NOT create
the Part-3 authority evidence, does NOT run activate/place/confirm, and does NOT
lift the readiness gate. Attended means a human confirms every order — that
human is the owner, not Kiro.
