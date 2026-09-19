from __future__ import annotations

import asyncio
import unittest
import traceback
from titan_brain.live.mcp_inventory import InventoryError, discover_tools


def tool(name, **extra):
    return {"name": name, "inputSchema": {"type": "object"}, **extra}


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, pages):
        calls = []
        async def rpc(method, params):
            calls.append((method, dict(params)))
            result = pages[len(calls) - 1]
            if isinstance(result, Exception):
                raise result
            return result
        return rpc, calls

    async def test_single_page(self):
        rpc, calls = self.client([{"tools": [tool("quotes")]}])
        result = await discover_tools(rpc)
        self.assertEqual(result.names, ("quotes",))
        self.assertEqual(calls, [("tools/list", {})])

    async def test_multiple_pages_include_later_tools(self):
        rpc, calls = self.client([
            {"tools": [tool("quotes")], "nextCursor": "opaque:2"},
            {"tools": [tool("orders")]}])
        result = await discover_tools(rpc)
        self.assertEqual(result.names, ("orders", "quotes"))
        self.assertEqual(result.page_count, 2)
        self.assertEqual(calls[1], ("tools/list", {"cursor": "opaque:2"}))

    async def test_empty_string_is_valid_cursor(self):
        rpc, calls = self.client([
            {"tools": [], "nextCursor": ""}, {"tools": [tool("later")]}])
        result = await discover_tools(rpc)
        self.assertEqual(calls[1][1], {"cursor": ""})
        self.assertEqual(result.names, ("later",))

    async def test_null_cursor_ends_discovery(self):
        rpc, calls = self.client([{"tools": [], "nextCursor": None}])
        result = await discover_tools(rpc)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(len(calls), 1)

    async def test_rpc_envelope(self):
        rpc, _ = self.client([{"jsonrpc": "2.0", "id": 1,
                               "result": {"tools": [tool("x")]}}])
        self.assertEqual((await discover_tools(rpc)).names, ("x",))

    async def test_preserves_confirmation_descriptions_and_annotations(self):
        definition = tool("place", description="Exact user confirmation required.",
                          annotations={"readOnlyHint": False, "destructiveHint": True})
        rpc, _ = self.client([{"tools": [definition]}])
        result = await discover_tools(rpc)
        self.assertEqual(result.tools[0], definition)
        self.assertFalse(result.report()["activation_authority"])
        self.assertEqual(result.report()["unattended_permission"], "NOT_ASSESSED")

    async def test_report_compares_names_only(self):
        rpc, _ = self.client([{"tools": [tool("quotes"), tool("orders")]}])
        report = (await discover_tools(rpc)).report(["quotes"])
        self.assertEqual(report["advertised_not_visible"], ["orders"])
        self.assertEqual(report["order_coverage"], "NOT_ASSESSED")

    async def test_hash_stable_across_page_order(self):
        rpc1, _ = self.client([{"tools": [tool("a"), tool("b")]}])
        rpc2, _ = self.client([{"tools": [tool("b")], "nextCursor": "p"},
                                {"tools": [tool("a")]}])
        self.assertEqual((await discover_tools(rpc1)).inventory_sha256,
                         (await discover_tools(rpc2)).inventory_sha256)

    async def test_returned_tool_copy_cannot_change_hash(self):
        rpc, _ = self.client([{"tools": [tool("x")]}])
        result = await discover_tools(rpc)
        before = result.inventory_sha256
        result.tools[0]["name"] = "changed"
        self.assertEqual(before, result.inventory_sha256)

    async def test_repeated_cursor_fails(self):
        rpc, _ = self.client([{"tools": [], "nextCursor": "x"},
                               {"tools": [], "nextCursor": "x"}])
        with self.assertRaisesRegex(InventoryError, "REPEATED_NEXT_CURSOR"):
            await discover_tools(rpc)

    async def test_duplicate_tool_fails(self):
        rpc, _ = self.client([{"tools": [tool("x"), tool("x")]}])
        with self.assertRaisesRegex(InventoryError, "DUPLICATE_TOOL_NAME"):
            await discover_tools(rpc)

    async def test_invalid_cursor_fails(self):
        rpc, _ = self.client([{"tools": [], "nextCursor": False}])
        with self.assertRaisesRegex(InventoryError, "INVALID_NEXT_CURSOR"):
            await discover_tools(rpc)

    async def test_malformed_page_and_tool_fail(self):
        for response in ([], {}, {"tools": {}}, {"tools": [None]},
                         {"tools": [{"name": "x"}]},
                         {"tools": [tool("  ")]},
                         {"tools": [tool("x", value=float("nan"))]}):
            with self.subTest(response=response):
                rpc, _ = self.client([response])
                with self.assertRaises(InventoryError):
                    await discover_tools(rpc)

    async def test_later_page_error_returns_no_partial_success(self):
        for failure in (RuntimeError("private provider response"),
                        {"error": {"code": -1, "message": "private"}}):
            with self.subTest(failure=failure):
                rpc, _ = self.client([{"tools": [tool("a")], "nextCursor": "p"}, failure])
                with self.assertRaises(InventoryError) as raised:
                    await discover_tools(rpc)
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("private", "".join(traceback.format_exception(raised.exception)))

    async def test_limits_fail_closed(self):
        rpc, _ = self.client([{"tools": [], "nextCursor": "p"}])
        with self.assertRaisesRegex(InventoryError, "PAGE_LIMIT_EXCEEDED"):
            await discover_tools(rpc, max_pages=1)
        rpc, _ = self.client([{"tools": [tool("a"), tool("b")]}])
        with self.assertRaisesRegex(InventoryError, "TOOL_COUNT_LIMIT_EXCEEDED"):
            await discover_tools(rpc, max_tools=1)

    async def test_invalid_limits_rejected(self):
        for kwargs in ({"max_pages": 0}, {"max_pages": True}, {"max_tools": -1},
                       {"page_timeout_seconds": float("inf")},
                       {"page_timeout_seconds": 0}):
            rpc, calls = self.client([])
            with self.assertRaises(ValueError):
                await discover_tools(rpc, **kwargs)
            self.assertEqual(calls, [])

    async def test_timeout_fails_without_tool_call(self):
        calls = []
        async def rpc(method, params):
            calls.append(method)
            await asyncio.sleep(1)
            return {"tools": []}
        with self.assertRaisesRegex(InventoryError, "TOOL_LIST_REQUEST_FAILED"):
            await discover_tools(rpc, page_timeout_seconds=0.01)
        self.assertEqual(calls, ["tools/list"])


if __name__ == "__main__":
    unittest.main()
