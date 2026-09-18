"""Durable one-shot attended control for the release-bound IBKR runtime.

Reviews and claims are persisted outside the immutable release.  A confirmation
always obtains a new runtime review, compares every approval-relevant field,
and writes an exclusive durable claim before the single dispatch call.  A
claim with no conclusive outcome is never retryable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import UUID

from .broker.base import (
    AttendedCancelReview,
    AttendedLocalReview,
    BrokerCapabilities,
    BrokerError,
    BrokerOperationResult,
    BrokerSide,
    BrokerUnknownSubmission,
    EquityOrderType,
    MarketHours,
    OrderCheck,
    OrderRequest,
    TimeInForce,
)


REVIEW_SCHEMA = "titan_ibkr_attended_review_2026-09-14_v1"
CLAIM_SCHEMA = "titan_ibkr_attended_claim_2026-09-14_v1"
OUTCOME_SCHEMA = "titan_ibkr_attended_outcome_2026-09-14_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_KEY = "ibkr-live-ending-3103"
_MAX_RECORD_BYTES = 1024 * 1024
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {"exact_account_id", "account_id", "account_number", "broker_account_number"}
)


class AttendedControlError(RuntimeError):
    """Stable failure code; broker/private exception text is never exposed."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"IBKR_ATTENDED_[A-Z0-9_]{1,96}", normalized):
            raise ValueError("invalid attended-control failure code")
        self.code = normalized
        super().__init__(normalized)


