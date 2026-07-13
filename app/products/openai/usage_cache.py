"""Prompt-cache helpers for xAI usage fields and sticky keys."""

from __future__ import annotations

import hashlib
from typing import Any


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def extract_prompt_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    return _as_int(usage.get("input_tokens") or usage.get("prompt_tokens"))


def extract_completion_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    return _as_int(usage.get("output_tokens") or usage.get("completion_tokens"))


def extract_cached_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return _as_int(details.get("cached_tokens"))
    return _as_int(usage.get("cached_tokens"))


def openai_usage_from_upstream(usage: dict[str, Any] | None) -> dict[str, Any]:
    prompt = extract_prompt_tokens(usage)
    completion = extract_completion_tokens(usage)
    cached = extract_cached_tokens(usage)
    total = _as_int((usage or {}).get("total_tokens")) or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {
            "cached_tokens": cached,
            "text_tokens": prompt,
            "audio_tokens": 0,
            "image_tokens": 0,
        },
        "completion_tokens_details": {
            "text_tokens": completion,
            "audio_tokens": 0,
            "reasoning_tokens": _as_int(
                ((usage or {}).get("output_tokens_details") or {}).get("reasoning_tokens")
                if isinstance((usage or {}).get("output_tokens_details"), dict)
                else 0
            ),
        },
    }


def sticky_key_from_headers(headers: Any) -> str:
    if headers is None:
        return ""
    get = getattr(headers, "get", None)
    if not callable(get):
        return ""
    for name in (
        "x-grok-conv-id",
        "X-Grok-Conv-Id",
        "x-conversation-id",
        "X-Conversation-Id",
    ):
        value = str(get(name) or "").strip()
        if value:
            return value[:200]
    return ""


def sticky_key_from_chat_messages(model: str, messages: list[dict[str, Any]] | None) -> str:
    """Stable multi-turn key from model + seed turns (system + first user).

    Using the conversation seed keeps sticky account affinity stable as the
    dialogue grows, which is what prompt-cache routing needs.
    """
    rows = list(messages or [])
    if not rows:
        return ""
    system = next((m for m in rows if str(m.get("role") or "") == "system"), None)
    first_user = next((m for m in rows if str(m.get("role") or "") == "user"), rows[0])
    seed = [m for m in (system, first_user) if m is not None]
    digest = hashlib.sha256(
        f"{model}|{seed!r}".encode("utf-8", "ignore")
    ).hexdigest()
    return f"chat:{digest[:32]}"


def sticky_key_from_responses(payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    for key in ("prompt_cache_key", "user"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:200]
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        for key in ("conversation_id", "session_id", "conv_id", "prompt_cache_key"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value[:200]
    prev = str(payload.get("previous_response_id") or "").strip()
    if prev:
        return f"prev:{prev[:180]}"
    return ""


__all__ = [
    "extract_prompt_tokens",
    "extract_completion_tokens",
    "extract_cached_tokens",
    "openai_usage_from_upstream",
    "sticky_key_from_headers",
    "sticky_key_from_chat_messages",
    "sticky_key_from_responses",
]
