"""Lightweight logging for Prowl.

Everything Prowl hears, decides, and does is appended to a daily log under
``~/.prowl/logs`` so actions (especially destructive ones) are auditable.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, ensure_home

_LOGGER: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    ensure_home()
    logger = logging.getLogger("prowl")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

        file_handler = RotatingFileHandler(
            LOG_DIR / "prowl.log", maxBytes=2_000_000, backupCount=5
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("prowl: %(message)s"))
        stream.setLevel(logging.WARNING)  # keep the console quiet by default
        logger.addHandler(stream)

    _LOGGER = logger
    return logger
