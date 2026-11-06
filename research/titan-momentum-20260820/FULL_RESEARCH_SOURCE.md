# Titan Momentum Research — Exact Source Handoff

This file intentionally points Codex to the **original complete research package**, not a summarized or rewritten derivative.

## Original Dropbox folder

https://www.dropbox.com/scl/fo/7wsq8kuwkg48pvilfgbwo/AJy8F6VYiHbOb9d_hR8y66U?rlkey=o2u29hbjukcne9cz3w1oiclmp&st=8sd4f558&dl=0

For a direct folder download attempt, use the same shared-folder URL with `dl=1`:

https://www.dropbox.com/scl/fo/7wsq8kuwkg48pvilfgbwo/AJy8F6VYiHbOb9d_hR8y66U?rlkey=o2u29hbjukcne9cz3w1oiclmp&st=8sd4f558&dl=1

## Codex instruction

**Do not substitute `INGEST.md` or `runtime-proposals.json` for the source research.** Pull and inspect the complete Dropbox package first. Preserve the original directory hierarchy and use the original research outputs as primary evidence.

Start with the source package's own prescribed order:

1. `handoff/study_report.md` — read section 0 and section 0b first.
2. `handoff/quality_report.md` — leakage audit, gaps and entitlement limits.
3. `handoff/live_prompt_change_log.md` — proposed rule changes with evidence, sample size, confidence and disposition.
4. `handoff/top_examples.md` — worked examples using decision-time information.
5. `handoff/next_research_queue.md` — unresolved questions ranked by decision value.
6. Then inspect machine-readable outputs and the full pipeline source/tests before drawing implementation conclusions.

## Complete package layout observed

- `README.md`
- `CHECKSUMS.sha256`
- `requirements.txt`
- `logs/run.log`
- `tests/test_features.py`
- `src/` — full Python research pipeline, including capability probing, universe/event construction, bars/context/reference data, RVOL baseline, feature building, setup/trigger detection, spread backfill, follow-through, rejection analysis, under-$5 analysis, options analysis, quality audit, exhaustion, score anatomy, sensitivity, exits/PCS, reporting and handoff builders.
- `handoff/README.md`
- `handoff/RESUME.md`
- `handoff/study_report.md`
- `handoff/quality_report.md`
- `handoff/live_prompt_change_log.md`
- `handoff/top_examples.md`
- `handoff/next_research_queue.md`
- `handoff/excluded_raw_data_manifest.md`
- `handoff/data_dictionary.csv`
- `handoff/database_schema.csv`
- `handoff/checksums.sha256`
- `handoff/run_status.json`
- `handoff/titan_runtime_parameters.json`
- `handoff/analysis_results.json`
- `handoff/sensitivity_results.json`
- `handoff/armed_sensitivity.json`
- `handoff/score_anatomy.json`
- `handoff/exhaustion.json`
- `handoff/under5_gates.json`
- `handoff/exits_and_pcs.json`
- `handoff/phase0_probe_raw.json`
- `handoff/source_request_manifest.jsonl`
- `manifests/source_request_manifest.jsonl`
- `handoff/parquet/study_events.parquet`
- `handoff/parquet/equity_bars_1m.parquet`
- `handoff/parquet/rejections_and_failures.parquet`
- `handoff/parquet/candidate_observations.parquet`
- `handoff/parquet/corporate_actions_and_listing_risk.parquet`
- `handoff/parquet/instrument_master.parquet`
- `handoff/parquet/setup_bases_and_triggers.parquet`
- `handoff/parquet/nbbo_spread_samples.parquet`
- `handoff/parquet/catalyst_events.parquet`
- `handoff/parquet/daily_market_bars.parquet`
- `handoff/parquet/option_stock_comparison.parquet`
- `handoff/parquet/filing_and_dilution_flags.parquet`
- `handoff/parquet/outcome_labels.parquet`

## Important source facts

The root README identifies this as a self-contained research-only package and states that the complete folder contains the sealed handoff, 13 Parquet fact tables, the full pipeline, tests, request manifests, logs and dependency list. It also states that the omitted DuckDB database is approximately 2.38 GB and is regenerable from the source/request metadata.

The source README explicitly says **no edge was demonstrated**: its cost-aware, leakage-audited out-of-sample measurement was negative after measured round-trip bid/ask spread. Codex must therefore treat this package as an evidence base and research input, not a green light to loosen execution safeguards.

## GitHub size/licensing note

Some original Parquet files are approximately 100–423 MB each and the source package marks raw market data as licensed/internal-use. They are intentionally referenced from the owner's Dropbox source rather than duplicated into normal GitHub contents. The full original research remains available through the Dropbox package above.

## Verification

Use the package's `CHECKSUMS.sha256` / `handoff/checksums.sha256` when copying or staging individual research files. Do not silently alter source files during ingestion.
