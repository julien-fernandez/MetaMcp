"""Tests for metamcp.config — config loading, validation, env-var expansion."""

from __future__ import annotations

import json
import os
import pytest
from pathlib import Path

from metamcp.config import load_config, expand_env_vars, ServerConfig, ToolOverride


# ---------------------------------------------------------------------------
# expand_env_vars
# ---------------------------------------------------------------------------

class TestExpandEnvVars:
    def test_simple_dollar(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret123")
        assert expand_env_vars("$MY_KEY") == "secret123"

    def test_braced_var(self, monkeypatch):
        monkeypatch.setenv("BRACED", "value")
        assert expand_env_vars("${BRACED}") == "value"

    def test_missing_var_becomes_empty(self):
        assert expand_env_vars("$TOTALLY_MISSING_VAR_XYZ") == ""

    def test_mixed_text_and_vars(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "5432")
        assert expand_env_vars("postgres://$HOST:${PORT}/db") == "postgres://localhost:5432/db"

    def test_no_vars_passthrough(self):
        assert expand_env_vars("plain text") == "plain text"


# ---------------------------------------------------------------------------
# load_config — valid
# ---------------------------------------------------------------------------

class TestLoadConfigValid:
    def test_basic_load(self, tmp_servers_json: Path):
        cfg = load_config(tmp_servers_json)
        assert cfg.port == 9999
        assert "echo" in cfg.servers
        assert cfg.servers["echo"].command == "python"
        assert cfg.servers["echo"].args == ["-c", "print('hello')"]
        assert cfg.servers["echo"].env == {"FOO": "bar"}

    def test_defaults(self, tmp_path: Path):
        raw = {"port": 8000, "servers": {"s": {"command": "node"}}}
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        cfg = load_config(p)
        s = cfg.servers["s"]
        assert s.args == []
        assert s.env is None
        assert s.exposed_tools is None
        assert s.hidden_tools is None
        assert s.tool_overrides is None
        assert s.lazy is False
        assert s.request_timeout == 60_000
        assert cfg.panic_if_invalid is False

    def test_env_expansion_in_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "s3cr3t")
        raw = {
            "port": 8000,
            "servers": {
                "s": {
                    "command": "python",
                    "env": {"API_KEY": "$SECRET_KEY"},
                }
            },
        }
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        cfg = load_config(p)
        assert cfg.servers["s"].env["API_KEY"] == "s3cr3t"

    def test_tool_overrides_parsed(self, tmp_path: Path):
        raw = {
            "port": 8000,
            "servers": {
                "s": {
                    "command": "node",
                    "toolOverrides": {
                        "search": {"description": "New desc"},
                        "read": {"name": "gread", "description": "Google read"},
                    },
                }
            },
        }
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        cfg = load_config(p)
        ov = cfg.servers["s"].tool_overrides
        assert ov is not None
        assert ov["search"].name is None
        assert ov["search"].description == "New desc"
        assert ov["read"].name == "gread"

    def test_panic_if_invalid(self, tmp_path: Path):
        raw = {"port": 8000, "panicIfInvalid": True, "servers": {"s": {"command": "x"}}}
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        cfg = load_config(p)
        assert cfg.panic_if_invalid is True


# ---------------------------------------------------------------------------
# load_config — validation errors
# ---------------------------------------------------------------------------

class TestLoadConfigInvalid:
    def test_missing_port(self, tmp_path: Path):
        p = tmp_path / "servers.json"
        p.write_text(json.dumps({"servers": {}}))
        with pytest.raises(ValueError, match="port"):
            load_config(p)

    def test_missing_command_and_url(self, tmp_path: Path):
        raw = {"port": 8000, "servers": {"bad": {}}}
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="needs 'command' or 'url'"):
            load_config(p)

    def test_both_command_and_url(self, tmp_path: Path):
        raw = {"port": 8000, "servers": {"bad": {"command": "x", "url": "http://localhost"}}}
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="cannot have both"):
            load_config(p)

    def test_url_mode_load(self, tmp_path: Path):
        raw = {"port": 8000, "servers": {"http-backend": {"url": "http://127.0.0.1:3847/mcp"}}}
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        cfg = load_config(p)
        assert cfg.servers["http-backend"].is_http
        assert cfg.servers["http-backend"].url == "http://127.0.0.1:3847/mcp"
        assert cfg.servers["http-backend"].command is None

    def test_both_exposed_and_hidden(self, tmp_path: Path):
        raw = {
            "port": 8000,
            "servers": {
                "bad": {
                    "command": "x",
                    "exposedTools": ["a"],
                    "hiddenTools": ["b"],
                }
            },
        }
        p = tmp_path / "servers.json"
        p.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="cannot have both"):
            load_config(p)

    def test_invalid_json(self, tmp_path: Path):
        p = tmp_path / "servers.json"
        p.write_text("not json!")
        with pytest.raises(Exception):
            load_config(p)
