"""Tests for metamcp.transforms — tool filtering and override middleware.

Uses FastMCP's in-memory Client(server) pattern: no subprocess, no network.
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP, Client

from metamcp.transforms import ToolFilterMiddleware, ToolOverrideMiddleware


# ---------------------------------------------------------------------------
# Helpers — build a small FastMCP server with known tools
# ---------------------------------------------------------------------------

def _make_server(name: str = "test") -> FastMCP:
    server = FastMCP(name)

    @server.tool
    def search(query: str) -> str:
        """Search for documents."""
        return f"results for {query}"

    @server.tool
    def read(path: str) -> str:
        """Read a document."""
        return f"content of {path}"

    @server.tool
    def secret_tool() -> str:
        """Internal debug tool."""
        return "secret"

    @server.tool
    def debug_internal() -> str:
        """Another internal tool."""
        return "debug"

    return server


# ---------------------------------------------------------------------------
# ToolFilterMiddleware — hidden (blacklist)
# ---------------------------------------------------------------------------

class TestToolFilterHidden:
    @pytest.mark.asyncio
    async def test_hidden_tools_removed_from_list(self):
        server = _make_server()
        server.add_middleware(
            ToolFilterMiddleware(hidden=["secret_tool", "debug_internal"])
        )

        async with Client(server) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "search" in names
            assert "read" in names
            assert "secret_tool" not in names
            assert "debug_internal" not in names

    @pytest.mark.asyncio
    async def test_hidden_tool_call_blocked(self):
        from fastmcp.exceptions import ToolError

        server = _make_server()
        server.add_middleware(
            ToolFilterMiddleware(hidden=["secret_tool"])
        )

        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool("secret_tool", {})

    @pytest.mark.asyncio
    async def test_allowed_tool_still_works(self):
        server = _make_server()
        server.add_middleware(
            ToolFilterMiddleware(hidden=["secret_tool"])
        )

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "hello"})
            assert not result.is_error


# ---------------------------------------------------------------------------
# ToolFilterMiddleware — exposed (whitelist)
# ---------------------------------------------------------------------------

class TestToolFilterExposed:
    @pytest.mark.asyncio
    async def test_only_exposed_tools_listed(self):
        server = _make_server()
        server.add_middleware(
            ToolFilterMiddleware(exposed=["search", "read"])
        )

        async with Client(server) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert names == {"search", "read"}

    @pytest.mark.asyncio
    async def test_non_exposed_tool_call_blocked(self):
        from fastmcp.exceptions import ToolError

        server = _make_server()
        server.add_middleware(
            ToolFilterMiddleware(exposed=["search"])
        )

        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool("secret_tool", {})


# ---------------------------------------------------------------------------
# ToolFilterMiddleware — no filter (passthrough)
# ---------------------------------------------------------------------------

class TestToolFilterPassthrough:
    @pytest.mark.asyncio
    async def test_no_filter_lists_all(self):
        server = _make_server()
        server.add_middleware(ToolFilterMiddleware())

        async with Client(server) as client:
            tools = await client.list_tools()
            assert len(tools) == 4


# ---------------------------------------------------------------------------
# ToolOverrideMiddleware
# ---------------------------------------------------------------------------

class TestToolOverride:
    @pytest.mark.asyncio
    async def test_tool_renamed_in_list(self):
        server = _make_server()
        server.add_middleware(
            ToolOverrideMiddleware({
                "read": {"name": "google_read", "description": "Read a Google Doc"},
            })
        )

        async with Client(server) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "google_read" in names
            assert "read" not in names

            # Check description override
            google_read = next(t for t in tools if t.name == "google_read")
            assert google_read.description == "Read a Google Doc"

    @pytest.mark.asyncio
    async def test_description_only_override(self):
        server = _make_server()
        server.add_middleware(
            ToolOverrideMiddleware({
                "search": {"name": None, "description": "Search Google Drive"},
            })
        )

        async with Client(server) as client:
            tools = await client.list_tools()
            search = next(t for t in tools if t.name == "search")
            assert search.description == "Search Google Drive"

    @pytest.mark.asyncio
    async def test_reverse_map_on_call(self):
        """Calling by the overridden name should reach the original tool."""
        server = _make_server()
        server.add_middleware(
            ToolOverrideMiddleware({
                "read": {"name": "google_read", "description": None},
            })
        )

        async with Client(server) as client:
            result = await client.call_tool("google_read", {"path": "/doc.txt"})
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_non_overridden_tools_untouched(self):
        server = _make_server()
        server.add_middleware(
            ToolOverrideMiddleware({
                "read": {"name": "google_read", "description": None},
            })
        )

        async with Client(server) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "search" in names
            assert "secret_tool" in names


# ---------------------------------------------------------------------------
# Combined: ToolOverride (outer) + ToolFilter (inner)
# ---------------------------------------------------------------------------

class TestCombinedMiddleware:
    @pytest.mark.asyncio
    async def test_filter_then_override(self):
        """Override registered first (outer), filter second (inner).

        Flow on list_tools:
          1. Override.on_list_tools calls call_next
          2. Filter.on_list_tools calls call_next → backend returns all tools
          3. Filter removes hidden tools (by ORIGINAL name)
          4. Override renames surviving tools
        """
        server = _make_server()
        # Outer
        server.add_middleware(
            ToolOverrideMiddleware({
                "read": {"name": "google_read", "description": "Read GDoc"},
            })
        )
        # Inner
        server.add_middleware(
            ToolFilterMiddleware(hidden=["secret_tool", "debug_internal"])
        )

        async with Client(server) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}

            # Hidden tools removed
            assert "secret_tool" not in names
            assert "debug_internal" not in names

            # Override applied to surviving tool
            assert "google_read" in names
            assert "read" not in names

            # search passes through untouched
            assert "search" in names

    @pytest.mark.asyncio
    async def test_call_renamed_tool_through_filter(self):
        """Calling google_read should reverse-map → read (original), pass filter, work."""
        server = _make_server()
        server.add_middleware(
            ToolOverrideMiddleware({
                "read": {"name": "google_read", "description": None},
            })
        )
        server.add_middleware(
            ToolFilterMiddleware(exposed=["search", "read"])
        )

        async with Client(server) as client:
            result = await client.call_tool("google_read", {"path": "/x"})
            assert not result.is_error
