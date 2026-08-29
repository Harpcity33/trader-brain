# Scoring Model v0.2.0 — Unvalidated Hypothesis

This version exists to cover every required factor while preserving the prior model in `config/scoring-model.md`. It is not a promoted trading rule.

| Component | Cap |
|---|---:|
| Liquidity | 15 |
| Relative volume | 15 |
| Price action / technical structure, including VWAP | 20 |
| Catalyst / context | 15 |
| Sector / market sympathy | 10 |
| Prior 90-day move behavior | 15 |
| Gap behavior | 5 |
| Other Massive data | 5 |
| Total | 100 |

Each input is normalized to [0,1], clipped to that interval, and multiplied by its cap. Missing inputs receive 0.5 temporarily and are listed in `missing_factors`; production calibration must replace this hypothesis. Fresh-news absence is neutral and never an eligibility gate. Totals are capped by construction at 100. Ties sort by ticker only in the test implementation; production should use execution quality then invalidation clarity.
