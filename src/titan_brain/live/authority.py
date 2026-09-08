"""Non-optional capability boundary for every broker order mutation.

The execution coordinators deliberately know nothing about process topology or
runtime activation.  They instead require this capability and revalidate it at
the last safe points before broker review and broker mutation.  Production
implements the protocol with the activated account writer; tests must provide
an explicit test capability rather than gaining authority by omission.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from .broker import AccountSnapshot, OrderRequest, OrderSnapshot


class MutationOperation(str, Enum):
    ENTRY_PLACE = "ENTRY_PLACE"
    SAFETY_PLACE = "SAFETY_PLACE"
    CANCEL = "CANCEL"


class MutationPhase(str, Enum):
    BEFORE_PREPARE = "BEFORE_PREPARE"
    BEFORE_REVIEW = "BEFORE_REVIEW"
    BEFORE_PLACE = "BEFORE_PLACE"
    BEFORE_CANCEL = "BEFORE_CANCEL"


class MutationAuthorityDenied(RuntimeError):
    """The live activation/lock/evidence capability was not current."""

    def __init__(self, *failure_codes: str) -> None:
        codes = tuple(dict.fromkeys(str(code) for code in failure_codes if str(code)))
        self.failure_codes = codes or ("MUTATION_AUTHORITY_DENIED",)
        super().__init__(",".join(self.failure_codes))


class MutationAuthority(Protocol):
    """Dynamically prove authority for one exact snapshot and operation."""

    def require_mutation_authority(
        self,
        *,
        snapshot: AccountSnapshot | None,
        operation: MutationOperation,
        phase: MutationPhase,
        now: object,
        plan_id: str,
        kind: object,
        request: OrderRequest | None = None,
        target: OrderSnapshot | None = None,
        plan: object | None = None,
        risk_decision: object | None = None,
    ) -> None: ...


__all__ = [
    "MutationAuthority",
    "MutationAuthorityDenied",
    "MutationOperation",
    "MutationPhase",
]
