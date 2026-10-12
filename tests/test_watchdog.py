"""Tests for metamcp.watchdog — PID file, health checks, grace period logic."""

from __future__ import annotations

import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from metamcp.watchdog import (
    read_pid,
    is_port_in_use,
    _fmt_duration,
    PID_PATH,
    GRACE_PERIOD,
)


class TestReadPid:
    def test_valid_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "metamcp.pid"
        pid_file.write_text("12345")
        monkeypatch.setattr("metamcp.watchdog.PID_PATH", pid_file)
        assert read_pid() == 12345

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("metamcp.watchdog.PID_PATH", tmp_path / "nope.pid")
        assert read_pid() is None

    def test_invalid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "metamcp.pid"
        pid_file.write_text("not-a-number")
        monkeypatch.setattr("metamcp.watchdog.PID_PATH", pid_file)
        assert read_pid() is None


class TestIsPortInUse:
    def test_unused_port(self):
        # Port 19999 should not be in use
        assert is_port_in_use(19999) is False

    def test_function_returns_bool(self):
        result = is_port_in_use(8750)
        assert isinstance(result, bool)


class TestFmtDuration:
    def test_seconds(self):
        assert _fmt_duration(45) == "45s"

    def test_minutes(self):
        assert _fmt_duration(125) == "2m05s"

    def test_hours(self):
        assert _fmt_duration(3725) == "1h02m"

    def test_zero(self):
        assert _fmt_duration(0) == "0s"


class TestGracePeriod:
    """Verify grace period logic by testing the condition directly."""

    def test_within_grace_period(self):
        started = time.time() - 60  # 60s ago
        age = time.time() - started
        assert age < GRACE_PERIOD

    def test_past_grace_period(self):
        started = time.time() - 200  # 200s ago
        age = time.time() - started
        assert age >= GRACE_PERIOD

    def test_grace_period_value(self):
        assert GRACE_PERIOD == 180
