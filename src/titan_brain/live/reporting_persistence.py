"""Reporting-setting persistence across a controlled restart (evaluator).

The handoff notes that the owner approved a Gateway reporting change (Master API
client id 19735) but that "a post-restart persistence check ... has NOT been
recorded." Whether the live Gateway actually retained that setting can only be
observed against the owner's live Gateway; it cannot be established offline.

What CAN be built offline, and is built here, is the pure decision function that
turns two genuine reporting observations — one taken before a controlled
restart and one after — into a fixed-code verdict, and that REFUSES (verdict
UNVERIFIED) whenever the evidence is missing, mismatched in scope, or not
actually separated by a restart. It authenticates nothing, reads no live
Gateway, and never asserts persistence without both observations. The live
readings are the owner's controlled-restart acceptance step; this module only
decides what they mean.

A verdict of PERSISTED is a necessary, not sufficient, readiness input: it says
the observed reporting client id/scope was the same reading before and after a
restart that genuinely happened, for the same account. It is not proof of
complete manual/TWS/FIX visibility (that is the separate supported-scope
determination the guide calls out).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re


_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)


class ReportingPersistenceError(ValueError):
    """Fixed-code contract error; never carries private text."""


def _fail(reason: str) -> None:
    raise ReportingPersistenceError("REPORTING_PERSISTENCE_" + reason)


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")
    return value.astimezone(timezone.utc)


class ReportingVerdict(str, Enum):
    PERSISTED = "PERSISTED"
    CHANGED = "CHANGED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class ReportingObservation:
    """One genuine observation of the Gateway reporting configuration.

    ``account_binding`` scopes the observation to an exact account; a probe that
    could not read the setting sets ``master_client_id=None`` (an absent
    reading, never a fabricated value).
    """

    account_binding: str
    master_client_id: int | None
    observed_at: datetime
    # True only if this reading was taken from a freshly (re)started Gateway
    # process, established by the caller's controlled restart, not assumed.
    post_restart: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.account_binding, str) or _TOKEN.fullmatch(self.account_binding) is None:
            _fail("ACCOUNT_BINDING_INVALID")
        if self.master_client_id is not None and (
            isinstance(self.master_client_id, bool)
            or type(self.master_client_id) is not int
            or not 0 <= self.master_client_id <= 2_147_483_647
        ):
            _fail("CLIENT_ID_INVALID")
        _aware(self.observed_at)
        if type(self.post_restart) is not bool:
            _fail("POST_RESTART_FLAG_INVALID")


@dataclass(frozen=True)
class ReportingPersistenceResult:
    verdict: ReportingVerdict
    reasons: tuple[str, ...]

    @property
    def persisted(self) -> bool:
        return self.verdict is ReportingVerdict.PERSISTED


def evaluate_reporting_persistence(
    *,
    expected_master_client_id: int,
    before: ReportingObservation,
    after: ReportingObservation,
) -> ReportingPersistenceResult:
    """Decide whether the reporting setting persisted across a controlled restart.

    PERSISTED  : both observations read the SAME expected client id, for the
                 same account, the 'after' reading was post-restart, and it did
                 not precede the 'before' reading.
    CHANGED    : both readings are present and scoped correctly but the client
                 id differs between them or from the expected value.
    UNVERIFIED : any evidence is missing — an absent reading, an account-scope
                 mismatch, or an 'after' reading not established as post-restart
                 (or ordered before 'before'). Fail closed: never assert
                 persistence without genuine before/after evidence.
    """
    if isinstance(expected_master_client_id, bool) or type(expected_master_client_id) is not int or not 0 <= expected_master_client_id <= 2_147_483_647:
        _fail("EXPECTED_CLIENT_ID_INVALID")
    if type(before) is not ReportingObservation or type(after) is not ReportingObservation:
        _fail("OBSERVATION_INVALID")

    reasons: set[str] = set()
    if before.account_binding != after.account_binding:
        reasons.add("ACCOUNT_SCOPE_MISMATCH")
    if not after.post_restart:
        reasons.add("AFTER_NOT_POST_RESTART")
    if _aware(after.observed_at) < _aware(before.observed_at):
        reasons.add("OBSERVATION_ORDER_INVALID")
    if before.master_client_id is None:
        reasons.add("BEFORE_READING_ABSENT")
    if after.master_client_id is None:
        reasons.add("AFTER_READING_ABSENT")

    if reasons:
        return ReportingPersistenceResult(ReportingVerdict.UNVERIFIED, tuple(sorted(reasons)))

    if before.master_client_id == after.master_client_id == expected_master_client_id:
        return ReportingPersistenceResult(ReportingVerdict.PERSISTED, ())

    changed: set[str] = set()
    if before.master_client_id != after.master_client_id:
        changed.add("CLIENT_ID_CHANGED_ACROSS_RESTART")
    if after.master_client_id != expected_master_client_id:
        changed.add("CLIENT_ID_NOT_EXPECTED")
    return ReportingPersistenceResult(ReportingVerdict.CHANGED, tuple(sorted(changed)))
