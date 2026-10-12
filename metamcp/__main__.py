"""Entry point: ``python -m metamcp``."""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_PATH = ROOT / "metamcp.pid"


def _is_port_in_use(port: int = 8750) -> bool:
    """Check if a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID exists (Windows)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        return str(pid) in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def _write_pid() -> None:
    PID_PATH.write_text(str(os.getpid()))


def _remove_pid() -> None:
    # Only remove if the PID file contains OUR pid (another instance may
    # have overwritten it before crashing, leaving the file for us).
    try:
        if PID_PATH.read_text().strip() == str(os.getpid()):
            PID_PATH.unlink(missing_ok=True)
    except (FileNotFoundError, ValueError):
        pass


def main() -> None:
    # Abort early if another instance is already on the port
    if _is_port_in_use():
        print("MetaMcp already running on port 8750, exiting.")
        sys.exit(0)

    # Abort if another instance owns the PID file and is alive (covers the
    # warm-up window where the port isn't bound yet but a process is starting)
    try:
        existing_pid = int(PID_PATH.read_text().strip())
        if existing_pid != os.getpid() and _is_pid_alive(existing_pid):
            print(f"MetaMcp already starting (PID {existing_pid}), exiting.")
            sys.exit(0)
    except (FileNotFoundError, ValueError):
        pass

    # Write PID immediately so watchdog knows we exist during warm-up
    _write_pid()
    atexit.register(_remove_pid)

    # Load .env if present (before anything else touches os.environ)
    env_path = ROOT / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)
        except ImportError:
            # python-dotenv not installed — skip silently
            pass

    from .config import load_config
    from .app import build_app

    cfg = load_config()
    app = build_app(cfg)

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=cfg.port,
        log_level="warning",  # We have our own logger
        access_log=False,
    )


if __name__ == "__main__":
    main()