@runtime_checkable
class AttendedIbkrRuntime(Protocol):
    """Release-shipped facade; the exact account stays inside this object."""

    @property
    def account_key(self) -> str: ...

    @property
    def account_masked(self) -> str: ...

    @property
    def capabilities(self) -> BrokerCapabilities: ...

    def review_order(self, request: OrderRequest) -> AttendedLocalReview: ...

    def prepare_mutation(self) -> None: ...

    def place_order(
        self,
        request: OrderRequest,
        review: AttendedLocalReview,
        exact_confirmation: str,
    ) -> BrokerOperationResult: ...

    def review_cancel(self, broker_order_id: str) -> AttendedCancelReview: ...

    def cancel_order(
        self,
        broker_order_id: str,
        review: AttendedCancelReview,
        exact_confirmation: str,
    ) -> BrokerOperationResult: ...

    def review_protection(
        self,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> AttendedLocalReview: ...


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AttendedControlError(f"IBKR_ATTENDED_{field.upper()}_INVALID")
    return value.astimezone(timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _json_value(value: object, *, key: str | None = None) -> object:
    if key is not None and key.lower() in _FORBIDDEN_PERSISTED_KEYS:
        raise AttendedControlError("IBKR_ATTENDED_PERSISTED_ACCOUNT_IDENTIFIER_FORBIDDEN")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, item in value.items():
            name = str(raw_key)
            if not name or name in normalized:
                raise AttendedControlError("IBKR_ATTENDED_REVIEW_PAYLOAD_INVALID")
            normalized[name] = _json_value(item, key=name)
        return {name: normalized[name] for name in sorted(normalized)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise AttendedControlError("IBKR_ATTENDED_REVIEW_PAYLOAD_INVALID")


def _request_payload(request: OrderRequest) -> dict[str, object]:
    if not isinstance(request, OrderRequest):
        raise AttendedControlError("IBKR_ATTENDED_ORDER_REQUEST_INVALID")
    return {
        "account_masked": request.account_masked,
        "symbol": request.symbol,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": request.quantity,
        "market_hours": request.market_hours.value,
        "time_in_force": request.time_in_force.value,
        "client_ref_id": request.client_ref_id,
        "limit_price": (
            format(request.limit_price, "f") if request.limit_price is not None else None
        ),
        "stop_price": (
            format(request.stop_price, "f") if request.stop_price is not None else None
        ),
    }


def _request_from_payload(raw: object) -> OrderRequest:
    if not isinstance(raw, Mapping) or set(raw) != {
        "account_masked",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "market_hours",
        "time_in_force",
        "client_ref_id",
        "limit_price",
        "stop_price",
    }:
        raise AttendedControlError("IBKR_ATTENDED_STORED_REQUEST_INVALID")
    try:
        return OrderRequest(
            account_masked=str(raw["account_masked"]),
            symbol=str(raw["symbol"]),
            side=BrokerSide(str(raw["side"])),
            order_type=EquityOrderType(str(raw["order_type"])),
            quantity=raw["quantity"],
            market_hours=MarketHours(str(raw["market_hours"])),
            time_in_force=TimeInForce(str(raw["time_in_force"])),
            client_ref_id=str(raw["client_ref_id"]),
            limit_price=raw["limit_price"],
            stop_price=raw["stop_price"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AttendedControlError("IBKR_ATTENDED_STORED_REQUEST_INVALID") from exc


def _request_from_stop_preview(raw: object, *, account_masked: str) -> OrderRequest:
    expected = {
        "symbol",
        "side",
        "order_type",
        "quantity",
        "market_hours",
        "time_in_force",
        "client_ref_id",
        "limit_price",
        "stop_price",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise AttendedControlError("IBKR_ATTENDED_PROTECTION_PLAN_INVALID")
    return _request_from_payload({"account_masked": account_masked, **dict(raw)})


def _checks_payload(checks: tuple[OrderCheck, ...]) -> list[dict[str, str]]:
    if any(not isinstance(check, OrderCheck) for check in checks):
        raise AttendedControlError("IBKR_ATTENDED_ORDER_CHECKS_INVALID")
    return [
        {"code": check.code, "severity": check.severity, "message": check.message}
        for check in checks
    ]


def _order_review_payload(review: AttendedLocalReview) -> dict[str, object]:
    if not isinstance(review, AttendedLocalReview) or review.expires_at is None:
        raise AttendedControlError("IBKR_ATTENDED_LOCAL_REVIEW_REQUIRED")
    return {
        "request": _request_payload(review.request),
        "reviewed_at": review.reviewed_at.isoformat(),
        "received_at": review.received_at.isoformat(),
        "expires_at": review.expires_at.isoformat(),
        "disclosure": review.disclosure,
        "order_checks": _checks_payload(review.order_checks),
        "required_confirmation_phrase": review.required_confirmation_phrase,
        "preview": _json_value(review.preview),
        "decision_id": review.decision_id,
        "policy_binding_id": review.policy_binding_id,
        "evidence_collection_id": review.evidence_collection_id,
        "provider_contract_id": review.provider_contract_id,
        "broker_bound": False,
        "broker_review_id": None,
    }


def _cancel_review_payload(review: AttendedCancelReview) -> dict[str, object]:
    if not isinstance(review, AttendedCancelReview):
        raise AttendedControlError("IBKR_ATTENDED_CANCEL_REVIEW_REQUIRED")
    return {
        "decision_id": review.decision_id,
        "account_masked": review.account_masked,
        "broker_order_id": review.broker_order_id,
        "client_ref_id": review.client_ref_id,
        "reviewed_at": review.reviewed_at.isoformat(),
        "received_at": review.received_at.isoformat(),
        "expires_at": review.expires_at.isoformat(),
        "disclosure": review.disclosure,
        "order_checks": _checks_payload(review.order_checks),
        "required_confirmation_phrase": review.required_confirmation_phrase,
        "preview": _json_value(review.preview),
    }


def _order_approval_material(review: AttendedLocalReview) -> bytes:
    payload = _order_review_payload(review)
    for volatile in (
        "decision_id",
        "evidence_collection_id",
        "reviewed_at",
        "received_at",
        "expires_at",
    ):
        payload.pop(volatile)
    return _canonical(payload)


def _cancel_approval_material(review: AttendedCancelReview) -> bytes:
    payload = _cancel_review_payload(review)
    for volatile in ("decision_id", "reviewed_at", "received_at", "expires_at"):
        payload.pop(volatile)
    return _canonical(payload)


@dataclass(frozen=True)
class AttendedReviewStore:
    root: Path
    release_manifest_hash: str
    config_hash: str
    policy_hash: str
    account_key: str
    account_masked: str

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        object.__setattr__(self, "root", root)
        if self.account_key != _ACCOUNT_KEY:
            raise AttendedControlError("IBKR_ATTENDED_ACCOUNT_KEY_MISMATCH")
        if not re.fullmatch(r"(?:\*{4}|•{4})3103", self.account_masked):
            raise AttendedControlError("IBKR_ATTENDED_ACCOUNT_MASK_MISMATCH")
        for value in (
            self.release_manifest_hash,
            self.config_hash,
            self.policy_hash,
        ):
            if not _SHA256.fullmatch(str(value)):
                raise AttendedControlError("IBKR_ATTENDED_RELEASE_BINDING_INVALID")
        for name in ("reviews", "claims", "outcomes"):
            path = root / "control/attended" / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise AttendedControlError("IBKR_ATTENDED_STORAGE_UNSAFE")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

    @property
    def bindings(self) -> dict[str, str]:
        return {
            "release_manifest_hash": self.release_manifest_hash,
            "config_hash": self.config_hash,
            "policy_hash": self.policy_hash,
            "account_key": self.account_key,
            "account_masked": self.account_masked,
        }

    def _path(self, collection: str, review_id: str) -> Path:
        if collection not in {"reviews", "claims", "outcomes"} or not _SHA256.fullmatch(
            str(review_id)
        ):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_ID_INVALID")
        return self.root / "control/attended" / collection / f"{review_id}.json"

    @staticmethod
    def _write_exclusive(path: Path, payload: Mapping[str, object]) -> None:
        data = _canonical(payload) + b"\n"
        if len(data) > _MAX_RECORD_BYTES:
            raise AttendedControlError("IBKR_ATTENDED_RECORD_TOO_LARGE")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_ALREADY_CLAIMED_NONRETRYABLE") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            # Never delete a possibly durable claim. Its existence is the
            # conservative no-retry signal after any ambiguous local failure.
            raise

    def create_review(self, body: Mapping[str, object]) -> dict[str, object]:
        normalized = dict(_json_value(body))
        normalized.update(self.bindings)
        review_id = hashlib.sha256(_canonical(normalized)).hexdigest()
        record = {**normalized, "review_id": review_id}
        self._write_exclusive(self._path("reviews", review_id), record)
        return record

    def load_review(self, review_id: str) -> dict[str, object]:
        path = self._path("reviews", review_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_NOT_FOUND_OR_UNSAFE")
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED") from exc
        if not isinstance(raw, dict) or raw.get("review_id") != review_id:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED")
        body = dict(raw)
        body.pop("review_id", None)
        if hashlib.sha256(_canonical(body)).hexdigest() != review_id:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED")
        if any(raw.get(key) != value for key, value in self.bindings.items()):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_RELEASE_BINDING_CHANGED")
        return raw

    def claim(self, review_id: str, *, claimed_at: datetime, phrase: str) -> None:
        body = {
            "schema_version": CLAIM_SCHEMA,
            "review_id": review_id,
            **self.bindings,
            "claimed_at": _utc(claimed_at, "claim_time").isoformat(),
            "confirmation_sha256": hashlib.sha256(phrase.encode("utf-8")).hexdigest(),
            "retry_allowed": False,
        }
        self._write_exclusive(self._path("claims", review_id), body)

    def load_claim(self, review_id: str) -> dict[str, object]:
        """Load and strictly validate the exclusive confirmation claim.

        A claim proves only that the exact stored review was consumed.  It is
        deliberately not treated as broker acceptance or fill evidence.
        """

        path = self._path("claims", review_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            raise AttendedControlError("IBKR_ATTENDED_ENTRY_NOT_CONFIRMED")
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AttendedControlError("IBKR_ATTENDED_CLAIM_TAMPERED") from exc
        expected = {
            "schema_version",
            "review_id",
            *self.bindings,
            "claimed_at",
            "confirmation_sha256",
            "retry_allowed",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != expected
            or raw.get("schema_version") != CLAIM_SCHEMA
            or raw.get("review_id") != review_id
            or raw.get("retry_allowed") is not False
            or not _SHA256.fullmatch(str(raw.get("confirmation_sha256", "")))
            or any(raw.get(key) != value for key, value in self.bindings.items())
        ):
            raise AttendedControlError("IBKR_ATTENDED_CLAIM_TAMPERED")
        try:
            _utc(datetime.fromisoformat(str(raw["claimed_at"])), "claim_time")
        except (KeyError, ValueError) as exc:
            raise AttendedControlError("IBKR_ATTENDED_CLAIM_TAMPERED") from exc
        return raw

    def record_outcome(self, review_id: str, body: Mapping[str, object]) -> None:
        payload = {
            "schema_version": OUTCOME_SCHEMA,
            **dict(_json_value(body)),
            "review_id": review_id,
            **self.bindings,
            "retry_allowed": False,
        }
        self._write_exclusive(self._path("outcomes", review_id), payload)

    def outcome_exists(self, review_id: str) -> bool:
        path = self._path("outcomes", review_id)
        return path.is_file() and not path.is_symlink()

    def claim_exists(self, review_id: str) -> bool:
        path = self._path("claims", review_id)
        return path.is_file() and not path.is_symlink()


class AttendedOrderControl:
    """Persist reviews and consume confirmations through one runtime facade."""

    def __init__(
        self,
        store: AttendedReviewStore,
        runtime: AttendedIbkrRuntime,
        *,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(runtime, AttendedIbkrRuntime):
            raise AttendedControlError("IBKR_ATTENDED_RUNTIME_UNAVAILABLE")
        self.store = store
        self.runtime = runtime
        self._clock = clock
        if runtime.account_key != store.account_key or runtime.account_masked != store.account_masked:
            raise AttendedControlError("IBKR_ATTENDED_RUNTIME_ACCOUNT_MISMATCH")
        capabilities = runtime.capabilities
        if not isinstance(capabilities, BrokerCapabilities) or not all(
            (
                capabilities.supports_equity_review,
                capabilities.supports_equity_place,
                capabilities.supports_equity_cancel,
                capabilities.supports_daemon_writes,
                not capabilities.supports_unattended_writes,
                not capabilities.supports_atomic_protection,
                capabilities.review_requires_explicit_confirmation,
                capabilities.cancel_requires_explicit_confirmation,
                capabilities.supported_market_hours == (MarketHours.REGULAR,),
            )
        ):
            raise AttendedControlError("IBKR_ATTENDED_RUNTIME_CAPABILITIES_UNACCEPTED")

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock")

    def _validate_request(self, request: OrderRequest, purpose: str) -> None:
        if request.account_masked != self.store.account_masked:
            raise AttendedControlError("IBKR_ATTENDED_ORDER_ACCOUNT_MISMATCH")
        if request.market_hours is not MarketHours.REGULAR:
            raise AttendedControlError("IBKR_ATTENDED_PREMARKET_ORDER_FORBIDDEN")
        if purpose == "entry" and not (
            request.side is BrokerSide.BUY
            and request.order_type is EquityOrderType.LIMIT
            and request.time_in_force is TimeInForce.GFD
        ):
            raise AttendedControlError("IBKR_ATTENDED_ENTRY_TUPLE_INVALID")
        if purpose == "protection" and not (
            request.side is BrokerSide.SELL
            and request.order_type is EquityOrderType.STOP_MARKET
            and request.time_in_force is TimeInForce.GTC
        ):
            raise AttendedControlError("IBKR_ATTENDED_PROTECTION_TUPLE_INVALID")
        if purpose == "exit" and request.side is not BrokerSide.SELL:
            raise AttendedControlError("IBKR_ATTENDED_EXIT_TUPLE_INVALID")
        if purpose not in {"entry", "exit", "protection"}:
            raise AttendedControlError("IBKR_ATTENDED_PURPOSE_INVALID")

    def create_order_review(
        self,
        request: OrderRequest,
        *,
        purpose: str,
        source_review_id: str | None = None,
    ) -> dict[str, object]:
        if purpose == "protection":
            raise AttendedControlError("IBKR_ATTENDED_PROTECTION_SOURCE_REQUIRED")
        self._validate_request(request, purpose)
        review = self.runtime.review_order(request)
        return self._persist_order_review(
            request,
            purpose=purpose,
            source_review_id=source_review_id,
            review=review,
        )

    def _persist_order_review(
        self,
        request: OrderRequest,
        *,
        purpose: str,
        source_review_id: str | None,
        review: AttendedLocalReview,
    ) -> dict[str, object]:
        if review.request.exact_tuple != request.exact_tuple or review.expired_at(self._now()):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_NOT_CURRENT_OR_EXACT")
        record = self.store.create_review(
            {
                "schema_version": REVIEW_SCHEMA,
                "kind": "order",
                "purpose": purpose,
                "source_review_id": source_review_id,
                "session_tag": "regular_hours",
                "review": _order_review_payload(review),
                "approval_material_sha256": hashlib.sha256(
                    _order_approval_material(review)
                ).hexdigest(),
            }
        )
        return self.public_review(record)

    def create_protection_review(self, source_review_id: str) -> dict[str, object]:
        source_request, plan_id, stop_template, claimed_at = self._protection_source(
            source_review_id
        )
        review = self.runtime.review_protection(
            source_request,
            plan_id,
            stop_template,
            claimed_at,
        )
        request = review.request
        self._validate_request(request, "protection")
        return self._persist_order_review(
            request,
            purpose="protection",
            source_review_id=source_review_id,
            review=review,
        )

    def _protection_source(
        self, source_review_id: str
    ) -> tuple[OrderRequest, str, OrderRequest, datetime]:
        source = self.store.load_review(source_review_id)
        if (
            source.get("schema_version") != REVIEW_SCHEMA
            or source.get("kind") != "order"
            or source.get("purpose") != "entry"
            or source.get("source_review_id") is not None
        ):
            raise AttendedControlError("IBKR_ATTENDED_PROTECTION_SOURCE_INVALID")
        stored = source.get("review")
        if not isinstance(stored, Mapping):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED")
        source_request = _request_from_payload(stored.get("request"))
        self._validate_request(source_request, "entry")
        claim = self.store.load_claim(source_review_id)
        phrase = str(stored.get("required_confirmation_phrase", ""))
        if (
            not phrase
            or claim.get("confirmation_sha256")
            != hashlib.sha256(phrase.encode("utf-8")).hexdigest()
        ):
            raise AttendedControlError("IBKR_ATTENDED_CLAIM_TAMPERED")
        preview = stored.get("preview")
        risk = preview.get("risk") if isinstance(preview, Mapping) else None
        required_stop = (
            preview.get("required_stop") if isinstance(preview, Mapping) else None
        )
        plan_id = str(risk.get("plan_id", "")) if isinstance(risk, Mapping) else ""
        if not _SHA256.fullmatch(plan_id):
            raise AttendedControlError("IBKR_ATTENDED_PROTECTION_PLAN_INVALID")
        stop_template = _request_from_stop_preview(
            required_stop, account_masked=source_request.account_masked
        )
        self._validate_request(stop_template, "protection")
        if (
            stop_template.symbol != source_request.symbol
            or stop_template.quantity != source_request.quantity
            or stop_template.client_ref_id == source_request.client_ref_id
        ):
            raise AttendedControlError("IBKR_ATTENDED_PROTECTION_PLAN_INVALID")
        try:
            claimed_at = _utc(
                datetime.fromisoformat(str(claim["claimed_at"])), "claim_time"
            )
        except (KeyError, ValueError) as exc:
            raise AttendedControlError("IBKR_ATTENDED_CLAIM_TAMPERED") from exc
        return source_request, plan_id, stop_template, claimed_at

    def confirm_order(
        self, review_id: str, exact_confirmation: str
    ) -> dict[str, object]:
        record = self.store.load_review(review_id)
        if record.get("kind") != "order":
            raise AttendedControlError("IBKR_ATTENDED_ORDER_REVIEW_REQUIRED")
        stored = record.get("review")
        if not isinstance(stored, Mapping):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED")
        phrase = str(stored.get("required_confirmation_phrase", ""))
        if exact_confirmation != phrase:
            raise AttendedControlError("IBKR_ATTENDED_EXACT_CONFIRMATION_REQUIRED")
        try:
            expires = datetime.fromisoformat(str(stored["expires_at"]))
        except (KeyError, ValueError) as exc:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED") from exc
        now = self._now()
        if now >= _utc(expires, "expiry"):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_EXPIRED")
        if self.store.claim_exists(review_id):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_ALREADY_CLAIMED_NONRETRYABLE")
        request = _request_from_payload(stored.get("request"))
        self._validate_request(request, str(record.get("purpose", "")))
        if record.get("purpose") == "protection":
            source_review_id = str(record.get("source_review_id", ""))
            source_request, plan_id, stop_template, claimed_at = self._protection_source(
                source_review_id
            )
            fresh = self.runtime.review_protection(
                source_request,
                plan_id,
                stop_template,
                claimed_at,
            )
        else:
            fresh = self.runtime.review_order(request)
        if (
            fresh.request.exact_tuple != request.exact_tuple
            or fresh.expired_at(now)
            or hashlib.sha256(_order_approval_material(fresh)).hexdigest()
            != record.get("approval_material_sha256")
            or fresh.required_confirmation_phrase != phrase
        ):
            raise AttendedControlError("IBKR_ATTENDED_FRESH_REVIEW_CHANGED")
        self.runtime.prepare_mutation()
        self.store.claim(review_id, claimed_at=now, phrase=exact_confirmation)
        try:
            result = self.runtime.place_order(request, fresh, exact_confirmation)
        except BrokerUnknownSubmission:
            outcome = self._unknown_outcome(review_id, request, "BROKER_SUBMISSION_UNKNOWN")
            self.store.record_outcome(review_id, outcome)
            return outcome
        except BrokerError as exc:
            state = (
                "UNKNOWN_NONRETRYABLE"
                if exc.submission_may_have_reached_broker
                else "BLOCKED_CONSUMED_REVIEW"
            )
            outcome = self._base_outcome(review_id, request, state, exc.code)
            self.store.record_outcome(review_id, outcome)
            return outcome
        except Exception:
            outcome = self._unknown_outcome(review_id, request, "RUNTIME_EXCEPTION")
            self.store.record_outcome(review_id, outcome)
            return outcome
        outcome = self._operation_outcome(review_id, request, result)
        self.store.record_outcome(review_id, outcome)
        return outcome

    def create_cancel_review(self, broker_order_id: str) -> dict[str, object]:
        review = self.runtime.review_cancel(str(broker_order_id))
        if review.expired_at(self._now()):
            raise AttendedControlError("IBKR_ATTENDED_CANCEL_REVIEW_EXPIRED")
        record = self.store.create_review(
            {
                "schema_version": REVIEW_SCHEMA,
                "kind": "cancel",
                "purpose": "cancel",
                "source_review_id": None,
                "session_tag": "regular_hours",
                "review": _cancel_review_payload(review),
                "approval_material_sha256": hashlib.sha256(
                    _cancel_approval_material(review)
                ).hexdigest(),
            }
        )
        return self.public_review(record)

    def confirm_cancel(
        self, review_id: str, exact_confirmation: str
    ) -> dict[str, object]:
        record = self.store.load_review(review_id)
        if record.get("kind") != "cancel":
            raise AttendedControlError("IBKR_ATTENDED_CANCEL_REVIEW_REQUIRED")
        stored = record.get("review")
        if not isinstance(stored, Mapping):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED")
        phrase = str(stored.get("required_confirmation_phrase", ""))
        if exact_confirmation != phrase:
            raise AttendedControlError("IBKR_ATTENDED_EXACT_CONFIRMATION_REQUIRED")
        now = self._now()
        try:
            expiry = _utc(datetime.fromisoformat(str(stored["expires_at"])), "expiry")
        except (KeyError, ValueError) as exc:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED") from exc
        if now >= expiry:
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_EXPIRED")
        if self.store.claim_exists(review_id):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_ALREADY_CLAIMED_NONRETRYABLE")
        target = str(stored.get("broker_order_id", ""))
        fresh = self.runtime.review_cancel(target)
        if (
            fresh.expired_at(now)
            or hashlib.sha256(_cancel_approval_material(fresh)).hexdigest()
            != record.get("approval_material_sha256")
            or fresh.required_confirmation_phrase != phrase
        ):
            raise AttendedControlError("IBKR_ATTENDED_FRESH_REVIEW_CHANGED")
        self.runtime.prepare_mutation()
        self.store.claim(review_id, claimed_at=now, phrase=exact_confirmation)
        try:
            result = self.runtime.cancel_order(target, fresh, exact_confirmation)
        except BrokerUnknownSubmission:
            outcome = self._cancel_outcome(review_id, target, "UNKNOWN_NONRETRYABLE", "BROKER_SUBMISSION_UNKNOWN")
        except BrokerError as exc:
            state = "UNKNOWN_NONRETRYABLE" if exc.submission_may_have_reached_broker else "BLOCKED_CONSUMED_REVIEW"
            outcome = self._cancel_outcome(review_id, target, state, exc.code)
        except Exception:
            outcome = self._cancel_outcome(review_id, target, "UNKNOWN_NONRETRYABLE", "RUNTIME_EXCEPTION")
        else:
            outcome = self._cancel_outcome(
                review_id,
                target,
                "BROKER_RECONCILIATION_REQUIRED",
                result.status.value,
            )
        self.store.record_outcome(review_id, outcome)
        return outcome

    def public_review(self, record: Mapping[str, object]) -> dict[str, object]:
        review = record.get("review")
        if not isinstance(review, Mapping):
            raise AttendedControlError("IBKR_ATTENDED_REVIEW_TAMPERED")
        request = review.get("request")
        side = str(request.get("side", "")) if isinstance(request, Mapping) else "cancel"
        heading = (
            "BUY REVIEW REQUIRED"
            if side == "buy"
            else "SELL / EXIT REVIEW REQUIRED"
        )
        preview = review.get("preview")
        alerts = preview.get("alerts", []) if isinstance(preview, Mapping) else []
        return {
            "requirement": heading,
            "review_id": record["review_id"],
            "purpose": record["purpose"],
            "session_tag": record["session_tag"],
            "preview": preview,
            "alerts": alerts,
            "order_checks": review.get("order_checks", []),
            "disclosure": review.get("disclosure"),
            "expires_at": review.get("expires_at"),
            "exact_confirmation_phrase": review.get("required_confirmation_phrase"),
            "confirmation_command_requires": ["review_id", "exact_confirmation_phrase"],
        }

    def _base_outcome(
        self, review_id: str, request: OrderRequest, state: str, code: str
    ) -> dict[str, object]:
        protection = (
            "AWAITING_CONFIRMED_FILL"
            if request.side is BrokerSide.BUY
            else (
                "AWAITING_BROKER_WORKING_VERIFICATION"
                if request.order_type is EquityOrderType.STOP_MARKET
                else "NOT_APPLICABLE"
            )
        )
        return {
            "review_id": review_id,
            "dispatch_state": state,
            "broker_result_code": code,
            "observed_at": self._now().isoformat(),
            "broker_reconciliation_required": True,
            "protection_state": protection,
            "protected": False,
            "next_action": (
                "After a broker-confirmed fill, run attended-protection-review."
                if request.side is BrokerSide.BUY
                else "Verify the broker order state before claiming coverage or flatness."
            ),
        }

    def _unknown_outcome(
        self, review_id: str, request: OrderRequest, code: str
    ) -> dict[str, object]:
        return self._base_outcome(review_id, request, "UNKNOWN_NONRETRYABLE", code)

    def _operation_outcome(
        self, review_id: str, request: OrderRequest, result: BrokerOperationResult
    ) -> dict[str, object]:
        if not isinstance(result, BrokerOperationResult):
            return self._unknown_outcome(review_id, request, "INVALID_RUNTIME_RESULT")
        return self._base_outcome(
            review_id, request, "BROKER_RECONCILIATION_REQUIRED", result.status.value
        )

    def _cancel_outcome(
        self, review_id: str, target: str, state: str, code: str
    ) -> dict[str, object]:
        return {
            "review_id": review_id,
            "broker_order_id": target,
            "dispatch_state": state,
            "broker_result_code": code,
            "observed_at": self._now().isoformat(),
            "broker_reconciliation_required": True,
            "cancel_confirmed": False,
            "retry_allowed": False,
        }


__all__ = [
    "AttendedControlError",
    "AttendedIbkrRuntime",
    "AttendedOrderControl",
    "AttendedReviewStore",
    "CLAIM_SCHEMA",
    "OUTCOME_SCHEMA",
    "REVIEW_SCHEMA",
]
