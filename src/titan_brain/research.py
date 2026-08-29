"""Deterministic daily market-study orchestration.

This module deliberately contains no broker or live-automation calls.  It turns
already-collected market and Titan evidence into one create-only daily artifact.
Individual collection/analysis stages are isolated so missing Massive data, a
malformed prior-day record, or an optional route study cannot affect the live
equity desk.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
UNAVAILABLE = "UNAVAILABLE"

RUN = "RUN"
MARKET_CLOSED = "MARKET_CLOSED"
CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
BEFORE_WINDOW = "BEFORE_WINDOW"
DEADLINE_MISSED = "DEADLINE_MISSED"
ALREADY_COMPLETED = "ALREADY_COMPLETED"
COMPLETED = "COMPLETED"

GOOD_PROCESS_GOOD_OUTCOME = "GOOD_PROCESS_GOOD_OUTCOME"
GOOD_PROCESS_BAD_OUTCOME = "GOOD_PROCESS_BAD_OUTCOME"
BAD_PROCESS_GOOD_OUTCOME = "BAD_PROCESS_GOOD_OUTCOME"
BAD_PROCESS_BAD_OUTCOME = "BAD_PROCESS_BAD_OUTCOME"


@dataclass(frozen=True)
class EvidenceWindowSpec:
    """One mandatory interpretation horizon for a daily study."""

    key: str
    label: str
    approximate_calendar_days: int
    importance: str
    purpose: str


EVIDENCE_WINDOWS: tuple[EvidenceWindowSpec, ...] = (
    EvidenceWindowSpec(
        "prior_day",
        "Prior trading day",
        1,
        "very high diagnostic",
        "process, execution, false-positive, and missed-opportunity diagnosis",
    ),
    EvidenceWindowSpec(
        "last_7_days",
        "Last 7 days",
        7,
        "high tactical",
        "near-term setup and execution behavior",
    ),
    EvidenceWindowSpec(
        "last_30_days",
        "Last 30 days",
        30,
        "high regime",
        "current regime, breadth, volatility, and setup expectancy",
    ),
    EvidenceWindowSpec(
        "last_90_days",
        "Last 90 days",
        90,
        "medium/high stability",
        "rolling stability and regime-transition context",
    ),
    EvidenceWindowSpec(
        "six_months",
        "Six months",
        183,
        "medium structural",
        "minimum structural history and walk-forward context",
    ),
    EvidenceWindowSpec(
        "twelve_months",
        "Twelve months",
        365,
        "background/regime comparison",
        "preferred long-horizon comparison when data quality permits",
    ),
)


PRIOR_DAY_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("setup_identified_correctly", "Was the setup correctly identified?"),
    ("direction_correct", "Was the direction correct?"),
    ("timing_correct", "Was timing correct?"),
    ("entry_timing", "Was entry too early or too late?"),
    ("stop_appropriate", "Was the stop appropriate?"),
    ("mae_r", "What was MAE in R?"),
    ("mfe_r", "What was MFE in R?"),
    ("exit_efficiency", "Was the exit efficient?"),
    ("stock_route_outperformed", "Did the stock route outperform?"),
    ("option_route_outperformed", "Did the option route outperform?"),
    ("iv_theta_impact", "Were IV or theta major factors?"),
    ("slippage_variance", "Was slippage materially larger than expected?"),
    ("valid_but_unlucky", "Was the trade valid but unlucky?"),
    ("profitable_bad_process", "Was the trade profitable despite bad process?"),
    ("false_positive", "Was the signal a false positive?"),
    ("missed_opportunity", "Was there a missed qualifying opportunity?"),
)


MARKET_REGIME_FIELDS: tuple[str, ...] = (
    "market_bias",
    "volatility_regime",
    "breadth_condition",
    "leading_sectors",
    "lagging_sectors",
    "gap_environment",
    "risk_level",
)


CANDIDATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("ticker", "Ticker"),
    ("setup_id", "Setup ID"),
    ("setup_score", "SETUP_SCORE"),
    ("setup_score_components", "SETUP_SCORE components"),
    ("execution_score", "EXECUTION_SCORE"),
    ("data_provenance", "Data provenance"),
    ("market_context", "Market context"),
    ("sector_context", "Sector context"),
    ("thesis", "Thesis"),
    ("entry_zone", "Entry zone"),
    ("structural_invalidation", "Structural invalidation"),
    ("targets", "Targets"),
    ("expected_rr", "Expected R/R"),
    ("stock_route", "Stock route evaluation"),
    ("option_route", "Option route evaluation"),
    ("preferred_instrument", "Preferred instrument"),
    ("key_risks", "Key risks"),
    ("live_rejection_reason", "Reason not live-qualified"),
)


class ArtifactExistsError(FileExistsError):
    """Raised when code attempts to replace an immutable daily artifact."""


@dataclass(frozen=True)
class RunGate:
    decision: str
    trading_date: date
    local_time: datetime
    reason: str

    @property
    def should_run(self) -> bool:
        return self.decision == RUN


@dataclass(frozen=True)
class ResearchRunResult:
    status: str
    trading_date: date
    artifact_path: Path | None
    stage_errors: tuple[str, ...] = ()
    message: str = ""


def _as_new_york(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(NEW_YORK)


def gate_research_run(
    now: datetime,
    valid_trading_dates: Iterable[date] | None,
    *,
    unexpected_closures: Iterable[date] = (),
    start_local: time = time(4, 0),
    deadline_local: time = time(6, 30),
) -> RunGate:
    """Return the fail-closed schedule decision for a daily study.

    ``valid_trading_dates`` must come from an authoritative exchange calendar.
    A weekday guess is intentionally not accepted as proof that the market is
    open.  Cron supplies the 04:00 trigger; the artifact path supplies the
    one-run-per-day idempotency key.
    """

    local_now = _as_new_york(now)
    trading_date = local_now.date()
    if valid_trading_dates is None:
        return RunGate(
            CALENDAR_UNAVAILABLE,
            trading_date,
            local_now,
            "authoritative U.S. market calendar is unavailable",
        )

    valid_dates = set(valid_trading_dates)
    closure_dates = set(unexpected_closures)
    if trading_date.weekday() >= 5 or trading_date not in valid_dates or trading_date in closure_dates:
        return RunGate(
            MARKET_CLOSED,
            trading_date,
            local_now,
            "not a valid U.S. trading day or an unexpected closure is in force",
        )
    if local_now.time().replace(tzinfo=None) < start_local:
        return RunGate(BEFORE_WINDOW, trading_date, local_now, "04:00 research window has not opened")
    if local_now.time().replace(tzinfo=None) > deadline_local:
        return RunGate(DEADLINE_MISSED, trading_date, local_now, "06:30 research deadline has passed")
    return RunGate(RUN, trading_date, local_now, "valid trading date and research window")


def normalize_evidence_windows(raw: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return all mandated horizons, explicitly marking absent data."""

    raw = raw if isinstance(raw, Mapping) else {}
    normalized: list[dict[str, Any]] = []
    for spec in EVIDENCE_WINDOWS:
        candidate = raw.get(spec.key)
        if isinstance(candidate, Mapping) and candidate.get("available") is not False:
            metrics = candidate.get("metrics", UNAVAILABLE)
            provenance = candidate.get("provenance", UNAVAILABLE)
            quality = candidate.get("data_quality", UNAVAILABLE)
            normalized.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "importance": spec.importance,
                    "purpose": spec.purpose,
                    "status": "AVAILABLE",
                    "metrics": metrics,
                    "provenance": provenance,
                    "data_quality": quality,
                }
            )
        else:
            reason = (
                candidate.get("unavailable_reason", "source did not supply this horizon")
                if isinstance(candidate, Mapping)
                else "source did not supply this horizon"
            )
            normalized.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "importance": spec.importance,
                    "purpose": spec.purpose,
                    "status": UNAVAILABLE,
                    "metrics": UNAVAILABLE,
                    "provenance": UNAVAILABLE,
                    "data_quality": UNAVAILABLE,
                    "unavailable_reason": reason,
                }
            )
    return normalized


