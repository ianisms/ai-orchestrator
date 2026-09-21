import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from typing import Any

class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level

_CONFIGURED: dict[str, object] = {"key": None}

def _parse_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        cleaned = level.strip().upper()
        if cleaned.isdigit():
            return int(cleaned)
        if cleaned == "VERBOSE":
            return logging.DEBUG
        return getattr(logging, cleaned, logging.INFO)
    return logging.INFO

@dataclass(frozen=True)
class LoggingConfig:
    log_dir: str
    level: str | int = "INFO"
    name: str = "orchestrator"
    stdout_level: str | int = "DEBUG"
    stdout_max_level: str | int = "INFO"
    stderr_level: str | int = "WARNING"
    file_level: str | int = "DEBUG"
    file_name: str | None = None
    backup_count: int = 14

def configure_logging(config: LoggingConfig) -> logging.Logger:
    os.makedirs(config.log_dir, exist_ok=True)

    base_level = _parse_level(config.level)
    stdout_level = _parse_level(config.stdout_level)
    stdout_max = _parse_level(config.stdout_max_level)
    stderr_level = _parse_level(config.stderr_level)
    file_level = _parse_level(config.file_level)
    file_name = config.file_name or f"{config.name}.log"

    key = (
        config.log_dir,
        base_level,
        config.name,
        stdout_level,
        stdout_max,
        stderr_level,
        file_level,
        file_name,
        config.backup_count,
    )
    if _CONFIGURED.get("key") == key:
        logger = logging.getLogger(config.name)
        logger.setLevel(base_level)
        logger.propagate = True
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    h_out = logging.StreamHandler(sys.stdout)
    h_out.setLevel(stdout_level)
    h_out.addFilter(_MaxLevelFilter(stdout_max))
    h_out.setFormatter(fmt)

    h_err = logging.StreamHandler(sys.stderr)
    h_err.setLevel(stderr_level)
    h_err.setFormatter(fmt)

    logfile = os.path.join(config.log_dir, file_name)
    h_file = TimedRotatingFileHandler(
        logfile,
        when="midnight",
        interval=1,
        backupCount=config.backup_count,
        utc=False,
        encoding="utf-8",
    )
    h_file.setLevel(file_level)
    h_file.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(base_level)
    root.handlers.clear()
    root.addHandler(h_out)
    root.addHandler(h_err)
    root.addHandler(h_file)

    logger = logging.getLogger(config.name)
    logger.setLevel(base_level)
    logger.handlers.clear()
    logger.propagate = True

    _CONFIGURED["key"] = key
    logger.debug("logging initialized")
    return logger

def setup_logging(log_dir: str, level: str = "INFO", *, name: str = "orchestrator") -> logging.Logger:
    config = LoggingConfig(log_dir=log_dir, level=level, name=name)
    return configure_logging(config)

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.propagate = True
    return logger

def format_kv(
    required: dict[str, Any],
    optional: dict[str, Any] | None = None,
    *,
    enabled: bool = True,
) -> str:
    fields: dict[str, Any] = {}
    fields.update(required)
    if enabled and optional:
        fields.update(optional)

    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")

    if not parts:
        return ""
    return " " + " ".join(parts)
