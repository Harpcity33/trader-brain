# Trader Brain — Codex / Kiro development handoff

## Start here

Continue SOFTWARE DEVELOPMENT in this isolated working tree. Do not start a
real-money trading session. No brokerage authority is conferred by this document,
by opening a different editor, or by a successful software test. Preserve all
provider and client approval requirements. Do not route a previously blocked
order-writing action through a different tool, agent, credential, or transport.

The user wants an eventual autonomous options application. The work below is
infrastructure toward evaluating such a system, not discretionary account
management or a claim that an unattended live executor has been completed.

## Latest user-stated preferences

- Proposed initial test capital: **$500** in the Robinhood Agentic account.
  The deposit has not been checked in this development session; do not assume it.
- Daily account loss threshold: **5%** of start-of-day equity, initially **$25**.
- Aspirational daily gain goal: **10%**, initially **$50**. Not an expected return,
  promise, instruction to force trades, or automatic risk escalation.
- Calculate session P&L from total equity, adjusting for external deposits and
  withdrawals. Count unrealized outcomes and costs, not closed trades alone.
- Earlier draft **$50 weekly / $15 per-trade** settings are NOT the user's
  approved live policy. The inherited `pilot_validation.PilotLimits` module is
  historical draft work only; do not import it as the policy for this request.
- No added subscriptions, paid-model API calls, funding operations, or permission
  changes. No reset or migration of the original or comparison paper ledgers.

`lifecycle_lab.daily_budget` implements the current percentages as a REPORT. It
never changes the deployed runtime. A trigger is not a guaranteed loss ceiling.
The stop-versus-full-premium distinction remains an economic fact, not an extra
user-approved risk cap. Live readiness cannot be inferred from a $25 calculation.

## Actual project layout

Repository: `Harpcity33/trader-brain`.
Running checkout last inspected: `/Users/harp/trader-brain`, main at
`48aabff245dcbd9216a55f92756adf794ab6b988`.
The existing services use that working directory: do not change it for experiments.

This development branch is based on PR #20 head
`ac90f20cf8466df395f267fe3a17ed39a938c8ba` and adds standalone lifecycle work.
The branch is `feature/lifecycle-lab-codex-handoff`. It is not production.

Existing components:
- `scripts/paper_options_runtime.py`: the running deterministic paper engine.
- `src/titan_brain/robinhood_options.py`: read-only options data; exactly three
  allowed market-data tools. Do not change the allowlist or authentication settings.
- `apps/control_center/`: branded iPhone Home Screen web app, private Tailscale
  connection and comparison-paper controls. The comparison account is NOT the
  original paper account and does not operate a live brokerage account.
- `src/titan_brain/pilot_validation.py`: older observation-validation draft;
  includes superseded draft risk defaults. Not a live order controller.

## What this addition actually implements

`src/titan_brain/lifecycle_lab.py`:

1. **Durable order-evidence journal.** SQLite transactions bind the expected
   reference, account, contract, side and quantity to supplied broker evidence.
   Cumulative fills survive reopen/restart; duplicate events do not double-count.
   An acknowledgement does not create a fill. Changed identities and unexplained
   economic corrections leave the prior state untouched and require review.
2. **Position/protection review.** Evaluates supplied position and quote snapshots
   for stop, target and closeout thresholds, stale prices, insufficient exit depth,
   unverified working protection, and an already-pending close. It outputs review
   diagnostics. It DOES NOT place protective orders or close real positions.
3. **Restart reconciliation report.** Compares persisted cumulative fills with a
   complete, fresh, caller-supplied position snapshot. Unknown order delivery,
   unmanaged holdings and mismatches remain visible. Never resubmit an uncertain
   order merely because the process restarted or one snapshot did not contain it.
4. **Current daily-preference calculation.** 5% loss threshold / 10% gain goal;
   external cash movements are not counted as investment performance.

There is deliberately NO broker client, live dispatcher, cancellation transport,
network call, background scheduler, activation function, or credential reader in
this module. The previous blocked order-writing action has not been retried.
No real-world protection is provided by these diagnostics.

## Integration gaps to investigate next

Work on these in simulation/read-only development without altering the running
services or broker permissions:

- Bind actual, supported READ responses to the normalized evidence contract.
  Inspect current broker schemas. Verify account, option contract, execution IDs,
  cumulative quantities, fee units, quote times, all pagination and cash-flow
  semantics. Do not infer fields or treat a dictionary from an LLM as a broker.
- Build a simulated dispatcher and fault-injection tests for delivery uncertainty,
  partial fills, cancel/fill races and recovery. Keep the fake transport incapable
  of accepting live credentials or reaching a broker.
- Add an account-explicit HUMAN REVIEW interface and audit trail; explain whether
  each record belongs to Original Paper, Comparison Paper, or verified broker
  observations. Do not portray an exit-review flag as an executed close.
- Test stale records, dropped connections, absent protection, closed sessions,
  expiration handling, abnormal exits, database contention, process termination,
  and external/manual changes to the account.
- Produce an integration matrix identifying which supported environment can
  perform each requested operation and which approvals it requires. Do not
  assume Codex, Kiro, or a specific model has blanket permission to manage funds.

Actual order transmission, protective-order placement, brokerage-authenticated
recovery automation and live deployment remain UNSUPPLIED. Any separately
operated implementation must meet the brokerage and execution client's current
requirements. This handoff is not an instruction to bypass them.

## Test commands

Run from this isolated project directory; all fixtures are synthetic:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_lifecycle_lab.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python3 -B -m unittest discover -s apps/control_center/tests -q
python3 -B scripts/replay_lifecycle_lab.py
```

The new module has 52 tests before any subsequent developer changes. Report actual
new runs and counts; never recycle an old passing count as current evidence.
The inherited suite includes 48 PR #20 validator tests plus the earlier baseline.
Do not run a provider doctor, email test, broker review or any deployment command
as part of these offline checks.

## Developer application setup

Both installed applications were inspected on the Mac in this session:
`codex-cli 0.159.1` (ChatGPT login) and `kiro-cli 2.26.0`.
Their versions and permissions may change; verify before relying on old CLI flags.
No development agent has been launched headlessly or granted additional access.

Open this isolated folder in Codex and start by asking it to read this document.
Kiro can open the same directory; keep one editor responsible for changes at a
time. Do not run both against the production directory. Retain normal approval
prompts. Existing ChatGPT sign-in does not mean unlimited usage or API credits.

## Official references checked

- Codex desktop/local-folder workflow and switching views:
  https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex
- Kiro MCP permissions and tool behavior:
  https://kiro.dev/docs/mcp/usage/
- Kiro security/approvals:
  https://kiro.dev/docs/privacy-and-security/
- Robinhood agent capabilities and account responsibility:
  https://robinhood.com/us/en/support/articles/trading-with-your-agent/

These references document product capabilities, not approval for this assistant
or this particular program to take over discretionary real-money trading.
