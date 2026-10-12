"""External watchdog for MetaMcp — detects zombie processes and recovers.

Standalone script with NO MetaMcp imports (pure stdlib). Designed to run
every 2 minutes via Windows Scheduled Task.

Usage:
    python -m metamcp.watchdog             # Run once (health check + recovery)
    python -m metamcp.watchdog --install   # Register Scheduled Task (every 2 min)
    python -m metamcp.watchdog --uninstall # Remove Scheduled Task
    python -m metamcp.watchdog --status    # Show current state
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_PATH = ROOT / "metamcp.pid"
LOG_PATH = ROOT / "metamcp-watchdog.log"
PYTHON_EXE = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHONW_EXE = ROOT / ".venv" / "Scripts" / "pythonw.exe"  # No console window

HEALTH_URL = "http://127.0.0.1:8750/health"
HEALTH_TIMEOUT = 10  # seconds per attempt
HEALTH_RETRIES = 3   # retry health check before declaring zombie
HEALTH_RETRY_DELAY = 5  # seconds between retries
GRACE_PERIOD = 180  # seconds after startup before zombie detection kicks in
TASK_NAME = "MetaMcp Watchdog"

# Logging setup
_log = logging.getLogger("metamcp-watchdog")
_log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=1, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_log.addHandler(_handler)
# Also log to stderr when run interactively
_log.addHandler(logging.StreamHandler())


def read_pid() -> int | None:
    """Read PID from metamcp.pid, or None if missing/invalid."""
    try:
        return int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_port_in_use(port: int = 8750) -> bool:
    """Check if a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID exists (Windows)."""
    try:
        # CREATE_NO_WINDOW = 0x08000000 — prevents console popup every 2 min
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        # tasklist CSV output contains the PID as a quoted string
        return str(pid) in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def check_health() -> bool:
    """HTTP GET /health — returns True if MetaMcp responds with 200."""
    try:
        req = urllib.request.Request(HEALTH_URL)
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            if resp.status == 200:
                data = json.loads(resp.read())
                return data.get("status") == "ok"
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        pass
    return False


def kill_process(pid: int) -> None:
    """Force-kill a process by PID."""
    _log.info(f"Killing PID {pid}")
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, timeout=15,
            creationflags=0x08000000,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.error(f"Failed to kill PID {pid}: {e}")


def kill_all_metamcp() -> None:
    """Kill ALL MetaMcp processes (prevents zombie accumulation).

    Uses tasklist + taskkill to find and kill every python.exe whose command
    line contains '-m metamcp'. This is the nuclear option — ensures no
    orphaned instances survive to spawn duplicate backends.
    """
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "Name='python.exe' and CommandLine like '%-m metamcp%'",
             "get", "ProcessId", "/format:csv"],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000,
        )
        pids = []
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[-1].strip().isdigit():
                pids.append(int(parts[-1].strip()))

        if pids:
            _log.info(f"Found {len(pids)} existing MetaMcp process(es): {pids}")
            for p in pids:
                # /T = kill process tree (includes all child backends)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p)],
                    capture_output=True, timeout=15,
                    creationflags=0x08000000,
                )
            _log.info("Killed all existing MetaMcp processes")
            time.sleep(2)  # Let ports release
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.error(f"Failed to kill existing MetaMcp processes: {e}")


def start_metamcp() -> None:
    """Start MetaMcp in background (no console window)."""
    _log.info(f"Starting MetaMcp: {PYTHON_EXE} -m metamcp")
    try:
        # CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [str(PYTHON_EXE), "-m", "metamcp"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except OSError as e:
        _log.error(f"Failed to start MetaMcp: {e}")


def run_check() -> None:
    """Main watchdog logic: check health and recover if needed."""
    pid = read_pid()

    if pid is None:
        # Before starting, check if MetaMcp is already responding (PID file
        # may be missing due to crash during startup, but process still alive).
        if check_health():
            _log.info("No PID file but health OK — MetaMcp already running, skipping")
            return
        _log.info("No PID file and not responding — starting MetaMcp")
        if is_port_in_use():
            _log.info("Port 8750 already in use — skipping start")
            return
        start_metamcp()
        return

    if not is_process_alive(pid):
        _log.warning(f"PID {pid} is dead (crash recovery)")
        PID_PATH.unlink(missing_ok=True)
        if is_port_in_use():
            _log.info("Port 8750 already in use — skipping start")
            return
        start_metamcp()
        return

    # Process is alive — check if it's actually serving (with retries)
    for attempt in range(1, HEALTH_RETRIES + 1):
        if check_health():
            _log.info(f"Healthy (PID {pid})")
            return
        if attempt < HEALTH_RETRIES:
            _log.info(
                f"Health check failed (attempt {attempt}/{HEALTH_RETRIES}), "
                f"retrying in {HEALTH_RETRY_DELAY}s..."
            )
            time.sleep(HEALTH_RETRY_DELAY)

    # All retries failed
    _log.warning(f"Health check failed {HEALTH_RETRIES} times (PID {pid})")

    # Check grace period using PID file mtime (more reliable than separate
    # started file which could be overwritten by duplicate instances)
    if PID_PATH.exists():
        age = time.time() - PID_PATH.stat().st_mtime
        if age < GRACE_PERIOD:
            _log.info(
                f"In grace period ({age:.0f}s / {GRACE_PERIOD}s), "
                f"skipping (PID {pid})"
            )
            return

    # Past grace period and all retries failed — zombie detected
    _log.warning(f"ZOMBIE detected: PID {pid} alive but not responding, restarting")
    kill_all_metamcp()
    start_metamcp()


def install_task() -> None:
    """Register Windows Scheduled Task to run watchdog every 2 minutes."""
    # Use pythonw.exe to avoid console window popping up every 2 min
    exe = PYTHONW_EXE if PYTHONW_EXE.exists() else PYTHON_EXE
    # Run the watchdog script directly by path instead of -m (which requires
    # the cwd to be the project root for editable installs to resolve).
    watchdog_script = Path(__file__).resolve()
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{exe}" "{watchdog_script}"',
        "/SC", "MINUTE",
        "/MO", "2",
        "/F",  # Force overwrite if exists
    ]
    _log.info(f"Installing scheduled task: {TASK_NAME}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        _log.info("Scheduled task installed successfully")
    else:
        _log.error(f"Failed to install: {result.stderr.strip()}")
    print(result.stdout.strip() or result.stderr.strip())


def uninstall_task() -> None:
    """Remove the Windows Scheduled Task."""
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    _log.info(f"Removing scheduled task: {TASK_NAME}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        _log.info("Scheduled task removed")
    else:
        _log.error(f"Failed to remove: {result.stderr.strip()}")
    print(result.stdout.strip() or result.stderr.strip())


def show_status() -> None:
    """Display current watchdog status."""
    pid = read_pid()

    print(f"PID file:    {PID_PATH}")
    print(f"  PID:       {pid or 'N/A'}")
    if pid:
        alive = is_process_alive(pid)
        print(f"  Alive:     {alive}")
    if PID_PATH.exists():
        age = time.time() - PID_PATH.stat().st_mtime
        print(f"  Started:   {_fmt_duration(age)} ago")

    healthy = check_health()
    print(f"Health:      {'OK' if healthy else 'UNHEALTHY'}")

    # Check if scheduled task exists
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True,
    )
    print(f"Sched Task:  {'Registered' if result.returncode == 0 else 'Not registered'}")


def _fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def main() -> None:
    parser = argparse.ArgumentParser(description="MetaMcp external watchdog")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="Register Scheduled Task")
    group.add_argument("--uninstall", action="store_true", help="Remove Scheduled Task")
    group.add_argument("--status", action="store_true", help="Show current state")
    args = parser.parse_args()

    if args.install:
        install_task()
    elif args.uninstall:
        uninstall_task()
    elif args.status:
        show_status()
    else:
        run_check()


if __name__ == "__main__":
    main()
