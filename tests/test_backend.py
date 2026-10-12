"""Tests for metamcp.backend — BackendProxy with in-memory FastMCP server.

Uses create_proxy() against an in-memory FastMCP instance (no subprocess).
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP, Client

from metamcp.config import ServerConfig, ToolOverride
from metamcp.transforms import ToolFilterMiddleware, ToolOverrideMiddleware


# ---------------------------------------------------------------------------
# Helper — simulate what BackendProxy._create_proxy does, but in-memory
# ---------------------------------------------------------------------------

def _make_echo_server() -> FastMCP:
    """A simple MCP server with tools/resources for proxy testing."""
    server = FastMCP("echo-backend")

    @server.tool
    def echo(message: str) -> str:
        """Echo a message back."""
        return f"echo: {message}"

    @server.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool
    def hidden_admin() -> str:
        """Admin-only tool."""
        return "admin"

    return server


def _apply_middleware(
    proxy: FastMCP,
    config: ServerConfig,
) -> FastMCP:
    """Apply the same middleware logic as BackendProxy._create_proxy."""
    if config.tool_overrides:
        overrides = {
            name: {"name": ov.name, "description": ov.description}
            for name, ov in config.tool_overrides.items()
        }
        proxy.add_middleware(ToolOverrideMiddleware(overrides))

    if config.exposed_tools or config.hidden_tools:
        proxy.add_middleware(
            ToolFilterMiddleware(
                exposed=config.exposed_tools,
                hidden=config.hidden_tools,
            )
        )
    return proxy


# ---------------------------------------------------------------------------
# create_proxy with in-memory server
# ---------------------------------------------------------------------------

class TestProxyInMemory:
    @pytest.mark.asyncio
    async def test_proxy_lists_tools(self):
        """create_proxy(server) mirrors all tools."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")

        async with Client(proxy) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "echo" in names
            assert "add" in names
            assert "hidden_admin" in names

    @pytest.mark.asyncio
    async def test_proxy_call_tool(self):
        """Proxy forwards tool calls to the backend."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")

        async with Client(proxy) as client:
            result = await client.call_tool("echo", {"message": "hello"})
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_proxy_with_hidden_tools(self):
        """Proxy + ToolFilterMiddleware hides tools."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")
        config = ServerConfig(
            command="unused",
            hidden_tools=["hidden_admin"],
        )
        _apply_middleware(proxy, config)

        async with Client(proxy) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "echo" in names
            assert "hidden_admin" not in names

    @pytest.mark.asyncio
    async def test_proxy_with_exposed_tools(self):
        """Proxy + ToolFilterMiddleware exposes only whitelisted tools."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")
        config = ServerConfig(
            command="unused",
            exposed_tools=["echo"],
        )
        _apply_middleware(proxy, config)

        async with Client(proxy) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert names == {"echo"}

    @pytest.mark.asyncio
    async def test_proxy_with_tool_override(self):
        """Proxy + ToolOverrideMiddleware renames and reverse-maps."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")
        config = ServerConfig(
            command="unused",
            tool_overrides={
                "echo": ToolOverride(name="shout", description="Shout a message"),
            },
        )
        _apply_middleware(proxy, config)

        async with Client(proxy) as client:
            # Verify renamed in list
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "shout" in names
            assert "echo" not in names

            # Verify call with renamed name works
            result = await client.call_tool("shout", {"message": "hi"})
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_proxy_combined_filter_and_override(self):
        """Filter + override together on a proxy."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")
        config = ServerConfig(
            command="unused",
            hidden_tools=["hidden_admin"],
            tool_overrides={
                "echo": ToolOverride(name="shout", description=None),
            },
        )
        _apply_middleware(proxy, config)

        async with Client(proxy) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}

            assert "hidden_admin" not in names
            assert "shout" in names
            assert "add" in names

            result = await client.call_tool("shout", {"message": "test"})
            assert not result.is_error


# ---------------------------------------------------------------------------
# http_app — verify it returns a valid ASGI app
# ---------------------------------------------------------------------------

class TestHttpApp:
    @pytest.mark.asyncio
    async def test_http_app_returns_starlette(self):
        """proxy.http_app() returns a Starlette-compatible ASGI app."""
        from fastmcp.server import create_proxy

        backend = _make_echo_server()
        proxy = create_proxy(backend, name="test-proxy")
        app = proxy.http_app(path="/mcp")

        assert callable(app)
        assert hasattr(app, "lifespan")