def classify_process_outcome(
    process_adherent: bool | None,
    *,
    realized_r: float | int | None = None,
    outcome_good: bool | None = None,
) -> dict[str, Any]:
    """Classify a prior signal without rewarding profitable rule violations."""

    if outcome_good is None and isinstance(realized_r, (int, float)) and not isinstance(realized_r, bool):
        outcome_good = float(realized_r) > 0.0
    if process_adherent is None or outcome_good is None:
        return {
            "quadrant": UNAVAILABLE,
            "learning_eligible": False,
            "note": "process or outcome evidence is unavailable",
        }

    if process_adherent and outcome_good:
        quadrant = GOOD_PROCESS_GOOD_OUTCOME
    elif process_adherent and not outcome_good:
        quadrant = GOOD_PROCESS_BAD_OUTCOME
    elif not process_adherent and outcome_good:
        quadrant = BAD_PROCESS_GOOD_OUTCOME
    else:
        quadrant = BAD_PROCESS_BAD_OUTCOME

    profitable_rule_violation = quadrant == BAD_PROCESS_GOOD_OUTCOME
    return {
        "quadrant": quadrant,
        "learning_eligible": bool(process_adherent),
        "profitable_rule_violation": profitable_rule_violation,
        "note": (
            "profitable rule violation: diagnose, but never reinforce or promote"
            if profitable_rule_violation
            else "eligible for diagnosis; promotion remains subject to the separate evidence process"
        ),
    }


