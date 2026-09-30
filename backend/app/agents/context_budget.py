"""Shared conservative sizing for chat-completion input envelopes."""

from __future__ import annotations

import json


def serialized_chat_input_bytes(user_message: str, system_prompt: str) -> int:
    """Return the exact UTF-8 byte size used by ``BaseAgent``'s input guard."""
    messages_json = json.dumps(
        {"messages": [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": user_message or ""},
        ]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(messages_json.encode("utf-8"))
