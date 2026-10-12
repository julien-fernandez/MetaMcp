"""MCP-level logging middleware — logs tool calls, resource reads, etc. with timing."""

from __future__ import annotations

import time

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .logger import log

# Threshold for slow request warnings (ms)
SLOW_THRESHOLD_MS = 5000


class LoggingMiddleware(Middleware):
    """Per-backend middleware that logs every MCP request with timing.

    Added as the outermost middleware in BackendProxy._create_proxy() so it
    sees client-facing tool names (after ToolOverrideMiddleware renames).
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend

    def _log(self, op: str, detail: str, ms: float, *, error: Exception | None = None) -> None:
        """Unified log helper with slow-request detection."""
        if error:
            log.error(f"[{self.backend}] {op} {detail} FAILED: {error} ({ms:.0f}ms)")
        elif ms > SLOW_THRESHOLD_MS:
            log.warning(f"[{self.backend}] SLOW {op} {detail} ({ms:.0f}ms)")
        else:
            log.info(f"[{self.backend}] {op} {detail} ({ms:.0f}ms)")

    # ------------------------------------------------------------------
    # Initialize
    # ------------------------------------------------------------------

    async def on_initialize(self, context: MiddlewareContext, call_next):
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
            self._log("INIT", "session", (time.perf_counter() - t0) * 1000)
            return result
        except Exception as e:
            self._log("INIT", "session", (time.perf_counter() - t0) * 1000, error=e)
            raise

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
            self._log("TOOL", tool_name, (time.perf_counter() - t0) * 1000)
            return result
        except Exception as e:
            self._log("TOOL", tool_name, (time.perf_counter() - t0) * 1000, error=e)
            raise

    # ------------------------------------------------------------------
    # List tools
    # ------------------------------------------------------------------

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        t0 = time.perf_counter()
        try:
            tools = await call_next(context)
            self._log("LIST_TOOLS", f"→ {len(tools)} tools", (time.perf_counter() - t0) * 1000)
            return tools
        except Exception as e:
            self._log("LIST_TOOLS", "", (time.perf_counter() - t0) * 1000, error=e)
            raise

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    async def on_list_resources(self, context: MiddlewareContext, call_next):
        t0 = time.perf_counter()
        try:
            resources = await call_next(context)
            self._log("LIST_RESOURCES", f"→ {len(resources)}", (time.perf_counter() - t0) * 1000)
            return resources
        except Exception as e:
            self._log("LIST_RESOURCES", "", (time.perf_counter() - t0) * 1000, error=e)
            raise

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        uri = getattr(context.message, "uri", "?")
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
            self._log("READ_RESOURCE", str(uri), (time.perf_counter() - t0) * 1000)
            return result
        except Exception as e:
            self._log("READ_RESOURCE", str(uri), (time.perf_counter() - t0) * 1000, error=e)
            raise

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    async def on_list_prompts(self, context: MiddlewareContext, call_next):
        t0 = time.perf_counter()
        try:
            prompts = await call_next(context)
            self._log("LIST_PROMPTS", f"→ {len(prompts)}", (time.perf_counter() - t0) * 1000)
            return prompts
        except Exception as e:
            self._log("LIST_PROMPTS", "", (time.perf_counter() - t0) * 1000, error=e)
            raise

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", "?")
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
            self._log("GET_PROMPT", name, (time.perf_counter() - t0) * 1000)
            return result
        except Exception as e:
            self._log("GET_PROMPT", name, (time.perf_counter() - t0) * 1000, error=e)
            raise
