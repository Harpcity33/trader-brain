"""Read-only, complete MCP tool discovery; never an execution authority.

This module only sends tools/list through an already authorized RPC client.
It neither logs in, loads credentials, invokes a discovered tool, nor alters
broker/client confirmation requirements. Wire the callback to the supported
client's existing session; do not extract credentials from another client.

Full discovery prevents a missing later page from being mistaken for a missing
broker feature. Tool presence is NOT proof of unattended execution permission,
order-history completeness, or a working production transport.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from typing import Any

Rpc = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class InventoryError(RuntimeError):
    """Discovery failed; no partial inventory is returned as complete."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InventoryError("NON_JSON_TOOL_DEFINITION") from exc


@dataclass(frozen=True)
class ToolInventory:
    # JSON strings avoid mutating a definition after its hash was calculated.
    definitions: tuple[str, ...]
    page_count: int

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(item) for item in self.definitions)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item["name"] for item in self.tools)

    @property
    def inventory_sha256(self) -> str:
        return hashlib.sha256(_canonical(list(self.tools)).encode()).hexdigest()

    def report(self, visible_names: Iterable[str] = ()) -> dict[str, Any]:
        visible = set(visible_names)
        if any(not isinstance(name, str) or not name for name in visible):
            raise ValueError("visible names must be nonempty strings")
        advertised = set(self.names)
        return {
            "schema_version": "titan_mcp_inventory_v1",
            "inventory_complete": True,
            "page_count": self.page_count,
            "tool_count": len(self.definitions),
            "inventory_sha256": self.inventory_sha256,
            "tool_names": list(self.names),
            "advertised_not_visible": sorted(advertised - visible),
            "visible_not_advertised": sorted(visible - advertised),
            "unattended_permission": "NOT_ASSESSED",
            "order_coverage": "NOT_ASSESSED",
            "activation_authority": False,
        }


async def discover_tools(
    rpc: Rpc,
    *,
    max_pages: int = 128,
    max_tools: int = 4096,
    page_timeout_seconds: float = 15.0,
) -> ToolInventory:
    """Follow all opaque cursors, including the valid empty-string cursor.

    ``rpc`` accepts an MCP method and params, and returns either the decoded
    JSON-RPC response or the unwrapped result mapping. SDK users can adapt
    their ListToolsResult with model_dump(mode="json", by_alias=True).
    Names must be compared with the client's exact namespace mapping, not
    fuzzy matching. Preserve all descriptions, annotations, and schemas.

    An error, malformed page, duplicate tool, repeated cursor, or limit breach
    fails the whole discovery. No credentials/cursor values enter the report.
    """
    for name, value in (("max_pages", max_pages), ("max_tools", max_tools)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (isinstance(page_timeout_seconds, bool)
            or not isinstance(page_timeout_seconds, (int, float))
            or not isfinite(page_timeout_seconds) or page_timeout_seconds <= 0):
        raise ValueError("page_timeout_seconds must be finite and positive")

    cursor: str | None = None
    seen_cursors: set[str] = set()
    definitions: dict[str, str] = {}
    for page_number in range(1, max_pages + 1):
        params = {} if cursor is None else {"cursor": cursor}
        try:
            response = await asyncio.wait_for(
                rpc("tools/list", params), timeout=page_timeout_seconds)
        except Exception as exc:
            # Do not propagate provider text that might contain credentials.
            raise InventoryError("TOOL_LIST_REQUEST_FAILED") from None
        if not isinstance(response, Mapping):
            raise InventoryError("NON_OBJECT_TOOL_LIST_RESPONSE")
        if "error" in response:
            raise InventoryError("TOOL_LIST_RPC_ERROR")
        page = response.get("result", response)
        if not isinstance(page, Mapping) or not isinstance(page.get("tools"), list):
            raise InventoryError("MALFORMED_TOOL_LIST_PAGE")
        for tool in page["tools"]:
            if not isinstance(tool, Mapping):
                raise InventoryError("MALFORMED_TOOL_DEFINITION")
            name = tool.get("name")
            if not isinstance(name, str) or not name or name != name.strip():
                raise InventoryError("INVALID_TOOL_NAME")
            if not isinstance(tool.get("inputSchema"), Mapping):
                raise InventoryError("MISSING_TOOL_INPUT_SCHEMA")
            if name in definitions:
                raise InventoryError("DUPLICATE_TOOL_NAME")
            definitions[name] = _canonical(dict(tool))
            if len(definitions) > max_tools:
                raise InventoryError("TOOL_COUNT_LIMIT_EXCEEDED")
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            return ToolInventory(
                tuple(definitions[name] for name in sorted(definitions)), page_number)
        if not isinstance(next_cursor, str):
            raise InventoryError("INVALID_NEXT_CURSOR")
        if next_cursor in seen_cursors:
            raise InventoryError("REPEATED_NEXT_CURSOR")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise InventoryError("PAGE_LIMIT_EXCEEDED")
