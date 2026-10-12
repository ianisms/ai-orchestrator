from __future__ import annotations

from typing import Any


class ChatHistory:
    def __init__(self, token_budget: int, *, on_change=None) -> None:
        self._token_budget = max(int(token_budget), 0)
        self._messages: list[dict[str, Any]] = []
        self._on_change = on_change
        self.awaiting_location: bool = False
        self.pending_location_query: str = ""

    def set_pending_location(self, query: str) -> None:
        self.awaiting_location = True
        self.pending_location_query = str(query or "").strip()

    def clear_pending_location(self) -> None:
        self.awaiting_location = False
        self.pending_location_query = ""

    def reset(self) -> None:
        self._messages = []
        self.awaiting_location = False
        self.pending_location_query = ""
        self._notify_change()

    def add_tool_turn(self, user_text: str, tool_messages: list[dict[str, Any]], assistant_text: str) -> None:
        """Store a turn that involved tool calls: user query, tool call/result context, and final assistant response."""
        if user_text:
            self._messages.append({"role": "user", "content": user_text})
        for msg in tool_messages:
            self._messages.append(msg)
        if assistant_text:
            self._messages.append({"role": "assistant", "content": assistant_text})
        self._messages = self._trim_messages(self._messages, self._token_budget)
        self._notify_change()

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        if user_text:
            self._messages.append({"role": "user", "content": user_text})
        if assistant_text:
            self._messages.append({"role": "assistant", "content": assistant_text})
        self._messages = self._trim_messages(self._messages, self._token_budget)
        self._notify_change()

    def build_messages(self, user_text: str) -> list[dict[str, str]]:
        if self._token_budget <= 0:
            return [{"role": "user", "content": user_text}]

        user_tokens = self._estimate_message_tokens(user_text)
        budget = max(self._token_budget - user_tokens, 0)
        history = self._trim_messages(self._messages, budget)
        return history + [{"role": "user", "content": user_text}]

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            self._messages = []
            return
        self._messages = self._trim_messages(messages, self._token_budget)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def set_on_change(self, on_change) -> None:
        self._on_change = on_change

    def set_token_budget(self, token_budget: int) -> None:
        self._token_budget = max(int(token_budget), 0)
        self._messages = self._trim_messages(self._messages, self._token_budget)

    def _notify_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(list(self._messages))
        except Exception:
            pass

    def _estimate_message_tokens(self, text: Any) -> int:
        # Rough heuristic: ~1 token per 4 chars + small role/format overhead.
        if not isinstance(text, str):
            text = ""
        token_est = (len(text) + 3) // 4
        return max(1, token_est) + 4

    def _total_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            total += self._estimate_message_tokens(msg.get("content", ""))
        return total

    def _trim_messages(self, messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        if budget <= 0 or not messages:
            return []

        # Walk backwards, accumulating messages that fit in budget.
        kept_rev: list[dict[str, str]] = []
        total = 0
        for msg in reversed(messages):
            t = self._estimate_message_tokens(msg.get("content", ""))
            if total + t > budget:
                break
            kept_rev.append(msg)
            total += t

        kept = list(reversed(kept_rev))

        # Ensure history starts on a user message so the LLM never sees
        # an orphaned assistant/tool response without its originating query.
        while kept and kept[0].get("role") != "user":
            kept = kept[1:]

        return kept
