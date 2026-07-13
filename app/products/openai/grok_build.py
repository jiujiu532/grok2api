"""OpenAI adapters for Grok Build OAuth accounts."""

import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

import orjson

from app.control.account.oauth import GrokOAuthService
from app.dataplane.reverse.protocol.xai_oauth import (
    API_RESPONSES_URL,
    BUILD_RESPONSES_URL,
    request_oauth_responses,
    stream_oauth_responses,
)
from app.platform.errors import RateLimitError, UpstreamError

_MAX_ACCOUNT_ATTEMPTS = 3


def _retryable(status: int) -> bool:
    return status in {401, 402, 408, 429, 500, 502, 503, 504}


def _responses_url(lease, model: str) -> str:
    if model in lease.build_models:
        return BUILD_RESPONSES_URL
    if model in lease.language_models or model in lease.api_models:
        return API_RESPONSES_URL
    raise UpstreamError(f"OAuth 账户不支持模型 {model!r}", status=404)


def _upstream_payload(payload: dict[str, Any], url: str) -> dict[str, Any]:
    if url == API_RESPONSES_URL and "reasoning" in payload:
        return {key: value for key, value in payload.items() if key != "reasoning"}
    return payload


async def _json_with_failover(
    service: GrokOAuthService,
    payload: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    excluded: set[str] = set()
    last_error: UpstreamError | None = None
    model = str(payload.get("model") or "")
    for _ in range(_MAX_ACCOUNT_ATTEMPTS):
        try:
            lease = await service.acquire(model=model, exclude=excluded)
        except RateLimitError:
            if last_error is not None:
                raise last_error
            raise
        refreshed = False
        try:
            while True:
                try:
                    url = _responses_url(lease, model)
                    result = await request_oauth_responses(
                        lease.access_token,
                        _upstream_payload(payload, url),
                        url=url,
                        timeout_s=timeout_s,
                    )
                    await service.success(lease)
                    return result
                except UpstreamError as exc:
                    last_error = exc
                    if exc.status == 401 and not refreshed:
                        try:
                            lease.access_token = await service.access_token(
                                lease.account_id,
                                force_refresh=True,
                            )
                        except UpstreamError as refresh_exc:
                            last_error = refresh_exc
                            break
                        refreshed = True
                        continue
                    if exc.status == 401:
                        await service.expire(lease.account_id, "oauth_upstream_unauthorized")
                    else:
                        await service.failure(lease, status=exc.status)
                    if not _retryable(exc.status):
                        raise
                    break
        finally:
            excluded.add(lease.account_id)
            await service.release(lease)
    if last_error is not None:
        raise last_error
    raise UpstreamError("没有可用的 OAuth 账户", status=503)


async def _stream_with_failover(
    service: GrokOAuthService,
    payload: dict[str, Any],
    *,
    timeout_s: float,
) -> AsyncGenerator[str, None]:
    excluded: set[str] = set()
    last_error: UpstreamError | None = None
    model = str(payload.get("model") or "")
    for _ in range(_MAX_ACCOUNT_ATTEMPTS):
        try:
            lease = await service.acquire(model=model, exclude=excluded)
        except RateLimitError:
            if last_error is not None:
                raise last_error
            raise
        refreshed = False
        sent = False
        try:
            while True:
                try:
                    url = _responses_url(lease, model)
                    async for line in stream_oauth_responses(
                        lease.access_token,
                        _upstream_payload(payload, url),
                        url=url,
                        timeout_s=timeout_s,
                    ):
                        sent = True
                        yield line
                    await service.success(lease)
                    return
                except UpstreamError as exc:
                    last_error = exc
                    if exc.status == 401 and not refreshed and not sent:
                        try:
                            lease.access_token = await service.access_token(
                                lease.account_id,
                                force_refresh=True,
                            )
                        except UpstreamError as refresh_exc:
                            last_error = refresh_exc
                            break
                        refreshed = True
                        continue
                    if exc.status == 401:
                        await service.expire(lease.account_id, "oauth_upstream_unauthorized")
                    else:
                        await service.failure(lease, status=exc.status)
                    if sent or not _retryable(exc.status):
                        raise
                    break
        finally:
            excluded.add(lease.account_id)
            await service.release(lease)
    if last_error is not None:
        raise last_error
    raise UpstreamError("没有可用的 OAuth 账户", status=503)


def _responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "model",
        "input",
        "instructions",
        "stream",
        "reasoning",
        "temperature",
        "top_p",
        "max_output_tokens",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "include",
        "previous_response_id",
        "metadata",
        "truncation",
    }
    body = {key: value for key, value in payload.items() if key in allowed and value is not None}
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") == "minimal":
        body["reasoning"] = {**reasoning, "effort": "low"}
    return body


