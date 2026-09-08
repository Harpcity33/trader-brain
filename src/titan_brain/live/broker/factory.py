"""Single named factory for attended and separately supported broker paths."""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from .base import BrokerClient
from .production import ProductionTransport, SupportedProductionBrokerAdapter
from .robinhood import RobinhoodBrokerAdapter


class BrokerAdapterKind(str, Enum):
    ROBINHOOD_ATTENDED_PLACEHOLDER = "robinhood_codex_connector"
    SUPPORTED_PRODUCTION_TRANSPORT = "supported_production_transport"


class BrokerFactoryError(RuntimeError):
    pass


def build_broker_client(
    execution_config: Mapping[str, object],
    *,
    account_masked: str,
    production_transport: ProductionTransport | None = None,
) -> BrokerClient:
    """Build exactly the configured path without discovering credentials.

    The current Robinhood connector remains an attended, mutation-blocking
    placeholder.  Selecting the production kind requires an explicitly
    injected provider-supported transport; absence is a hard readiness error.
    """

    if not isinstance(execution_config, Mapping):
        raise BrokerFactoryError("execution broker configuration is missing")
    try:
        kind = BrokerAdapterKind(str(execution_config.get("broker_adapter", "")))
    except ValueError as exc:
        raise BrokerFactoryError("unknown broker adapter kind") from exc
    if kind is BrokerAdapterKind.ROBINHOOD_ATTENDED_PLACEHOLDER:
        if production_transport is not None:
            raise BrokerFactoryError("production transport supplied while attended path is selected")
        adapter = RobinhoodBrokerAdapter()
    else:
        if production_transport is None:
            raise BrokerFactoryError(
                "supported production broker selected without an injected authorized transport"
            )
        expected_transport_id = str(
            execution_config.get("production_transport_id", "")
        ).strip()
        if not expected_transport_id:
            raise BrokerFactoryError(
                "supported production broker has no signed transport identity"
            )
        if production_transport.descriptor.transport_id != expected_transport_id:
            raise BrokerFactoryError(
                "injected production transport differs from signed configuration"
            )
        expected_account_binding = str(
            execution_config.get("production_account_binding_fingerprint", "")
        ).strip()
        expected_authorization = str(
            execution_config.get("production_authorization_binding_id", "")
        ).strip()
        if (
            not expected_account_binding
            or production_transport.descriptor.account_binding_fingerprint
            != expected_account_binding
        ):
            raise BrokerFactoryError(
                "production broker exact-account binding differs from signed configuration"
            )
        if (
            not expected_authorization
            or production_transport.descriptor.authorization_binding_id
            != expected_authorization
        ):
            raise BrokerFactoryError(
                "production broker authorization differs from signed configuration"
            )
        adapter = SupportedProductionBrokerAdapter(production_transport)
    if adapter.capabilities.account_masked != str(account_masked):
        raise BrokerFactoryError("configured broker account does not match policy")
    return adapter


__all__ = ["BrokerAdapterKind", "BrokerFactoryError", "build_broker_client"]
