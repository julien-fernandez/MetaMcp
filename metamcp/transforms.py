"""Tool filtering and override middleware for FastMCP proxies."""

from __future__ import annotations

from fastmcp.server.middleware import Middleware, MiddlewareContext


class ToolFilterMiddleware(Middleware):
    """Filter tools by whitelist (exposed_tools) or blacklist (hidden_tools).

    Also blocks execution of filtered-out tools in on_call_tool for consistency.
    """

    def __init__(
        self,
        exposed: list[str] | None = None,
        hidden: list[str] | None = None,
    ):
        self.exposed = set(exposed) if exposed else None
        self.hidden = set(hidden) if hidden else None

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        if self.exposed is not None:
            return [t for t in tools if t.name in self.exposed]
        if self.hidden is not None:
            return [t for t in tools if t.name not in self.hidden]
        return tools

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        if self.exposed is not None and name not in self.exposed:
            from fastmcp.exceptions import ToolError
            raise ToolError(f"Tool '{name}' not found")
        if self.hidden is not None and name in self.hidden:
            from fastmcp.exceptions import ToolError
            raise ToolError(f"Tool '{name}' not found")
        return await call_next(context)


class ToolOverrideMiddleware(Middleware):
    """Rename tools and/or change descriptions.

    Transparently reverse-maps overridden names back to originals when
    forwarding call_tool to the backend.

    *overrides* maps **original** tool name → ``{"name": ..., "description": ...}``.
    """

    def __init__(self, overrides: dict[str, dict]):
        self.overrides = overrides
        # Reverse map: new_name → original_name
        self._reverse: dict[str, str] = {}
        for orig, ov in overrides.items():
            if ov.get("name"):
                self._reverse[ov["name"]] = orig

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        result = []
        for tool in tools:
            if tool.name in self.overrides:
                ov = self.overrides[tool.name]
                updates: dict = {}
                if ov.get("name"):
                    updates["name"] = ov["name"]
                if ov.get("description"):
                    updates["description"] = ov["description"]
                if updates:
                    tool = tool.model_copy(update=updates)
            result.append(tool)
        return result

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        if name in self._reverse:
            context.message.name = self._reverse[name]
        return await call_next(context)
