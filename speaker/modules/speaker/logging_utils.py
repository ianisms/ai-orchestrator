from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Optional


def setup_logger(
    name: str = "speaker",
    log_path: Optional[str] = "/ai/speaker/logs/client.log",
    level: str = "INFO",
) -> logging.Logger:
    """
    Create a logger with stdout + rotating file (daily) handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    h_out = logging.StreamHandler(sys.stdout)
    h_out.setLevel(logging.DEBUG)
    h_out.setFormatter(fmt)
    logger.addHandler(h_out)

    if log_path:
        d = os.path.dirname(log_path)
        if d:
            os.makedirs(d, exist_ok=True)
        h_file = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=14,
            utc=False,
            encoding="utf-8",
        )
        h_file.setLevel(logging.DEBUG)
        h_file.setFormatter(fmt)
        logger.addHandler(h_file)

    logger.debug("logger initialized")
    return logger