async def responses(
    service: GrokOAuthService,
    payload: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any] | AsyncGenerator[str, None]:
    body = _responses_payload(payload)
    if body.get("stream"):
        async def _stream() -> AsyncGenerator[str, None]:
            async for line in _stream_with_failover(service, body, timeout_s=timeout_s):
                yield f"{line}\n"

        return _stream()
    return await _json_with_failover(service, body, timeout_s=timeout_s)


def _chat_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content or ""
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind in {"text", "input_text"}:
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind in {"image_url", "input_image"}:
            image = part.get("image_url") or part.get("url") or ""
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                parts.append({"type": "input_image", "image_url": str(url)})
    return parts


def _chat_to_responses(
    *,
    model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    temperature: float | None,
    top_p: float | None,
    reasoning_effort: str | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role in {"system", "developer"}:
            if content:
                instructions.append(str(content))
            continue
        if role == "assistant":
            if content:
                input_items.append(
                    {"type": "message", "role": "assistant", "content": _chat_content(content)}
                )
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                call_id = str(call.get("id") or f"call_{uuid.uuid4().hex[:16]}")
                input_items.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": str(fn["name"]),
                        "arguments": str(fn.get("arguments") or "{}"),
                    }
                )
            continue
        if role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(content or ""),
                }
            )
            continue
        input_items.append(
            {"type": "message", "role": role, "content": _chat_content(content)}
        )

    body: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": stream,
    }
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    if tools:
        body["tools"] = [
            {
                "type": "function",
                **(tool.get("function") or {}),
            }
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            else tool
            for tool in tools
        ]
    if tool_choice is not None:
        if isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
            body["tool_choice"] = {
                "type": "function",
                "name": str(tool_choice["function"].get("name") or ""),
            }
        else:
            body["tool_choice"] = tool_choice
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if max_tokens is not None:
        body["max_output_tokens"] = max_tokens
    if reasoning_effort:
        body["reasoning"] = {
            "effort": "low" if reasoning_effort in {"none", "minimal"} else reasoning_effort
        }
    return body


def _message_from_response(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
    text: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if response.get("output_text"):
        text.append(str(response["output_text"]))
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"}:
                    text.append(str(part.get("text") or ""))
        elif kind == "reasoning":
            for summary in item.get("summary") or []:
                if isinstance(summary, dict) and summary.get("text"):
                    reasoning.append(str(summary["text"]))
        elif kind in {"function_call", "tool_call"}:
            call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:16]}")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text) if text else (None if tool_calls else ""),
    }
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message, "tool_calls" if tool_calls else "stop"


def _chat_response(response: dict[str, Any], model: str) -> dict[str, Any]:
    message, finish_reason = _message_from_response(response)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return {
        "id": str(response.get("id") or f"chatcmpl_{uuid.uuid4().hex[:24]}"),
        "object": "chat.completion",
        "created": int(response.get("created_at") or time.time()),
        "model": str(response.get("model") or model),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
        },
    }


