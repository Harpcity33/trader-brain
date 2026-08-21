# Architecture and Validation Map

| Requirement | Current path/evidence | Status |
|---|---|---|
| Shared doctrine | `BRAIN.md` | Present |
| 03:55 schedule | `config/schedule.md` | Configured; scheduler absent |
| Massive full universe | adapter contract described in validation summary | Blocked |
| Price/volume gates | `src/trader_brain.py`, boundary tests | Pass |
| 0–100 required factors | `config/scoring-model-v0.2-hypothesis.md`, source/tests | Test pass; uncalibrated |
| Top 5 snapshot | `validation/samples/top5.md` | Synthetic sample only |
| Paper/live isolation | existing `paper/`, `live/`, risk and experiment rules | Schema/documentation pass; broker guard untested |
| EOD review | `validation/samples/eod-review.md` | Synthetic sample only |
| Observations isolation | `research/observations/` | Established |
| Promoted edges | `knowledge/promoted-edges.md` contains policy only | No promoted edge to audit |
| Run evidence | `validation/runs/2026-08-20-synthetic/` | Synthetic; not market validation |

Established paths are retained. The sample validation area maps to operational paths without renaming historical conventions.
