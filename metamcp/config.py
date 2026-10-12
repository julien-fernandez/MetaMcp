"""Load, validate & expand env vars in servers.json — port of src/config.ts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Z_][A-Z0-9_]*)", re.IGNORECASE)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "servers.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolOverride:
    name: str | None = None
    description: str | None = None


@dataclass
class ServerConfig:
    command: str | None = None       # stdio mode: executable to spawn
    url: str | None = None           # http mode: upstream URL to proxy to
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    exposed_tools: list[str] | None = None
    hidden_tools: list[str] | None = None
    tool_overrides: dict[str, ToolOverride] | None = None
    lazy: bool = False
    request_timeout: int = 60_000  # ms

    @property
    def is_http(self) -> bool:
        return self.url is not None


@dataclass
class Config:
    port: int = 8750
    panic_if_invalid: bool = False
    servers: dict[str, ServerConfig] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Env-var expansion
# ---------------------------------------------------------------------------

def expand_env_vars(value: str) -> str:
    """Replace $VAR and ${VAR} patterns with os.environ values."""
    def _replace(m: re.Match) -> str:
        var = m.group(1) or m.group(2)
        return os.environ.get(var, "")
    return _ENV_VAR_RE.sub(_replace, value)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load servers.json, validate, expand env vars. Same logic as config.ts."""
    raw = json.loads(path.read_text(encoding="utf-8"))

    port = raw.get("port")
    if not isinstance(port, int):
        raise ValueError("servers.json: 'port' must be a number")

    servers_raw = raw.get("servers")
    if not isinstance(servers_raw, dict):
        raise ValueError("servers.json: 'servers' must be an object")

    servers: dict[str, ServerConfig] = {}
    for name, srv in servers_raw.items():
        has_command = bool(srv.get("command"))
        has_url = bool(srv.get("url"))
        if not has_command and not has_url:
            raise ValueError(f"servers.json: server '{name}' needs 'command' or 'url'")
        if has_command and has_url:
            raise ValueError(
                f"servers.json: server '{name}' cannot have both 'command' and 'url'"
            )
        if srv.get("exposedTools") and srv.get("hiddenTools"):
            raise ValueError(
                f"servers.json: server '{name}' cannot have both exposedTools and hiddenTools"
            )

        # Parse tool overrides
        tool_overrides: dict[str, ToolOverride] | None = None
        if "toolOverrides" in srv:
            tool_overrides = {}
            for tool_name, override in srv["toolOverrides"].items():
                tool_overrides[tool_name] = ToolOverride(
                    name=override.get("name"),
                    description=override.get("description"),
                )

        sc = ServerConfig(
            command=srv.get("command"),
            url=srv.get("url"),
            args=srv.get("args", []),
            env=srv.get("env"),
            cwd=srv.get("cwd"),
            exposed_tools=srv.get("exposedTools"),
            hidden_tools=srv.get("hiddenTools"),
            tool_overrides=tool_overrides,
            lazy=srv.get("lazy", False),
            request_timeout=srv.get("requestTimeout", 60_000),
        )

        # Expand env vars in command, args, env values, cwd, url
        if sc.command:
            sc.command = expand_env_vars(sc.command)
        sc.args = [expand_env_vars(a) for a in sc.args]
        if sc.env:
            sc.env = {k: expand_env_vars(v) for k, v in sc.env.items()}
        if sc.cwd:
            sc.cwd = expand_env_vars(sc.cwd)
        if sc.url:
            sc.url = expand_env_vars(sc.url)

        servers[name] = sc

    return Config(
        port=port,
        panic_if_invalid=raw.get("panicIfInvalid", False),
        servers=servers,
    )
