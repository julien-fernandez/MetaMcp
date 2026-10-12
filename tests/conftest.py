"""Shared fixtures for MetaMcp tests."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from metamcp.config import ServerConfig, ToolOverride


@pytest.fixture
def tmp_servers_json(tmp_path: Path) -> Path:
    """Create a minimal servers.json in a temp directory."""
    cfg = {
        "port": 9999,
        "servers": {
            "echo": {
                "command": "python",
                "args": ["-c", "print('hello')"],
                "env": {"FOO": "bar"},
            }
        },
    }
    p = tmp_path / "servers.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


@pytest.fixture
def sample_server_config() -> ServerConfig:
    """A simple ServerConfig for unit tests."""
    return ServerConfig(
        command="python",
        args=["-c", "pass"],
        env={"TEST": "1"},
    )


@pytest.fixture
def server_config_with_filter() -> ServerConfig:
    """ServerConfig with tool filtering."""
    return ServerConfig(
        command="python",
        args=["-c", "pass"],
        hidden_tools=["secret_tool", "debug_internal"],
    )


@pytest.fixture
def server_config_with_overrides() -> ServerConfig:
    """ServerConfig with tool overrides."""
    return ServerConfig(
        command="python",
        args=["-c", "pass"],
        tool_overrides={
            "search": ToolOverride(description="Search Google Drive"),
            "read": ToolOverride(name="google_read", description="Read a Google Doc"),
        },
    )
