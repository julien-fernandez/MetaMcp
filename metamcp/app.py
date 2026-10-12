"""Main ASGI application — dynamic MCP dispatch + health/restart routes.

Architecture:
  - /servers/{name}/mcp  →  dispatched dynamically to BackendProxy.http_app
  - /health, /servers/{name}/health, /restart, etc. → Starlette routes
  - Lifespan manages backend startup/shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .backend import BackendProxy
from .config import Config, load_config, CONFIG_PATH
from .logger import log

# Module-level state (populated at startup)
backends: dict[str, BackendProxy] = {}
config: Config | None = None

# Regex for MCP endpoint paths: /servers/{name}/mcp or /servers/{name}/mcp/...
_MCP_PATH_RE = re.compile(r"^/servers/([^/]+)/mcp(/.*)?$")

# Heartbeat interval (seconds)
_HEARTBEAT_INTERVAL = 60


def _get_rss_mb() -> int:
    """Get current process RSS in MB (Windows + Unix)."""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
    except ImportError:
        pass
    # Fallback: /proc on Linux
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024  # kB → MB
    except (OSError, ValueError):
        pass
    return -1


def _fmt_duration(seconds: float) -> str:
    """Format seconds as e.g. '2h13m' or '45m' or '30s'."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# -----------------------------------------------------------------------
# Health & restart routes (Starlette)
# -----------------------------------------------------------------------

async def health(request: Request) -> JSONResponse:
    statuses: dict[str, Any] = {}
    for name, bp in backends.items():
        entry: dict[str, Any] = {
            "connected": bp.connected,
        }
        if bp.is_lazy:
            entry["lazy"] = True
            entry["lazyStatus"] = "idle" if bp.is_lazy_idle else "running"
        statuses[name] = entry
    return JSONResponse({"status": "ok", "servers": statuses})