async def _events(lines: AsyncIterable[str]) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    event = ""
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                raw = "\n".join(data)
                if raw != "[DONE]":
                    try:
                        payload = orjson.loads(raw)
                    except orjson.JSONDecodeError:
                        payload = {}
                    if isinstance(payload, dict):
                        yield event or str(payload.get("type") or ""), payload
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if data:
        try:
            payload = orjson.loads("\n".join(data))
        except orjson.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            yield event or str(payload.get("type") or ""), payload


async def _chat_stream(
    service: GrokOAuthService,
    body: dict[str, Any],
    *,
    model: str,
    timeout_s: float,
    emit_think: bool,
) -> AsyncGenerator[str, None]:
    response_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    role_sent = False
    completed = False
    function_items: dict[str, tuple[int, str]] = {}
    tool_count = 0
    usage: dict[str, Any] = {}

    async for event, payload in _events(
        _stream_with_failover(service, body, timeout_s=timeout_s)
    ):
        if event == "response.created":
            response = payload.get("response")
            if isinstance(response, dict):
                response_id = str(response.get("id") or response_id)
                created = int(response.get("created_at") or created)
        if not role_sent and event.startswith("response."):
            role_sent = True
            yield _chat_chunk(
                response_id,
                model,
                created,
                {"role": "assistant", "content": ""},
            )
        if event == "response.output_text.delta":
            yield _chat_chunk(
                response_id,
                model,
                created,
                {"content": str(payload.get("delta") or "")},
            )
        elif event in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        } and emit_think:
            yield _chat_chunk(
                response_id,
                model,
                created,
                {"reasoning_content": str(payload.get("delta") or "")},
            )
        elif event == "response.output_item.added":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                item_id = str(item.get("id") or item.get("call_id") or "")
                call_id = str(item.get("call_id") or item_id)
                function_items[item_id] = (tool_count, call_id)
                yield _chat_chunk(
                    response_id,
                    model,
                    created,
                    {
                        "tool_calls": [
                            {
                                "index": tool_count,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": str(item.get("name") or ""),
                                    "arguments": "",
                                },
                            }
                        ]
                    },
                )
                tool_count += 1
        elif event == "response.function_call_arguments.delta":
            item_id = str(payload.get("item_id") or "")
            index, call_id = function_items.get(item_id, (0, item_id))
            yield _chat_chunk(
                response_id,
                model,
                created,
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "arguments": str(payload.get("delta") or ""),
                            },
                        }
                    ]
                },
            )
        elif event == "response.completed":
            response = payload.get("response")
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                usage = response["usage"]
            completed = True
            yield _chat_chunk(
                response_id,
                model,
                created,
                {},
                finish_reason="tool_calls" if tool_count else "stop",
                usage=usage,
            )
        elif event in {"response.failed", "response.incomplete"}:
            raise UpstreamError(f"Grok Build {event}", status=502, body=orjson.dumps(payload).decode())

    if not completed:
        yield _chat_chunk(
            response_id,
            model,
            created,
            {},
            finish_reason="tool_calls" if tool_count else "stop",
            usage=usage,
        )
    yield "data: [DONE]\n\n"


def _chat_chunk(
    response_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    chunk: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        chunk["usage"] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": int(usage.get("total_tokens") or prompt + completion),
        }
    return f"data: {orjson.dumps(chunk).decode()}\n\n"


async def chat_completions(
    service: GrokOAuthService,
    *,
    model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    emit_think: bool,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    temperature: float | None,
    top_p: float | None,
    reasoning_effort: str | None,
    max_tokens: int | None,
    timeout_s: float,
) -> dict[str, Any] | AsyncGenerator[str, None]:
    body = _chat_to_responses(
        model=model,
        messages=messages,
        stream=stream,
        tools=tools,
        tool_choice=tool_choice,
        temperature=temperature,
        top_p=top_p,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
    )
    if stream:
        return _chat_stream(
            service,
            body,
            model=model,
            timeout_s=timeout_s,
            emit_think=emit_think,
        )
    return _chat_response(
        await _json_with_failover(service, body, timeout_s=timeout_s),
        model,
    )


__all__ = ["responses", "chat_completions"]
