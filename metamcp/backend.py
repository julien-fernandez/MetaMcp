"""BackendProxy — wraps a FastMCP proxy for a single stdio backend.

Manages the lifecycle (start / stop / recovery) and exposes an ASGI
``http_app`` that the main dispatcher forwards MCP requests to.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy

from .config import ServerConfig
from .logger import log
from .logging_middleware import LoggingMiddleware
from .transforms import ToolFilterMiddleware, ToolOverrideMiddleware

BACKOFF_DELAYS = [1, 2, 5, 10, 30]  # seconds
MAX_RECOVERY = 5
SESSION_TTL_S = 30 * 60  # 30 min — matches TS version
LAZY_IDLE_S = 10 * 60  # auto-stop lazy backends after 10 min

# HTTP backend startup retry (servers may not be ready when MetaMcp starts)
HTTP_STARTUP_RETRIES = 5
HTTP_STARTUP_DELAYS = [2, 4, 8, 15, 30]  # seconds between retries


class BackendProxy:
    """One-per-backend wrapper around FastMCP's ``create_proxy``."""

    def __init__(self, name: str, config: ServerConfig) -> None:
        self.name = name
        self.config = config

        # FastMCP objects
        self.proxy: Any | None = None
        self.http_app: Any | None = None  # Starlette ASGI app

        # State flags
        self._connected = False
        self._starting = False
        self._stopping = False
        self._recovery_attempts = 0
        self._recovery_task: asyncio.Task | None = None

        # Lifespan context manager (from FastMCP's http_app)
        self._lifespan_ctx: Any | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_lazy(self) -> bool:
        return self.config.lazy

    @property
    def is_lazy_idle(self) -> bool:
        return self.config.lazy and not self._connected

    @property
    def current_config(self) -> ServerConfig:
        return self.config

    # ------------------------------------------------------------------
    # Build environment for subprocess
    # ------------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """Inherit current env + backend-specific vars."""
        env = dict(os.environ)
        if self.config.env:
            env.update(self.config.env)
        return env

    # ------------------------------------------------------------------
    # Create FastMCP proxy
    # ------------------------------------------------------------------

    def _create_proxy(self) -> Any:
        """Create the FastMCP proxy for this backend."""
        if self.config.is_http:
            # HTTP mode: proxy to an upstream URL
            target = self.config.url
            log.info(f"[{self.name}] HTTP proxy to {target}")
        else:
            # stdio mode: spawn subprocess
            target = StdioTransport(
                command=self.config.command,
                args=self.config.args,
                env=self._build_env(),
                cwd=self.config.cwd,
            )

        timeout_s = self.config.request_timeout / 1000  # ms → seconds
        client = Client(target, timeout=timeout_s)
        proxy = create_proxy(client, name=self.name)

        # Middleware order (first added = outermost):
        # 1. LoggingMiddleware (outer) — logs with client-facing names & timing
        # 2. ToolOverrideMiddleware — renames on list, reverse-maps on call
        # 3. ToolFilterMiddleware (inner) — filters using ORIGINAL names
        proxy.add_middleware(LoggingMiddleware(self.name))

        if self.config.tool_overrides:
            overrides = {
                name: {"name": ov.name, "description": ov.description}
                for name, ov in self.config.tool_overrides.items()
            }
            proxy.add_middleware(ToolOverrideMiddleware(overrides))

        if self.config.exposed_tools or self.config.hidden_tools:
            proxy.add_middleware(
                ToolFilterMiddleware(
                    exposed=self.config.exposed_tools,
                    hidden=self.config.hidden_tools,
                )
            )

        return proxy

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the backend process and connect.

        For HTTP backends, retries with backoff if the upstream server
        isn't ready yet (common at boot when servers start concurrently).
        """
        if self._starting:
            log.warning(f"[{self.name}] start() already in progress, skipping")
            return
        self._starting = True

        try:
            if self.config.is_http:
                await self._start_http()
            else:
                await self._start_stdio()
        finally:
            self._starting = False

    async def _start_stdio(self) -> None:
        """Start a stdio backend (single attempt)."""
        log.info(
            f"[{self.name}] Spawning: {self.config.command} "
            f"{' '.join(self.config.args)}"
        )
        try:
            await self._connect()
            log.info(f"[{self.name}] Connected")
        except Exception as e:
            self._connected = False
            log.error(f"[{self.name}] Failed to connect: {e}")
            raise

    async def _start_http(self) -> None:
        """Start an HTTP backend with retry + backoff."""
        log.info(f"[{self.name}] Connecting to: {self.config.url}")

        for attempt in range(HTTP_STARTUP_RETRIES + 1):
            try:
                await self._connect()
                if attempt > 0:
                    log.info(
                        f"[{self.name}] Connected (after {attempt} "
                        f"{'retry' if attempt == 1 else 'retries'})"
                    )
                else:
                    log.info(f"[{self.name}] Connected")
                return
            except Exception as e:
                self._connected = False
                if attempt >= HTTP_STARTUP_RETRIES:
                    log.error(
                        f"[{self.name}] Failed to connect after "
                        f"{HTTP_STARTUP_RETRIES} retries: {e}"
                    )
                    raise
                delay = HTTP_STARTUP_DELAYS[
                    min(attempt, len(HTTP_STARTUP_DELAYS) - 1)
                ]
                log.warning(
                    f"[{self.name}] Connection failed ({e}), "
                    f"retry {attempt + 1}/{HTTP_STARTUP_RETRIES} in {delay}s..."
                )
                await asyncio.sleep(delay)
                # Clean up failed proxy before retry
                await self._cleanup_proxy()

    async def _connect(self) -> None:
        """Create proxy, trigger lifespan, mark connected."""
        self.proxy = self._create_proxy()
        self.http_app = self.proxy.http_app(path="/mcp")

        # Trigger FastMCP's lifespan startup (connects the client / spawns process)
        # http_app.lifespan is an @asynccontextmanager factory: (app) -> AsyncContextManager
        try:
            lifespan_fn = getattr(self.http_app, "lifespan", None)
            if lifespan_fn and callable(lifespan_fn):
                self._lifespan_ctx = lifespan_fn(self.http_app)
                await self._lifespan_ctx.__aenter__()
        except Exception as e:
            log.warning(
                f"[{self.name}] Lifespan startup failed ({e}), "
                f"connection will happen lazily on first request"
            )
            self._lifespan_ctx = None

        self._connected = True
        self._recovery_attempts = 0

        # Lower process priority on Windows
        self._lower_priority()

    async def _cleanup_proxy(self) -> None:
        """Clean up proxy state after a failed connection attempt."""
        if hasattr(self, "_lifespan_ctx") and self._lifespan_ctx:
            try:
                await self._lifespan_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._lifespan_ctx = None
        self.proxy = None
        self.http_app = None

    async def start_lazy(self) -> None:
        """Register a lazy backend without spawning."""
        log.info(f"[{self.name}] Registered (lazy — will start on first request)")

    async def stop(self) -> None:
        """Gracefully stop the backend."""
        self._stopping = True

        # Cancel pending recovery
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None

        # Trigger lifespan shutdown (closes client / kills subprocess)
        if hasattr(self, "_lifespan_ctx") and self._lifespan_ctx:
            try:
                await self._lifespan_ctx.__aexit__(None, None, None)
            except Exception as e:
                # FastMCP's ContextVar cross-context error is benign
                if "different Context" in str(e):
                    log.debug(f"[{self.name}] ContextVar shutdown noise (ignored)")
                else:
                    log.warning(f"[{self.name}] Lifespan shutdown error (ignored): {e}")
            self._lifespan_ctx = None

        self.proxy = None
        self.http_app = None
        self._connected = False
        self._stopping = False
        log.info(f"[{self.name}] Stopped")

    # ------------------------------------------------------------------
    # Auto-recovery
    # ------------------------------------------------------------------

    def schedule_recovery(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        if self._stopping:
            return
        if self._recovery_attempts >= MAX_RECOVERY:
            log.error(
                f"[{self.name}] Max recovery attempts ({MAX_RECOVERY}) reached, giving up"
            )
            return

        delay = BACKOFF_DELAYS[min(self._recovery_attempts, len(BACKOFF_DELAYS) - 1)]
        self._recovery_attempts += 1
        log.warning(
            f"[{self.name}] Recovery attempt "
            f"{self._recovery_attempts}/{MAX_RECOVERY} in {delay}s..."
        )

        async def _recover() -> None:
            await asyncio.sleep(delay)
            try:
                await self.stop()
                await self.start()
                log.info(f"[{self.name}] Recovery successful")
            except Exception as e:
                log.error(f"[{self.name}] Recovery failed: {e}")
                self.schedule_recovery()

        self._recovery_task = asyncio.create_task(_recover())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lower_priority(self) -> None:
        """Best-effort: lower subprocess priority on Windows."""
        if sys.platform != "win32":
            return
        try:
            # wmic process where ProcessId=... CALL setpriority 16384
            # (BELOW_NORMAL_PRIORITY_CLASS)
            # We don't have direct access to the PID from FastMCP's transport,
            # so we skip this for now. Can be added if FastMCP exposes the PID.
            pass
        except Exception:
            pass