async def backend_health(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    bp = backends.get(name)
    if not bp:
        return JSONResponse({"error": f"Unknown server: {name}"}, status_code=404)
    return JSONResponse({
        "status": "ok" if bp.connected else "disconnected",
    })


async def restart_backend(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    bp = backends.get(name)
    if not bp:
        return JSONResponse({"error": f"Unknown server: {name}"}, status_code=404)
    if bp._starting:
        return JSONResponse(
            {"error": f"Server {name} is already starting"}, status_code=409
        )

    log.info(f"[{name}] Restart requested via API")
    try:
        await bp.stop()
        await bp.start()
        return JSONResponse({"success": True, "name": name, "status": "restarted"})
    except Exception as e:
        log.error(f"[{name}] Restart failed: {e}")
        return JSONResponse(
            {"error": f"Restart failed: {e}", "server": name}, status_code=500
        )


async def call_tool(request: Request) -> JSONResponse:
    """Simple REST API for calling MCP tools — no session dance required.

    POST /servers/{name}/call
    { "tool": "search_pages", "args": { "query": "..." } }
    → { "result": [...] }
    """
    name = request.path_params["name"]
    bp = backends.get(name)
    if not bp:
        return JSONResponse({"error": f"Unknown server: {name}"}, status_code=404)

    # Auto-start lazy backends
    if bp.is_lazy and not bp.connected and not bp._stopping and not bp._starting:
        log.info(f"[{name}] Lazy start triggered by /call")
        try:
            await bp.start()
        except Exception as e:
            return JSONResponse(
                {"error": f"Failed to start backend '{name}': {e}"},
                status_code=503,
            )

    if not bp.proxy:
        return JSONResponse(
            {"error": f"Backend '{name}' not connected"},
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"}, status_code=400
        )

    tool_name = body.get("tool")
    if not tool_name:
        return JSONResponse(
            {"error": "Missing 'tool' field"}, status_code=400
        )
    args = body.get("args", {})

    t0 = time.perf_counter()
    try:
        result = await bp.proxy.call_tool(tool_name, args)
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        log.error(f"[{name}] /call {tool_name} failed ({ms:.0f}ms): {e}")
        return JSONResponse(
            {"error": str(e), "tool": tool_name},
            status_code=500,
        )

    ms = (time.perf_counter() - t0) * 1000
    log.info(f"[{name}] /call {tool_name} → OK ({ms:.0f}ms)")

    # Serialize ToolResult content blocks to JSON-friendly format
    content = []
    for block in result.content:
        if hasattr(block, "model_dump"):
            content.append(block.model_dump())
        elif hasattr(block, "text"):
            content.append({"type": "text", "text": block.text})
        else:
            content.append({"type": "unknown", "data": str(block)})

    response: dict[str, Any] = {"result": content}
    if result.structured_content is not None:
        response["structured"] = result.structured_content
    return JSONResponse(response)


async def list_backend_tools(request: Request) -> JSONResponse:
    """List available tools for a backend.

    GET /servers/{name}/tools
    → { "tools": [{ "name": "...", "description": "...", "inputSchema": {...} }] }
    """
    name = request.path_params["name"]
    bp = backends.get(name)
    if not bp:
        return JSONResponse({"error": f"Unknown server: {name}"}, status_code=404)

    if bp.is_lazy and not bp.connected and not bp._stopping and not bp._starting:
        log.info(f"[{name}] Lazy start triggered by /tools")
        try:
            await bp.start()
        except Exception as e:
            return JSONResponse(
                {"error": f"Failed to start backend '{name}': {e}"},
                status_code=503,
            )

    if not bp.proxy:
        return JSONResponse(
            {"error": f"Backend '{name}' not connected"},
            status_code=503,
        )

    try:
        tools = await bp.proxy.list_tools()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    tool_list = []
    for t in tools:
        entry: dict[str, Any] = {"name": t.name}
        if t.description:
            entry["description"] = t.description
        if hasattr(t, "inputSchema") and t.inputSchema:
            entry["inputSchema"] = t.inputSchema
        elif hasattr(t, "parameters") and t.parameters:
            entry["inputSchema"] = t.parameters
        tool_list.append(entry)

    return JSONResponse({"tools": tool_list})


async def restart_all(request: Request) -> JSONResponse:
    log.info("[MetaMcp] Full restart requested via API")

    # Stop all
    await asyncio.gather(
        *(bp.stop() for bp in backends.values()),
        return_exceptions=True,
    )

    # Restart all in parallel
    import time
    t0 = time.monotonic()
    results: list[dict[str, str]] = []

    async def _start_one(name: str, bp: BackendProxy) -> None:
        try:
            if bp.is_lazy:
                await bp.start_lazy()
            else:
                await bp.start()
            results.append({"name": name, "status": "restarted"})
        except Exception:
            results.append({"name": name, "status": "failed"})

    await asyncio.gather(
        *(_start_one(n, b) for n, b in backends.items()),
        return_exceptions=True,
    )

    elapsed = time.monotonic() - t0
    log.info(f"[MetaMcp] Full restart completed in {elapsed:.1f}s")
    return JSONResponse({"status": "restarted", "backends": results})


# Starlette routes for non-MCP endpoints
_starlette_routes = [
    Route("/health", health),
    Route("/servers/{name}/health", backend_health),
    Route("/servers/{name}/call", call_tool, methods=["POST"]),
    Route("/servers/{name}/tools", list_backend_tools),
    Route("/servers/{name}/restart", restart_backend, methods=["POST"]),
    Route("/restart", restart_all, methods=["POST"]),
]


# -----------------------------------------------------------------------
# ASGI dispatch
# -----------------------------------------------------------------------

class MetaMcpApp:
    """Top-level ASGI application.

    Handles the ASGI lifespan protocol, dynamically dispatches
    ``/servers/{name}/mcp`` requests to the correct ``BackendProxy.http_app``,
    and falls through to Starlette for health/restart routes.
    """

    def __init__(self, cfg: Config) -> None:
        global config
        config = cfg
        self._starlette = Starlette(routes=_starlette_routes)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return

        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")

            # Try MCP dispatch
            m = _MCP_PATH_RE.match(path)
            if m:
                await self._dispatch_mcp(
                    m.group(1), m.group(2) or "", scope, receive, send
                )
                return

        # Fallthrough to Starlette (health, restart, etc.)
        await self._starlette(scope, receive, send)

    # ---- MCP dispatch ----

    async def _dispatch_mcp(
        self,
        name: str,
        rest: str,
        scope: dict,
        receive: Any,
        send: Any,
    ) -> None:
        t0 = time.perf_counter()
        http_method = scope.get("method", "?")
        response_status: int | None = None

        original_send = send

        async def logging_send(msg: dict) -> None:
            nonlocal response_status
            if msg.get("type") == "http.response.start" and response_status is None:
                response_status = msg.get("status")
            await original_send(msg)

        send = logging_send

        bp = backends.get(name)

        try:
            if not bp:
                resp = JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": f"Unknown server: {name}"},
                        "id": None,
                    },
                    status_code=404,
                )
                await resp(scope, receive, send)
                return

            # Auto-start lazy backends on first request
            if bp.is_lazy and not bp.connected and not bp._stopping and not bp._starting:
                log.info(f"[{name}] Lazy start triggered by incoming request")
                try:
                    await bp.start()
                except Exception as e:
                    resp = JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32000,
                                "message": f"Failed to start lazy backend '{name}': {e}",
                            },
                            "id": None,
                        },
                        status_code=503,
                    )
                    await resp(scope, receive, send)
                    return

            if not bp.http_app:
                resp = JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32000,
                            "message": f"Backend '{name}' not connected",
                        },
                        "id": None,
                    },
                    status_code=503,
                )
                await resp(scope, receive, send)
                return

            # Rewrite path so FastMCP's http_app sees /mcp
            inner_scope = dict(scope)
            inner_scope["path"] = "/mcp" + rest
            inner_scope["root_path"] = scope.get("root_path", "")

            # Check for mcp-session-id header (stale session detection)
            has_session = any(
                k == b"mcp-session-id" for k, _ in inner_scope.get("headers", [])
            )

            if not has_session:
                # No session ID — fresh request, forward directly
                await bp.http_app(inner_scope, receive, send)
                return

            # Has session ID — pre-read body so we can replay on stale session
            body_chunks: list[dict] = []
            while True:
                msg = await receive()
                body_chunks.append(msg)
                if msg.get("type") != "http.request" or not msg.get("more_body", False):
                    break

            response_msgs: list[dict] = []

            async def buffering_send(msg: dict) -> None:
                response_msgs.append(msg)

            await self._replay_request(bp.http_app, inner_scope, body_chunks, buffering_send, receive)

            # Check if response was 404 (stale session)
            resp_status = None
            for msg in response_msgs:
                if msg["type"] == "http.response.start":
                    resp_status = msg.get("status")
                    break

            if resp_status != 404:
                # Not stale — flush buffered response to client
                for msg in response_msgs:
                    await send(msg)
                return

            # ---- Stale session auto-recovery ----
            log.info(f"[{name}] Stale session detected, auto-recovering...")

            # Parse original request body to check method
            raw_body = b"".join(c.get("body", b"") for c in body_chunks)
            try:
                req_data = json.loads(raw_body)
                method = req_data.get("method", "")
            except (json.JSONDecodeError, AttributeError):
                method = ""

            # Headers without mcp-session-id
            clean_headers = [
                (k, v)
                for k, v in inner_scope.get("headers", [])
                if k != b"mcp-session-id"
            ]

            if method == "initialize":
                # Original request IS initialize — just strip session ID and retry
                retry_scope = dict(inner_scope)
                retry_scope["headers"] = clean_headers
                await self._replay_request(bp.http_app, retry_scope, body_chunks, send, receive)
                return

            # Original request is NOT initialize — must auto-init first
            new_session_id = await self._auto_initialize(bp.http_app, inner_scope, clean_headers)
            if not new_session_id:
                log.error(f"[{name}] Auto-recovery failed, returning original 404")
                for msg in response_msgs:
                    await send(msg)
                return

            log.info(f"[{name}] Auto-recovered, new session: {new_session_id.decode()}")

            # Replay original request with new session ID
            replay_headers = [
                (k, v)
                for k, v in inner_scope.get("headers", [])
                if k != b"mcp-session-id"
            ]
            replay_headers.append((b"mcp-session-id", new_session_id))
            replay_scope = dict(inner_scope)
            replay_scope["headers"] = replay_headers
            await self._replay_request(bp.http_app, replay_scope, body_chunks, send, receive)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            log.info(
                f"[{name}] {http_method} /mcp → {response_status or '?'} ({ms:.0f}ms)"
            )

    # ---- Stale session recovery helpers ----

    @staticmethod
    async def _replay_request(
        app: Any,
        scope: dict,
        body_chunks: list[dict],
        send: Any,
        original_receive: Any | None = None,
    ) -> None:
        """Replay a buffered request body through an ASGI app.

        After body chunks are exhausted, delegates to ``original_receive``
        for disconnect detection (important for SSE streams).
        """
        idx = 0

        async def replay_receive() -> dict:
            nonlocal idx
            if idx < len(body_chunks):
                msg = body_chunks[idx]
                idx += 1
                return msg
            # After body, wait for real client disconnect
            if original_receive is not None:
                return await original_receive()
            # Fallback: block forever (will be cancelled when app finishes)
            await asyncio.get_event_loop().create_future()
            return {"type": "http.disconnect"}

        await app(scope, replay_receive, send)

    @staticmethod
    async def _auto_initialize(
        app: Any, original_scope: dict, clean_headers: list[tuple[bytes, bytes]],
    ) -> bytes | None:
        """Send initialize + initialized to create a fresh session.

        Returns the new mcp-session-id bytes, or None on failure.
        """
        # --- Step 1: initialize ---
        init_body = json.dumps({
            "jsonrpc": "2.0",
            "id": "__auto_init__",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "metamcp-auto-recovery",
                    "version": "1.0",
                },
            },
        }).encode()

        init_headers = [
            (k, v) for k, v in clean_headers if k != b"content-length"
        ]
        init_headers.append((b"content-length", str(len(init_body)).encode()))

        init_scope = dict(original_scope)
        init_scope["headers"] = init_headers

        sent_body = False

        async def init_receive() -> dict:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": init_body, "more_body": False}
            return {"type": "http.disconnect"}

        init_response: list[dict] = []

        async def init_send(msg: dict) -> None:
            init_response.append(msg)

        await app(init_scope, init_receive, init_send)

        # Extract new session ID
        new_session_id: bytes | None = None
        init_status: int | None = None
        for msg in init_response:
            if msg["type"] == "http.response.start":
                init_status = msg.get("status")
                for k, v in msg.get("headers", []):
                    if k == b"mcp-session-id":
                        new_session_id = v
                        break
                break

        if not new_session_id or init_status not in (200, 201):
            return None

        # --- Step 2: initialized notification ---
        notif_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }).encode()

        notif_headers = [
            (k, v) for k, v in clean_headers
            if k not in (b"content-length", b"mcp-session-id")
        ]
        notif_headers.append((b"content-length", str(len(notif_body)).encode()))
        notif_headers.append((b"mcp-session-id", new_session_id))

        notif_scope = dict(original_scope)
        notif_scope["headers"] = notif_headers

        sent_notif = False

        async def notif_receive() -> dict:
            nonlocal sent_notif
            if not sent_notif:
                sent_notif = True
                return {"type": "http.request", "body": notif_body, "more_body": False}
            return {"type": "http.disconnect"}

        # Fire-and-forget the notification response
        async def notif_send(msg: dict) -> None:
            pass

        await app(notif_scope, notif_receive, notif_send)

        return new_session_id

    # ---- Lifespan ----

    async def _handle_lifespan(self, scope: dict, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self._startup()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as e:
                    log.error(f"[MetaMcp] Startup failed: {e}")
                    await send(
                        {"type": "lifespan.startup.failed", "message": str(e)}
                    )
                    return
            elif message["type"] == "lifespan.shutdown":
                await self._shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _startup(self) -> None:
        """Start all backends + hot-reload watcher."""
        import time
        from .watcher import start_watcher

        log.info("MetaMcp v2 — MCP HTTP Multiplexer (FastMCP)")
        log.info("=" * 46)

        t0 = time.monotonic()
        results: list[tuple[str, bool]] = []

        async def _start_one(name: str, srv_config: Any) -> None:
            bp = BackendProxy(name, srv_config)
            backends[name] = bp
            if srv_config.lazy:
                await bp.start_lazy()
                results.append((name, True))
            else:
                try:
                    await bp.start()
                    results.append((name, True))
                except Exception:
                    log.error(f"[{name}] Skipping (failed to start)")
                    results.append((name, False))

        await asyncio.gather(
            *(_start_one(n, sc) for n, sc in config.servers.items()),
            return_exceptions=True,
        )

        elapsed = time.monotonic() - t0
        log.info(f"All backends started in {elapsed:.1f}s")

        # Strict mode: abort if any backend failed
        if config.panic_if_invalid:
            failed = [n for n, ok in results if not ok]
            if failed:
                raise RuntimeError(
                    f"panicIfInvalid is set and these backends failed: "
                    f"{', '.join(failed)}"
                )

        # Hot-reload watcher disabled — restart MetaMCP manually instead
        # start_watcher(CONFIG_PATH, backends)

        # Print startup summary
        eager_ok = sum(
            1 for n, ok in results if ok and not config.servers[n].lazy
        )
        lazy_count = sum(
            1 for n, ok in results if ok and config.servers[n].lazy
        )
        failed_count = sum(1 for _, ok in results if not ok)

        log.info("=" * 46)
        log.info("MetaMcp HTTP Multiplexer (v2 — FastMCP)")
        log.info("=" * 46)
        log.info(f"URL:      http://127.0.0.1:{config.port}")
        log.info(f"Health:   http://127.0.0.1:{config.port}/health")
        log.info(
            f"Backends: {eager_ok} connected, {lazy_count} lazy, {failed_count} failed"
        )
        log.info("Hot-reload: disabled (use /restart-metamcp)")
        log.info("-" * 46)
        for name, ok in results:
            is_lazy = config.servers[name].lazy
            status = "LAZY" if is_lazy else ("OK" if ok else "FAILED")
            log.info(f"  {name}: {status}")
            if ok:
                log.info(
                    f"    -> http://127.0.0.1:{config.port}/servers/{name}/mcp"
                )
        log.info("=" * 46)

        # Pre-warm backends by calling list_tools (caches connection + tool list)
        asyncio.create_task(self._prewarm_backends(results))

        # Start heartbeat (forensic logging for deadlock diagnosis)
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _prewarm_backends(self, results: list[tuple[str, bool]]) -> None:
        """Call list_tools on each connected backend to warm the connection.

        Retries once (after 30s) for backends that return 0 tools,
        which can happen when the backend is still initializing.
        """
        warm_names = [n for n, ok in results if ok and not config.servers[n].lazy]
        if not warm_names:
            return

        log.info(f"[MetaMcp] Pre-warming {len(warm_names)} backends...")
        t0 = time.monotonic()
        retry_names: list[str] = []

        async def _warm_one(name: str) -> int:
            bp = backends.get(name)
            if not bp or not bp.proxy:
                return -1
            try:
                tools = await bp.proxy.list_tools()
                count = len(tools)
                log.info(f"[{name}] Warmed: {count} tools")
                return count
            except Exception as e:
                log.warning(f"[{name}] Pre-warm failed: {e}")
                return -1

        results_map: dict[str, int] = {}
        warm_results = await asyncio.gather(
            *(_warm_one(n) for n in warm_names), return_exceptions=True,
        )
        for name, count in zip(warm_names, warm_results):
            c = count if isinstance(count, int) else -1
            results_map[name] = c
            if c == 0:
                retry_names.append(name)

        elapsed = time.monotonic() - t0
        log.info(f"[MetaMcp] Pre-warm complete in {elapsed:.1f}s")

        # Retry backends that returned 0 tools (likely still initializing)
        if retry_names:
            log.info(
                f"[MetaMcp] {len(retry_names)} backends returned 0 tools, "
                f"retrying in 30s: {', '.join(retry_names)}"
            )
            await asyncio.sleep(30)
            for name in retry_names:
                count = await _warm_one(name)
                if count == 0:
                    log.warning(f"[{name}] Still 0 tools after retry")
                elif count < 0:
                    log.warning(f"[{name}] Retry also failed")

    async def _heartbeat(self) -> None:
        """Periodic event loop health check — forensic evidence for deadlocks.

        Logs uptime, event loop latency, backend status, task count, and memory
        every 60s. If the event loop freezes, the last heartbeat timestamp in
        the log pinpoints when the deadlock occurred.
        """
        start = time.monotonic()
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                # Measure event loop responsiveness
                t0 = time.monotonic()
                await asyncio.sleep(0)
                latency = time.monotonic() - t0

                uptime = time.monotonic() - start
                n_tasks = len(asyncio.all_tasks())
                connected = sum(1 for b in backends.values() if b.connected)
                total = len(backends)
                mem_mb = _get_rss_mb()

                mem_str = f", mem={mem_mb}MB" if mem_mb >= 0 else ""
                msg = (
                    f"[MetaMcp] Heartbeat: uptime={_fmt_duration(uptime)}, "
                    f"loop_latency={latency:.3f}s, backends={connected}/{total}, "
                    f"tasks={n_tasks}{mem_str}"
                )
                if latency > 1.0:
                    log.warning(msg)
                else:
                    log.info(msg)
        except asyncio.CancelledError:
            pass

    async def _shutdown(self) -> None:
        """Stop all backends gracefully."""
        log.info("[MetaMcp] Shutting down...")
        # Cancel heartbeat
        if hasattr(self, "_heartbeat_task") and self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        from .watcher import stop_watcher
        stop_watcher()
        await asyncio.gather(
            *(bp.stop() for bp in backends.values()),
            return_exceptions=True,
        )
        log.info("[MetaMcp] Shutdown complete")


# -----------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------

def build_app(cfg: Config | None = None) -> MetaMcpApp:
    """Build the ASGI application."""
    if cfg is None:
        cfg = load_config()
    return MetaMcpApp(cfg)
