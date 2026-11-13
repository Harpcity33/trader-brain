# $1,000 paper reporting quarantine

## Defect and evidence boundary

The published standalone runtime defaults to
`~/.local/state/trader-brain/paper-options-v1.json`. Control Center imports the
same runtime but passes its own `--home/portfolio.json` (default
`~/.local/state/trader-brain/control-center/portfolio.json`). Both independently
seed $1,000. Before this fix both queue `eod:{date}` and SMTP hashes that ID,
so different books produce the same Message-ID and indistinguishable reports.

Neither the October 6 handoff nor a configured $1,000 starting value establishes
which store contains complete authoritative history. The October 12 reports also
contain stock/EXIT_MONITOR fields absent from the published runtime. Inspection
of all 22 branch tips, main's runtime history, and relevant legacy implementations
did not establish the deployed stock source. This patch targets published code;
it is not evidence of host repair, migration, or deployment.

## Implemented boundary

- Official experiment equity, daily P&L, daily return and learning eligibility
  are unavailable while authority/history remain unresolved. There is no flag
  that turns a balanced local snapshot into the official experiment.
- EOD notices are explicitly QUARANTINED, identify their local source and runtime,
  preserve diagnostic balances separately, and omit profit factor/expectancy.
  Per-source/date/version IDs are stable across retries, including copied books
  at different paths. Source IDs hash the resolved path and stored ledger ID;
  they are provenance labels, not proof of ownership or complete history.
- Pending pre-fix EOD payloads are preserved as `quarantined_unsent` and saved
  before SMTP. Sent/unknown-delivery records are never replayed. An old day's
  `eod_sent` flag does not suppress its one source-specific quarantine notice
  when that date is processed after this fix.
- Control Center withholds headline equity, risk-capacity amounts and lane
  learning statistics, including old cached heartbeat equity. Individual trade
  rows remain diagnostic evidence. Malformed state is unavailable, not reseeded.
- Audit checks verify the $1,000 basis, identity, schema, unique trade IDs,
  timestamps, option quantities/fills/fees, individual net P&L, fill-derived cash,
  and aggregate recorded-net cash. Calculations use Decimal with a one-cent
  legacy-rounding tolerance per comparison and at aggregate cash level.
- Stock/unknown trade schemas are flagged rather than guessed as options.
  Open option marks are unverified because legacy records lack mark timestamps.
  Daily realized P&L uses the New York close date; it is never represented as
  daily equity change. No opening baseline is fabricated.
- Newly created states receive distinct ledger IDs and the explicit experiment
  ID. Existing states, trades, cash and identities are not migrated or rewritten
  by the auditor. A wrong/missing funding basis blocks new entries, while readable
  recorded positions remain eligible for existing quote-based exit management.
- `build_eod_learning_packet(..., paper_report=...)` refuses these paper imports,
  even if their status/eligibility flags are hand-edited. There is no existing
  runtime-to-learning consumer in this checkout. This is an explicit ingestion
  guard and machine-readable ineligibility, not provenance enforcement on
  arbitrary manually supplied review items or a complete learning pipeline.

## Read-only reconciliation diagnostic

Use preserved snapshot copies and the actual market date:

```sh
python3 -B scripts/reconcile_paper_reports.py --date 2026-10-12 \
  --source /path/to/preserved-baseline.json \
  --source /path/to/preserved-control-portfolio.json
```

This command only reads files and prints JSON. It does not load the trading
state loader (which can initialize/roll the weekly state), contact providers,
send email, merge books, alter ledgers, or choose an equity. Missing/corrupt files
produce unavailable diagnostics. Exit code 2 means historical reconciliation is
still blocked, even when local arithmetic balances. `file_sha256` hashes raw
file bytes; `snapshot_sha256` hashes canonical JSON at observation time.
Do not publish private state dumps or credentials to GitHub.

## Minimum host evidence and remaining cutover

Collect read-only, preserving each source independently before any migration:

1. Actual process/launchd ProgramArguments, WorkingDirectory, service identity,
   source Git SHA and uncommitted runtime/engine/bridge diff. Defaults are
   `~/Library/LaunchAgents/com.harpcity.traderbrain.paper-options.plist` and
   `~/Library/LaunchAgents/com.harpcity.traderbrain.control.plist`. The live stock
   implementation must be captured, not replaced by the published options code.
2. Both complete state JSONs at the resolved `TB_STATE_PATH` and Control Center
   `--home/portfolio.json`, plus historical snapshots/closed fills and the $500
   retirement/$1,000 funding migration evidence. Capture only the path override
   from configuration, never the secrets file contents.
3. Control Center `control.sqlite3` (settings, heartbeat/events, command history),
   baseline `paper-options.out.log`/`.err.log`, Control Center
   `service.out.log`/`.err.log`, and `deployed-github-sha` if present. A SHA marker
   alone does not prove which source/PID is running.
4. Reconstruct funding/cash/positions/fees from immutable history; identify copied
   versus distinct trades and any documented cash adjustments. Reconcile exact
   stock quantities and option multipliers without double funding or averaging.
   Establish a verified same-source daily opening baseline before computing
   daily return. Select a canonical ledger only on evidence and reviewed migration.
5. Integrate this guard into the actual stock runtime, stop duplicate official
   report publishers through an authorized cutover, verify the running revision
   and source IDs, then verify one authoritative next EOD. Merge/deployment and
   host changes are not performed by this repository patch.

Until those steps are complete, strategy adjustments must not use either report
as the official result. Local paper simulation remains paper-only. Risk limits,
broker permissions, credentials, historical ledger files and live automation
definitions are unchanged.

## Verification

Regression tests cover conflicting reported balances, duplicate sources and
retry stability, fee accounting, fill/P&L corruption, aggregated rounding drift,
legacy seed/identity, unsupported stock rows, malformed/non-finite amounts,
New York day boundaries, missing baselines, legacy outbox preservation, learning
imports, raw-file immutability, API cache quarantine and preserved paper exits.
Run the full engine suite, Control Center suite, source validator, configuration
checks and JavaScript syntax checks. All market/SMTP tests use fake data; passing
CI does not establish deployed source, quote availability or historical NAV.
