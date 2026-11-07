"""
Logging configuration shared by the backend scripts.

Policy: log *counts, file names, table names and error types* only.
Never pass record contents or raw exception messages from the database
engine into a log call - those can echo data values. Use ``safe_error``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import config

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(name: str, *, log_file: str | None = None, level: str | None = None) -> logging.Logger:
    """Configure root logging once (console + optional file in ``logs/``)."""
    root = logging.getLogger()
    if not getattr(root, "_blast_configured", False):
        root.setLevel(level or config.LOG_LEVEL)
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(_FORMAT, "%H:%M:%S"))
        root.addHandler(console)
        if log_file:
            config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(Path(config.LOGS_DIR) / log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(fh)
        root._blast_configured = True  # type: ignore[attr-defined]
    return logging.getLogger(name)


def safe_error(exc: BaseException) -> str:
    """Describe an exception without its message (messages may contain values)."""
    return type(exc).__name__
