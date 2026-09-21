import logging
from typing import Any

from utils.metrics import record_tool_call


def log_tool_result(logger: logging.Logger, tool_name: str, result: Any) -> str:
    text = result if isinstance(result, str) else str(result)
    logger.debug("tool result tool=%s chars=%d result=%s", tool_name, len(text), text)
    return text


def log_tool_call(logger: logging.Logger, tool_name: str, **params: Any) -> None:
    try:
        preview = ", ".join(f"{k}={v!r}" for k, v in params.items())
    except Exception:
        preview = str(params)
    try:
        record_tool_call(tool_name)
    except Exception:
        pass
    logger.info("tool call tool=%s", tool_name)
    logger.debug("tool call args tool=%s %s", tool_name, preview)
