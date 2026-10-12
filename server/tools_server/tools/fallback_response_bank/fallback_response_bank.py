import json
import logging
import random
import time
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_DEFAULT_BANK: dict[str, list[str]] = {
    "generic": [
        "I did not catch that. Could you repeat it?",
        "I can help, but I need you to say that again.",
        "Please try that one more time.",
    ],
    "network": [
        "Network seems slow right now. Try again in a moment.",
        "I am having a connectivity issue. Please retry shortly.",
    ],
    "tools": [
        "I could not complete that action right now.",
        "That tool call did not finish. Want me to try again?",
    ],
    "stt": [
        "I could not hear that clearly. Please speak a bit louder.",
        "Audio was unclear. Could you repeat that?",
    ],
    "tts": [
        "I have the answer but cannot speak it right now.",
        "Speech output is unavailable at the moment.",
    ],
    "llm": [
        "I am having trouble generating a response right now.",
        "I need a moment to recover before answering.",
    ],
}


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _state_path(config: dict[str, Any]) -> Path:
    raw = str(config.get("state_file") or "/ai/server/tools_server/state/fallback_response_bank.json")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return dict(_DEFAULT_BANK)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_BANK)
    if not isinstance(data, dict):
        return dict(_DEFAULT_BANK)
    out: dict[str, list[str]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        texts = [str(v).strip() for v in value if isinstance(v, str) and str(v).strip()]
        out[key.strip().lower()] = texts
    return out if out else dict(_DEFAULT_BANK)


def _save_state(path: Path, state: dict[str, list[str]]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True)


def register(server) -> List[str]:
    log = logging.getLogger("tools.fallback_response_bank")
    config = _load_config()
    state_file = _state_path(config)

    tool_description, param_meta = get_tool_config(
        config,
        "fallback_response_bank",
        "Manage fallback response templates by category.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: get, list, upsert, delete, reset_defaults, clear.",
    )
    category_desc, category_alias, category_title = get_param_meta(
        param_meta,
        "category",
        "Fallback category.",
    )
    text_desc, text_alias, text_title = get_param_meta(
        param_meta,
        "text",
        "Fallback response text for upsert.",
    )
    index_desc, index_alias, index_title = get_param_meta(
        param_meta,
        "index",
        "Optional zero-based index for get/delete.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(name="fallback_response_bank", description=tool_description)
    def fallback_response_bank(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "get",
        category: Annotated[str | None, Field(**_field_kwargs(category_desc, category_alias, category_title))] = None,
        text: Annotated[str | None, Field(**_field_kwargs(text_desc, text_alias, text_title))] = None,
        index: Annotated[int | None, Field(**_field_kwargs(index_desc, index_alias, index_title))] = None,
    ) -> str:
        log_tool_call(log, "fallback_response_bank", action=action, category=category, index=index)
        state = _load_state(state_file)
        action_value = str(action or "get").strip().lower()
        category_value = str(category or "generic").strip().lower()

        if action_value == "reset_defaults":
            _save_state(state_file, dict(_DEFAULT_BANK))
            return log_tool_result(log, "fallback_response_bank", _as_json({"status": "ok", "reset": True}))

        if action_value == "clear":
            _save_state(state_file, {})
            return log_tool_result(log, "fallback_response_bank", _as_json({"status": "ok", "cleared": True}))

        if action_value == "upsert":
            phrase = str(text or "").strip()
            if not phrase:
                return log_tool_result(log, "fallback_response_bank", _as_json({"status": "error", "message": "text required"}))
            lines = state.get(category_value) if isinstance(state.get(category_value), list) else []
            if phrase not in lines:
                lines.append(phrase)
            state[category_value] = lines
            _save_state(state_file, state)
            return log_tool_result(
                log,
                "fallback_response_bank",
                _as_json({"status": "ok", "category": category_value, "count": len(lines)}),
            )

        if action_value == "delete":
            lines = state.get(category_value) if isinstance(state.get(category_value), list) else []
            if index is None:
                removed = bool(state.pop(category_value, None) is not None)
                _save_state(state_file, state)
                return log_tool_result(
                    log,
                    "fallback_response_bank",
                    _as_json({"status": "ok", "category": category_value, "deleted_category": removed}),
                )
            idx = int(index)
            if idx < 0 or idx >= len(lines):
                return log_tool_result(
                    log,
                    "fallback_response_bank",
                    _as_json({"status": "error", "message": "index out of range"}),
                )
            removed_text = lines.pop(idx)
            if lines:
                state[category_value] = lines
            else:
                state.pop(category_value, None)
            _save_state(state_file, state)
            return log_tool_result(
                log,
                "fallback_response_bank",
                _as_json({"status": "ok", "category": category_value, "removed": removed_text}),
            )

        if action_value == "list":
            categories = sorted(state.keys())
            return log_tool_result(
                log,
                "fallback_response_bank",
                _as_json(
                    {
                        "status": "ok",
                        "categories": categories,
                        "counts": {name: len(state.get(name) or []) for name in categories},
                    }
                ),
            )

        if action_value != "get":
            return log_tool_result(
                log,
                "fallback_response_bank",
                _as_json({"status": "error", "message": "unsupported action: use get,list,upsert,delete,reset_defaults,clear"}),
            )

        lines = state.get(category_value) if isinstance(state.get(category_value), list) else []
        if not lines and category_value != "generic":
            lines = state.get("generic") if isinstance(state.get("generic"), list) else []
            category_value = "generic"
        if not lines:
            lines = _DEFAULT_BANK["generic"]
            category_value = "generic"

        if index is not None:
            idx = int(index)
            if idx < 0 or idx >= len(lines):
                return log_tool_result(
                    log,
                    "fallback_response_bank",
                    _as_json({"status": "error", "message": "index out of range"}),
                )
            selected = lines[idx]
        else:
            random.seed(int(time.time()) // 2)
            selected = random.choice(lines)

        return log_tool_result(
            log,
            "fallback_response_bank",
            _as_json({"status": "ok", "category": category_value, "response": selected, "count": len(lines)}),
        )

    return ["fallback_response_bank"]

