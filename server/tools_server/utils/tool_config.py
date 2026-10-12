"""Shared config loading for tools.

Each tool keeps a config.yaml next to its source file.  This helper
deduplicates the identical loading logic that was repeated in every tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_tool_config(caller_file: str) -> dict[str, Any]:
    """Load config.yaml from the same directory as *caller_file*.

    Usage inside a tool module::

        config = load_tool_config(__file__)
    """
    path = Path(caller_file).parent / "config.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}
