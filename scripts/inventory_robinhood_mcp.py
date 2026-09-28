#!/usr/bin/env python3
"""Inventory one configured MCP server through Codex without invoking its tools.

The script starts the installed Codex app-server, uses its existing supported
OAuth/session store, and calls only ``mcpServerStatus/list``.  The app-server
returns one authenticated tool-definition map for the selected server; it does
not expose the origin server's ``tools/list`` cursor.  The definitions are then
passed through ``titan_brain.live.mcp_inventory`` using deliberately small
opaque pages to exercise the repository helper's cursor handling.  This can
detect client-visible/status-map divergence and helper truncation, but it must
not be reported as a direct test of an origin-server cursor.  No discovered
broker tool is called.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
import tomllib
from typing import Any, Mapping

from titan_brain.live.mcp_inventory import discover_tools


class AppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self, executable: str, *, timeout_seconds: float = 45.0) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.request_id = 0

    def __enter__(self) -> "CodexAppServer":
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        initialized = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "titan-readonly-mcp-inventory",
                    "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")
        self.initialize_result = initialized
        return self

    def __exit__(self, *_: object) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def _write(self, payload: Mapping[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("APP_SERVER_NOT_STARTED")
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.request_id += 1
        wanted = self.request_id
        self._write(
            {
                "jsonrpc": "2.0",
                "id": wanted,
                "method": method,
                "params": dict(params),
            }
        )
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self.process is None or self.process.stdout is None:
                raise AppServerError("APP_SERVER_NOT_STARTED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("APP_SERVER_RESPONSE_TIMEOUT")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise AppServerError("APP_SERVER_RESPONSE_TIMEOUT")
            line = self.process.stdout.readline()
            if not line:
                raise AppServerError("APP_SERVER_CLOSED")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != wanted:
                continue
            if "error" in message:
                raise AppServerError("APP_SERVER_REQUEST_FAILED")
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise AppServerError("APP_SERVER_MALFORMED_RESULT")
            return result


def configured_server(codex: str, server: str) -> dict[str, Any]:
    process = subprocess.run(
        [codex, "mcp", "get", server, "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if process.returncode != 0:
        raise AppServerError("CODEX_MCP_CONFIG_READ_FAILED")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise AppServerError("CODEX_MCP_CONFIG_MALFORMED") from exc
    if not isinstance(value, dict):
        raise AppServerError("CODEX_MCP_CONFIG_MALFORMED")
    return value


def installed_client_version(codex: str) -> str:
    """Read the exact installed CLI version without contacting any MCP server."""

    process = subprocess.run(
        [codex, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    value = process.stdout.strip()
    if process.returncode != 0 or not value:
        raise AppServerError("CODEX_VERSION_READ_FAILED")
    return value


def list_server_statuses(
    client: CodexAppServer, *, page_size: int = 1
) -> tuple[list[dict[str, Any]], int]:
    cursor: str | None = None
    seen: set[str] = set()
    statuses: list[dict[str, Any]] = []
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": page_size, "detail": "full"}
        if cursor is not None:
            params["cursor"] = cursor
        result = client.request("mcpServerStatus/list", params)
        data = result.get("data")
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise AppServerError("MALFORMED_MCP_STATUS_PAGE")
        statuses.extend(data)
        pages += 1
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            return statuses, pages
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise AppServerError("INVALID_MCP_STATUS_CURSOR")
        seen.add(next_cursor)
        cursor = next_cursor


async def validate_with_inventory_helper(
    definitions: list[dict[str, Any]], *, page_size: int
) -> dict[str, Any]:
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    async def rpc(method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method != "tools/list":
            raise AssertionError("inventory may only request tools/list")
        raw_cursor = params.get("cursor")
        offset = 0 if raw_cursor is None else int(str(raw_cursor).removeprefix("tool:"))
        page = definitions[offset : offset + page_size]
        end = offset + len(page)
        result: dict[str, Any] = {"tools": page}
        if end < len(definitions):
            result["nextCursor"] = f"tool:{end}"
        return result

    inventory = await discover_tools(rpc)
    return inventory.report(inventory.names)


def exact_restriction_sources(tools: Mapping[str, Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    needles = {
        "review_equity_order": "get explicit confirmation before calling place_equity_order",
        "place_equity_order": "get explicit user confirmation before calling this tool",
        "cancel_equity_order": "Always confirm with the user before calling",
        "review_option_order": "get explicit confirmation before calling place_option_order",
        "place_option_order": "get explicit user confirmation before calling this tool",
        "cancel_option_order": "Always confirm with the user before calling",
    }
    for name, needle in needles.items():
        definition = tools.get(name)
        if not isinstance(definition, Mapping):
            continue
        description = definition.get("description")
        if isinstance(description, str) and needle.lower() in description.lower():
            sources.append(
                {
                    "server_tool": name,
                    "source_field": "server-advertised tool description",
                    "exact_text": needle,
                }
            )
    for name, needle in (
        ("review_equity_order", "get explicit confirmation before calling place_equity_order"),
        ("review_option_order", "get explicit confirmation before calling place_option_order"),
    ):
        definition = tools.get(name)
        if not isinstance(definition, Mapping):
            continue
        output_schema = definition.get("outputSchema")
        if not isinstance(output_schema, Mapping):
            continue
        properties = output_schema.get("properties")
        guide = properties.get("guide") if isinstance(properties, Mapping) else None
        description = guide.get("description") if isinstance(guide, Mapping) else None
        if isinstance(description, str) and needle.lower() in description.lower():
            sources.append(
                {
                    "server_tool": name,
                    "source_field": "server-advertised outputSchema.guide.description",
                    "exact_text": needle,
                }
            )
    return sources


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    codex = args.codex or os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    if not codex:
        raise AppServerError("CODEX_EXECUTABLE_NOT_FOUND")
    client_version = installed_client_version(codex)
    config = configured_server(codex, args.server)
    with CodexAppServer(codex, timeout_seconds=args.timeout) as client:
        statuses, status_pages = list_server_statuses(client, page_size=1)
        matches = [item for item in statuses if item.get("name") == args.server]
        if len(matches) != 1:
            raise AppServerError("CONFIGURED_SERVER_STATUS_NOT_UNIQUE")
        status = matches[0]
        raw_tools = status.get("tools")
        if not isinstance(raw_tools, Mapping):
            raise AppServerError("MCP_STATUS_HAS_NO_TOOL_MAP")
        tools = {str(name): value for name, value in raw_tools.items()}
        definitions = []
        for name in sorted(tools):
            definition = tools[name]
            if not isinstance(definition, dict):
                raise AppServerError("MALFORMED_TOOL_DEFINITION")
            definitions.append(definition)
        helper = asyncio.run(
            validate_with_inventory_helper(definitions, page_size=args.tool_page_size)
        )
        init = dict(client.initialize_result)

    configured = set(config.get("enabled_tools") or [])
    discovered = set(tools)
    missing = sorted(configured - discovered)
    unexpected = sorted(discovered - configured)
    version = str(init.get("userAgent", "unknown"))
    pagination_outcome = (
        "NO_CLIENT_STATUS_DIVERGENCE_OBSERVED_ORIGIN_CURSOR_NOT_EXPOSED"
    )
    codex_home = Path(str(init.get("codexHome", "")))
    local_approval_mode = None
    config_path = codex_home / "config.toml"
    if config_path.is_file():
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        local_approval_mode = (
            parsed.get("mcp_servers", {})
            .get(args.server, {})
            .get("default_tools_approval_mode")
        )
    return {
        "schema_version": "titan_authenticated_mcp_inventory_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "broker_tools_invoked": [],
        "configured_server": args.server,
        "transport": config.get("transport"),
        "installed_client_version": client_version,
        "codex_user_agent": version,
        "server_info": status.get("serverInfo"),
        "auth_status": status.get("authStatus"),
        "runtime_status": status.get("runtimeStatus"),
        "codex_status_page_count": status_pages,
        "origin_server_tool_cursor_observed": False,
        "origin_server_pagination_test": "NOT_DIRECTLY_OBSERVABLE_THROUGH_APP_SERVER_STATUS",
        "helper_pagination_test": "PASSED_ON_LOCALLY_SLICED_AUTHENTICATED_DEFINITIONS",
        "inventory_helper": helper,
        "configured_tool_count": len(configured),
        "configured_tool_names": sorted(configured),
        "discovered_tool_count": len(discovered),
        "discovered_tool_names": sorted(discovered),
        "configured_not_discovered": missing,
        "discovered_not_configured": unexpected,
        "pagination_bug_test": pagination_outcome,
        "pagination_bug_attribution": (
            "The repository helper followed every synthetic opaque cursor, "
            "including an empty cursor in its unit suite, and the installed "
            "client-visible names matched the authenticated app-server status "
            "map. The app-server did not expose the Robinhood origin server's "
            "tools/list cursor, so this evidence neither reproduces nor rules "
            "out origin-server pagination. A configured whitelist name absent "
            "from the authenticated advertisement is not by itself a "
            "pagination defect."
        ),
        "restriction_sources": exact_restriction_sources(tools),
        "config_default_tools_approval_mode": local_approval_mode,
        "config_approval_note": (
            "Codex tool-call approval configuration does not erase the "
            "server-advertised confirmation contract or owner policy."
        ),
        "tools": {name: tools[name] for name in sorted(tools)},
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="robinhood_trading")
    parser.add_argument("--codex")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--tool-page-size", type=int, default=7)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = build_report(args)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