def audit_prior_day_records(records: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize every prior-day signal, trade, rejection, and missed event."""

    if records is None:
        return []
    audited: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            audited.append(
                {
                    "record_id": f"unparseable-{index + 1}",
                    "record_type": UNAVAILABLE,
                    "quadrant": UNAVAILABLE,
                    "learning_eligible": False,
                    "answers": {key: UNAVAILABLE for key, _ in PRIOR_DAY_QUESTIONS},
                    "unavailable_reason": "record is not a mapping",
                }
            )
            continue

        classification = classify_process_outcome(
            record.get("process_adherent"),
            realized_r=record.get("realized_r"),
            outcome_good=record.get("outcome_good"),
        )
        answers = {key: record.get(key, UNAVAILABLE) for key, _ in PRIOR_DAY_QUESTIONS}
        audited.append(
            {
                "record_id": record.get(
                    "record_id",
                    record.get("trade_id", record.get("signal_id", f"record-{index + 1}")),
                ),
                "record_type": record.get("record_type", UNAVAILABLE),
                "setup_id": record.get("setup_id", UNAVAILABLE),
                "instrument": record.get("instrument", UNAVAILABLE),
                "quadrant": classification["quadrant"],
                "learning_eligible": classification["learning_eligible"],
                "profitable_rule_violation": classification.get("profitable_rule_violation", False),
                "classification_note": classification["note"],
                "answers": answers,
                "source": record.get("source", UNAVAILABLE),
            }
        )
    return audited


def read_prior_day_artifacts(paths: Mapping[str, str | Path] | None) -> list[dict[str, Any]]:
    """Read immutable prior-day artifacts without inventing absent records.

    JSON and JSONL inputs are expanded into structured records.  Markdown or
    other text remains traceable as a source record, but analytical fields are
    marked unavailable until a structured producer supplies them.
    """

    if not paths:
        return []
    records: list[dict[str, Any]] = []
    for artifact_type, raw_path in paths.items():
        path = Path(raw_path)
        if not path.is_file():
            records.append(
                {
                    "record_id": f"missing:{artifact_type}",
                    "record_type": artifact_type,
                    "source": str(path),
                    "unavailable_reason": "prior-day artifact is missing",
                }
            )
            continue
        try:
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                items = payload if isinstance(payload, list) else [payload]
            elif path.suffix.lower() == ".jsonl":
                items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            else:
                items = [
                    {
                        "record_id": f"text:{artifact_type}",
                        "record_type": artifact_type,
                        "source": str(path),
                        "raw_text": path.read_text(encoding="utf-8"),
                    }
                ]
            for item in items:
                if isinstance(item, Mapping):
                    record = dict(item)
                else:
                    record = {"raw_value": item}
                record.setdefault("record_type", artifact_type)
                record.setdefault("source", str(path))
                records.append(record)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            records.append(
                {
                    "record_id": f"unreadable:{artifact_type}",
                    "record_type": artifact_type,
                    "source": str(path),
                    "unavailable_reason": f"artifact could not be read: {type(exc).__name__}",
                }
            )
    return records


def normalize_tactical_adjustments(
    adjustments: Sequence[Mapping[str, Any]] | None,
    trading_date: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach EOD expiry to evidence-backed daily changes only."""

    expiration = datetime.combine(trading_date, time(23, 59, 59), tzinfo=NEW_YORK).isoformat()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, adjustment in enumerate(adjustments or ()):
        if not isinstance(adjustment, Mapping):
            rejected.append({"index": index, "reason": "adjustment is not a mapping"})
            continue
        missing = [field for field in ("parameter", "value", "reason", "evidence") if not adjustment.get(field)]
        if missing:
            rejected.append(
                {
                    "index": index,
                    "parameter": adjustment.get("parameter", UNAVAILABLE),
                    "reason": f"missing required fields: {', '.join(missing)}",
                }
            )
            continue
        normalized = dict(adjustment)
        normalized["scope"] = "DAILY_TACTICAL_ADJUSTMENT"
        normalized["expiration"] = expiration
        normalized["promotion_effect"] = "NONE"
        accepted.append(normalized)
    return accepted, rejected


def select_top_candidates(
    candidates: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select at most five standards-qualified candidates without padding."""

    if limit < 0 or limit > 5:
        raise ValueError("candidate limit must be between 0 and 5")
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates or ()):
        if not isinstance(candidate, Mapping):
            rejected.append({"candidate": f"index-{index}", "reason": "candidate is not a mapping"})
            continue
        item = dict(candidate)
        ticker = item.get("ticker", f"index-{index}")
        if item.get("standards_met") is not True:
            rejected.append(
                {
                    "candidate": ticker,
                    "reason": item.get("rejection_reason", "research standards were not met"),
                }
            )
            continue
        rank_value = item.get("estimated_attractiveness")
        if not isinstance(rank_value, (int, float)) or isinstance(rank_value, bool):
            rejected.append({"candidate": ticker, "reason": "estimated_attractiveness is unavailable"})
            continue
        eligible.append(item)
    eligible.sort(key=lambda candidate: (-float(candidate["estimated_attractiveness"]), str(candidate.get("ticker", ""))))
    selected = eligible[:limit]
    for overflow in eligible[limit:]:
        rejected.append({"candidate": overflow.get("ticker", UNAVAILABLE), "reason": "ranked below Top 5 cutoff"})
    return selected, rejected


def write_immutable_artifact(path: str | Path, content: str) -> Path:
    """Atomically create a daily artifact and never overwrite an existing one."""

    final_path = Path(path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        raise ArtifactExistsError(str(final_path))

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{final_path.name}.", dir=final_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, final_path)
        except FileExistsError as exc:
            raise ArtifactExistsError(str(final_path)) from exc
        final_path.chmod(0o444)
    finally:
        temporary_path.unlink(missing_ok=True)
    return final_path


def _display(value: Any) -> str:
    if value is None:
        return UNAVAILABLE
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, Mapping):
        if not value:
            return UNAVAILABLE
        return "; ".join(f"{key}={_display(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        if not value:
            return UNAVAILABLE
        return "; ".join(_display(item) for item in value)
    rendered = str(value).strip()
    return rendered if rendered else UNAVAILABLE


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    def clean(value: Any) -> str:
        return _display(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(clean(value) for value in row) + " |" for row in rows)
    return lines


def render_daily_strategy(
    *,
    trading_date: date,
    created_at: datetime,
    market_regime: Mapping[str, Any],
    evidence_windows: Sequence[Mapping[str, Any]],
    prior_audit: Sequence[Mapping[str, Any]],
    preferred_setups: Sequence[Any],
    avoid_setups: Sequence[Any],
    tactical_adjustments: Sequence[Mapping[str, Any]],
    rejected_adjustments: Sequence[Mapping[str, Any]],
    promoted_edges: Sequence[Mapping[str, Any]],
    top_candidates: Sequence[Mapping[str, Any]],
    candidate_rejections: Sequence[Mapping[str, Any]],
    stage_errors: Sequence[str],
) -> str:
    """Render the immutable, auditable daily strategy document."""

    local_created = _as_new_york(created_at)
    lines = [
        f"# Titan Daily Strategy — {trading_date.isoformat()}",
        "",
        f"- Created: {local_created.isoformat()}",
        f"- Analysis deadline: {trading_date.isoformat()}T06:30:00 America/New_York",
        "- Artifact policy: immutable / create-only",
        "- Production effect: advisory; no broker authority and no permanent-rule mutation",
        "- Baseline history: six months minimum, twelve months preferred when available",
        "",
        "## Market regime",
        "",
    ]
    lines.extend(f"- {field.replace('_', ' ').title()}: {_display(market_regime.get(field, UNAVAILABLE))}" for field in MARKET_REGIME_FIELDS)

    lines.extend(["", "## Evidence horizons", ""])
    lines.extend(
        _markdown_table(
            ("Window", "Importance", "Status", "Data quality", "Metrics", "Provenance / unavailable reason"),
            [
                (
                    item.get("label"),
                    item.get("importance"),
                    item.get("status"),
                    item.get("data_quality"),
                    item.get("metrics"),
                    item.get("provenance", item.get("unavailable_reason", UNAVAILABLE)),
                )
                for item in evidence_windows
            ],
        )
    )

    lines.extend(["", "## Lessons from yesterday", ""])
    if not prior_audit:
        lines.append("Prior-day structured artifacts: UNAVAILABLE. No lesson is inferred from absent evidence.")
    else:
        counts = Counter(item.get("quadrant", UNAVAILABLE) for item in prior_audit)
        lines.append("Process/outcome quadrants:")
        lines.append("")
        for quadrant in (
            GOOD_PROCESS_GOOD_OUTCOME,
            GOOD_PROCESS_BAD_OUTCOME,
            BAD_PROCESS_GOOD_OUTCOME,
            BAD_PROCESS_BAD_OUTCOME,
            UNAVAILABLE,
        ):
            lines.append(f"- {quadrant}: {counts.get(quadrant, 0)}")
        lines.append("")
        for item in prior_audit:
            lines.extend(
                [
                    f"### {_display(item.get('record_id'))} — {_display(item.get('quadrant'))}",
                    "",
                    f"- Type / setup / instrument: {_display(item.get('record_type'))} / {_display(item.get('setup_id'))} / {_display(item.get('instrument'))}",
                    f"- Learning eligible: {_display(item.get('learning_eligible'))}",
                    f"- Guardrail: {_display(item.get('classification_note'))}",
                    f"- Source: {_display(item.get('source'))}",
                    "",
                ]
            )
            answers = item.get("answers", {})
            for key, question in PRIOR_DAY_QUESTIONS:
                lines.append(f"- {question} {_display(answers.get(key, UNAVAILABLE))}")

    lines.extend(["", "## Today's preferred setups", ""])
    if preferred_setups:
        lines.extend(f"{index}. {_display(setup)}" for index, setup in enumerate(preferred_setups, start=1))
    else:
        lines.append("None met the evidence standard; the list is intentionally not padded.")

    lines.extend(["", "## Today's setups to avoid", ""])
    if avoid_setups:
        lines.extend(f"- {_display(setup)}" for setup in avoid_setups)
    else:
        lines.append("No evidence-backed avoidance condition was supplied.")

    lines.extend(["", "## Daily tactical adjustments — expire at EOD", ""])
    if tactical_adjustments:
        lines.extend(
            _markdown_table(
                ("Parameter", "Value", "Reason", "Evidence", "Expiration", "Promotion effect"),
                [
                    (
                        item.get("parameter"),
                        item.get("value"),
                        item.get("reason"),
                        item.get("evidence"),
                        item.get("expiration"),
                        item.get("promotion_effect"),
                    )
                    for item in tactical_adjustments
                ],
            )
        )
    else:
        lines.append("No validated daily departure from baseline.")
    if rejected_adjustments:
        lines.append("")
        lines.append("Rejected adjustment proposals (not applied):")
        lines.append("")
        lines.extend(f"- {_display(item)}" for item in rejected_adjustments)

    lines.extend(["", "## Permanent promoted edges — read-only", ""])
    lines.append("This run cannot promote, edit, or delete a permanent edge.")
    lines.append("")
    if promoted_edges:
        lines.extend(f"- {_display(edge)}" for edge in promoted_edges)
    else:
        lines.append("- No promoted-edge state was supplied; status is UNAVAILABLE, not empty-by-inference.")

    lines.extend(["", "## Top candidates", ""])
    if not top_candidates:
        lines.append("NO TRADE candidates met the research standard. No placeholder names were manufactured.")
    for rank, candidate in enumerate(top_candidates, start=1):
        lines.extend(["", f"### {rank}. {_display(candidate.get('ticker'))}", ""])
        for key, label in CANDIDATE_FIELDS:
            lines.append(f"- {label}: {_display(candidate.get(key, UNAVAILABLE))}")
        lines.append(f"- Live qualified: {_display(candidate.get('live_qualified', False))}")

    lines.extend(["", "## Candidate exclusions", ""])
    if candidate_rejections:
        lines.extend(f"- {_display(item)}" for item in candidate_rejections)
    else:
        lines.append("None recorded.")

    lines.extend(["", "## Data availability and isolated failures", ""])
    if stage_errors:
        lines.extend(f"- {error}" for error in stage_errors)
    else:
        lines.append("- No stage failure was recorded.")
    lines.extend(
        [
            "",
            "## Safety boundary",
            "",
            "This artifact is research only. It cannot pause, replace, or mutate the existing equity heartbeat; place, cancel, or replace any broker order; or promote a one-day observation into production. Missing data remains UNAVAILABLE and cannot be fabricated or treated as a passing gate.",
            "",
        ]
    )
    return "\n".join(lines)


class DailyResearchEngine:
    """Create one partial-failure-tolerant daily strategy artifact."""

    def __init__(self, artifact_root: str | Path):
        self.artifact_root = Path(artifact_root)

    @staticmethod
    def _stage(
        name: str,
        operation: Callable[[], Any],
        fallback: Any,
        errors: list[str],
    ) -> Any:
        try:
            return operation()
        except Exception as exc:  # isolation boundary intentionally catches provider/parser errors
            errors.append(f"{name}: UNAVAILABLE ({type(exc).__name__}: {exc})")
            return fallback

    def run(
        self,
        *,
        now: datetime,
        valid_trading_dates: Iterable[date] | None,
        inputs: Mapping[str, Any] | None = None,
        unexpected_closures: Iterable[date] = (),
    ) -> ResearchRunResult:
        """Run the daily workflow after an authoritative schedule gate.

        Collector values in ``inputs`` may be direct values or zero-argument
        callables.  Each callable is evaluated inside its own failure boundary.
        Prior-day artifacts are always resolved before market evidence.
        """

        gate = gate_research_run(
            now,
            valid_trading_dates,
            unexpected_closures=unexpected_closures,
        )
        if not gate.should_run:
            return ResearchRunResult(
                status=gate.decision,
                trading_date=gate.trading_date,
                artifact_path=None,
                message=gate.reason,
            )

        artifact_path = self.artifact_root / f"{gate.trading_date.isoformat()}.md"
        if artifact_path.exists():
            return ResearchRunResult(
                status=ALREADY_COMPLETED,
                trading_date=gate.trading_date,
                artifact_path=artifact_path,
                message="immutable artifact already exists; no second run was written",
            )

        supplied: Mapping[str, Any] = inputs if isinstance(inputs, Mapping) else {}
        errors: list[str] = []

        def resolve(key: str, default: Any) -> Any:
            value = supplied.get(key, default)
            return value() if callable(value) else value

        # Required ordering: yesterday first, then the current market study.
        prior_records = self._stage(
            "prior_day_artifacts",
            lambda: read_prior_day_artifacts(resolve("prior_day_artifact_paths", {}))
            + list(resolve("prior_day_records", [])),
            [],
            errors,
        )
        prior_audit = self._stage(
            "prior_day_audit",
            lambda: audit_prior_day_records(prior_records),
            [],
            errors,
        )
        evidence = self._stage(
            "market_evidence",
            lambda: normalize_evidence_windows(resolve("evidence_windows", {})),
            normalize_evidence_windows({}),
            errors,
        )
        market_regime = self._stage(
            "market_regime",
            lambda: dict(resolve("market_regime", {})),
            {},
            errors,
        )
        preferred_setups = self._stage(
            "preferred_setups",
            lambda: list(resolve("preferred_setups", [])),
            [],
            errors,
        )
        avoid_setups = self._stage(
            "avoid_setups",
            lambda: list(resolve("avoid_setups", [])),
            [],
            errors,
        )
        tactical_adjustments, rejected_adjustments = self._stage(
            "tactical_adjustments",
            lambda: normalize_tactical_adjustments(
                resolve("tactical_adjustments", []),
                gate.trading_date,
            ),
            ([], []),
            errors,
        )
        promoted_edges = self._stage(
            "promoted_edges",
            lambda: list(resolve("promoted_edges", [])),
            [],
            errors,
        )
        top_candidates, candidate_rejections = self._stage(
            "candidate_ranking",
            lambda: select_top_candidates(resolve("candidates", [])),
            ([], []),
            errors,
        )

        markdown = render_daily_strategy(
            trading_date=gate.trading_date,
            created_at=gate.local_time,
            market_regime=market_regime,
            evidence_windows=evidence,
            prior_audit=prior_audit,
            preferred_setups=preferred_setups,
            avoid_setups=avoid_setups,
            tactical_adjustments=tactical_adjustments,
            rejected_adjustments=rejected_adjustments,
            promoted_edges=promoted_edges,
            top_candidates=top_candidates,
            candidate_rejections=candidate_rejections,
            stage_errors=errors,
        )
        try:
            written = write_immutable_artifact(artifact_path, markdown)
        except ArtifactExistsError:
            return ResearchRunResult(
                status=ALREADY_COMPLETED,
                trading_date=gate.trading_date,
                artifact_path=artifact_path,
                stage_errors=tuple(errors),
                message="another run created the immutable artifact first",
            )

        return ResearchRunResult(
            status=COMPLETED,
            trading_date=gate.trading_date,
            artifact_path=written,
            stage_errors=tuple(errors),
            message="daily research artifact created without broker or live-heartbeat mutation",
        )


__all__ = [
    "ALREADY_COMPLETED",
    "ArtifactExistsError",
    "BAD_PROCESS_BAD_OUTCOME",
    "BAD_PROCESS_GOOD_OUTCOME",
    "BEFORE_WINDOW",
    "CALENDAR_UNAVAILABLE",
    "COMPLETED",
    "DailyResearchEngine",
    "EVIDENCE_WINDOWS",
    "GOOD_PROCESS_BAD_OUTCOME",
    "GOOD_PROCESS_GOOD_OUTCOME",
    "MARKET_CLOSED",
    "DEADLINE_MISSED",
    "ResearchRunResult",
    "RunGate",
    "UNAVAILABLE",
    "audit_prior_day_records",
    "classify_process_outcome",
    "gate_research_run",
    "normalize_evidence_windows",
    "normalize_tactical_adjustments",
    "read_prior_day_artifacts",
    "render_daily_strategy",
    "select_top_candidates",
    "write_immutable_artifact",
]
