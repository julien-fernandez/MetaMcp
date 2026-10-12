"""Logging with file rotation (2 MB, 1 backup) + console output."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "metamcp.log"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("metamcp")
    if logger.handlers:
        return logger  # Already configured (e.g. after hot-reload)

    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s.%(msecs)03d] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console: stdout for INFO/WARN, stderr for ERROR/FATAL
    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setLevel(logging.DEBUG)
    stdout_h.addFilter(lambda r: r.levelno < logging.ERROR)
    stdout_h.setFormatter(fmt)
    logger.addHandler(stdout_h)

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setLevel(logging.ERROR)
    stderr_h.setFormatter(fmt)
    logger.addHandler(stderr_h)

    # File with rotation (2 MB, 1 backup)
    try:
        file_h = RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8"
        )
        file_h.setFormatter(fmt)
        logger.addHandler(file_h)
    except Exception:
        pass  # Don't crash if we can't write logs

    return logger


log = setup_logger()
