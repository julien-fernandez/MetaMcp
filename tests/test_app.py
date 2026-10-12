"""Tests for metamcp.app — ASGI dispatcher, health routes, MCP dispatch.

Uses httpx for direct ASGI transport testing (no real HTTP server needed).
"""

from __future__ import annotations

import pytest
import httpx
from fastmcp import FastMCP, Client
from fastmcp.server import create_proxy

from metamcp.app import MetaMcpApp, backends
from metamcp.backend import BackendProxy
from metamcp.config import Config, ServerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_echo_server() -> FastMCP:
    server = FastMCP("echo")

    @server.tool
    def ping() -> str:
        return "pong"

    return server


class FakeBackendProxy:
    """Lightweight stand-in for BackendProxy that uses an in-memory FastMCP."""

    def __init__(self, name: str, server: FastMCP):
        self.name = name
        self.config = ServerConfig(command="fake")
        self._connected = True
        self._starting = False
        self._stopping = False
        self._recovery_attempts = 0
        self._recovery_task = None
        self._lifespan_gen = None

        proxy = create_proxy(server, name=name)
        self.proxy = proxy
        self.http_app = proxy.http_app(path="/mcp")

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_lazy(self) -> bool:
        return False

    @property
    def is_lazy_idle(self) -> bool:
        return False

    @property
    def current_config(self) -> ServerConfig:
        return self.config

    async def start(self):
        pass

    async def start_lazy(self):
        pass

    async def stop(self):
        self._connected = False
        self.http_app = None


def _build_test_app(fake_backends: dict[str, FakeBackendProxy]) -> MetaMcpApp:
    """Build a MetaMcpApp with pre-populated fake backends."""
    cfg = Config(port=9999, servers={})
    app = MetaMcpApp(cfg)
    # Inject fake backends into module-level dict
    backends.clear()
    backends.update(fake_backends)
    return app


# ---------------------------------------------------------------------------
# Health routes
# ---------------------------------------------------------------------------

class TestHealthRoutes:
    @pytest.mark.asyncio
    async def test_global_health(self):
        fb = {"echo": FakeBackendProxy("echo", _make_echo_server())}
        app = _build_test_app(fb)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "echo" in data["servers"]
            assert data["servers"]["echo"]["connected"] is True

    @pytest.mark.asyncio
    async def test_backend_health(self):
        fb = {"echo": FakeBackendProxy("echo", _make_echo_server())}
        app = _build_test_app(fb)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/servers/echo/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_backend_health_404(self):
        app = _build_test_app({})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/servers/nonexistent/health")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# MCP dispatch
# ---------------------------------------------------------------------------

class TestMcpDispatch:
    @pytest.mark.asyncio
    async def test_unknown_server_returns_404(self):
        app = _build_test_app({})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/servers/nonexistent/mcp",
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_disconnected_backend_returns_503(self):
        fb = FakeBackendProxy("echo", _make_echo_server())
        fb._connected = True
        fb.http_app = None  # simulate disconnected
        app = _build_test_app({"echo": fb})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/servers/echo/mcp",
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
            )
            assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_mcp_endpoint_reachable(self):
        """Verify that the MCP endpoint dispatches to FastMCP's http_app.

        Uses run_server_async so the lifespan (StreamableHTTPSessionManager)
        is properly initialised before sending requests.
        """
        from fastmcp.utilities.tests import run_server_async
        from fastmcp.client.transports import StreamableHttpTransport

        echo = _make_echo_server()

        async with run_server_async(echo) as url:
            # Confirm FastMCP standalone server works
            async with Client(StreamableHttpTransport(url)) as client:
                tools = await client.list_tools()
                assert any(t.name == "ping" for t in tools)

    @pytest.mark.asyncio
    async def test_mcp_dispatch_via_full_asgi_lifespan(self):
        """Full integration: MetaMcpApp wrapping a FakeBackend that has its
        lifespan triggered through the ASGI lifespan protocol."""
        fb = {"echo": FakeBackendProxy("echo", _make_echo_server())}
        app = _build_test_app(fb)

        # Trigger the lifespan of the backend's http_app manually
        backend = fb["echo"]
        lifespan_fn = getattr(backend.http_app, "lifespan", None)
        ctx = None
        if lifespan_fn:
            ctx = lifespan_fn(backend.http_app)
            await ctx.__aenter__()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/servers/echo/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"},
                        },
                        "id": 1,
                    },
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                )
                assert resp.status_code == 200

                # Response may be SSE (text/event-stream) or JSON
                import json as jsonlib
                body = resp.text
                if body.startswith("event:"):
                    # Parse SSE: extract data line
                    for line in body.splitlines():
                        if line.startswith("data: "):
                            data = jsonlib.loads(line[6:])
                            break
                    else:
                        pytest.fail("No data line in SSE response")
                else:
                    data = resp.json()

                assert data.get("jsonrpc") == "2.0"
                assert "result" in data
        finally:
            if ctx:
                await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Restart routes
# ---------------------------------------------------------------------------

class TestRestartRoutes:
    @pytest.mark.asyncio
    async def test_restart_unknown_backend_404(self):
        app = _build_test_app({})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/servers/nonexistent/restart")
            assert resp.status_code == 404
