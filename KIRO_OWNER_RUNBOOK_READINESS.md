# Run-it-yourself: readiness check & live evidence (owner)

Written by Kiro for the owner. **You** run every command here against your own
logged-in IBKR Gateway; Kiro does not. Copy each command, run it, and paste the
output back into the chat — Kiro will read the output and tell you honestly
whether it satisfies the gate. Nothing here places an order or activates
trading; the one command that activates (`activate`) is listed only so you know
to STOP before it.

## Before you start

- Your IBKR Gateway is logged in (you said it is). Keep it up while you run these.
- You need the **install root** — the folder the paused release was installed to.
  Default on macOS:
  `~/Library/Application Support/Titan Momentum/full-live`
  If you installed somewhere else, use that path. Below it is written as
  `INSTALL_ROOT` — set it once so you can paste the rest verbatim:

  ```sh
  INSTALL_ROOT="$HOME/Library/Application Support/Titan Momentum/full-live"
  ```

- The launcher is `titan-full-live` inside the installed release. If you can run
  `titan-full-live status --install-root "$INSTALL_ROOT"` and get output, you're
  set. (If the release isn't installed yet, stop here and tell Kiro — the paused
  install is an earlier step and Kiro will give you that runbook.)

> Note: these exact commands assume the release is already installed paused. If
> a command says the install root or release manifest is missing, that is the
> signal that the install step hasn't happened yet — paste the message back and
> Kiro will switch you to the install runbook.

## Step 1 — Where does it stand? (safe, read-only)

```sh
titan-full-live status --install-root "$INSTALL_ROOT"
```
- **What it does:** reports current install/activation state. Read-only.
- **Good:** it prints a status without errors, and shows trading is PAUSED / not
  activated (that's expected and correct right now).
- **Paste the whole output back.**

## Step 2 — Readiness check (the "is it ready" gate)

```sh
titan-full-live readiness --install-root "$INSTALL_ROOT"
```
- **What it does:** runs the full readiness evaluation against the live Gateway —
  single-writer ownership, broker/data readiness, and the session-trading
  integration gate.
- **Expect it to FAIL right now**, and that is correct: it should report
  `SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE` plus whatever live evidence is
  still missing. A readiness that "passes" before the evidence below exists would
  be the alarming result, not this one.
- **Paste the whole output back** — the exact blockers it lists are the checklist
  of what still has to be true.

## Step 3 — Diagnostics (safe, read-only)

```sh
titan-full-live doctor --install-root "$INSTALL_ROOT"
```
- **What it does:** environment / connection diagnostics against the Gateway.
- **Good:** connection CONNECTED, SDK ATTESTED, no missing-config errors.
- **Stop if:** it reports the wrong account, or "not connected" — that means the
  Gateway session isn't the one the release expects. Paste the output back.

## Step 4 — Reporting evidence (P9 groundwork)

```sh
titan-full-live flex-setup-status --install-root "$INSTALL_ROOT"
titan-full-live flex-probe --install-root "$INSTALL_ROOT" --date <YYYY-MM-DD>
```
- **What it does:** checks the IBKR Flex / reporting scope and pulls a dated
  reporting reading. This feeds **P9** (reporting-persistence across a restart).
- Use today's date (or the last trading day) for `--date`.
- **Paste both outputs back.** For the full P9 proof you'll run one reading, do a
  controlled Gateway restart, and run it again — Kiro will tell you exactly when,
  after seeing the first reading.

## What each piece of live evidence means (so the output makes sense)

- **P8 — reconciled session P&L:** real fills reconciled (proceeds − costs +
  residual long bid value − signed fees) against the frozen pre-trade balance.
  Surfaces through the readiness/reconciliation path above.
- **P9 — reporting-persistence:** two reporting readings across a restart prove
  the scope survives. The `flex-*` commands produce the readings; the offline
  evaluator renders the verdict.
- **P10 — fresh bid marks:** current bid marks for any residual inventory, from
  live Massive. Readiness reports `SESSION_OPEN_RISK_REVALUATION_REQUIRED` until
  present.

## The line you do NOT cross yet

```sh
# DO NOT RUN until Kiro confirms P8/P9/P10 genuinely pass and you decide to go live.
# titan-full-live activate --install-root "$INSTALL_ROOT" --activation-id <id> --confirm <phrase>
```
- `activate` (and `serve`) are what turn on autonomous live trading. They require
  a confirmation phrase **only you** enter. Kiro will never run these and will
  never tell you to run them until the evidence above actually passes. Reaching
  this line is *your* deliberate decision, not a step in a script.

## The loop

1. Run Steps 1–4, paste outputs here.
2. Kiro reads them and says, per gate, PASS / STILL-MISSING and why — honestly,
   no fabricated readiness.
3. Repeat for the P9 restart reading and any P10 marks Kiro identifies as needed.
4. When (and only when) real evidence satisfies every gate, Kiro tells you the
   remaining action is yours: enter the activation confirmation. Kiro stops there.
