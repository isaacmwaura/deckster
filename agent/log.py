"""Logging setup: rotating file in the data dir plus console.

The agent usually fails on the user's machine when I am not present, so a
persistent log is the primary post-hoc debugging tool.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import data_dir

_configured = False


def get_logger(name: str = "streamcontrol") -> logging.Logger:
    """Return the shared logger, configuring handlers once."""
    global _configured
    logger = logging.getLogger("streamcontrol")
    if not _configured:
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )

        file_handler = RotatingFileHandler(
            data_dir() / "agent.log",
            maxBytes=512 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)

        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)

        logger.addHandler(file_handler)
        logger.addHandler(console)
        _configured = True

    return logger if name == "streamcontrol" else logger.getChild(name)
