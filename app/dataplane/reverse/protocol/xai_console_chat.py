"""XAI console.x.ai chat protocol — payload builder and SSE stream adapter.

端点: POST https://console.x.ai/v1/responses
认证: Authorization: Bearer anonymous  +  Cookie: sso=<token>; sso-rw=<token>

请求格式 (OpenAI Responses API):
{
    "model": "grok-4.3",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "..."}]}],
    "max_output_tokens": 1000000,
    "temperature": 0.7,
    "top_p": 0.95,
    "reasoning": {"effort": "low"},
    "store": false,
    "include": ["reasoning.encrypted_content"],
    "stream": true
}

响应 SSE 事件类型:
- response.created / response.in_progress  — 忽略
- response.output_item.added               — 忽略
- response.output_item.done                — reasoning item，含 encrypted_content（不可读）
- response.content_part.added             — 忽略
- response.output_text.delta              — 文本 token，delta 字段
- response.output_text.done              — 忽略
- response.content_part.done             — 忽略
- response.output_item.done (message)    — 忽略
- response.completed                      — 含 usage 统计
"""

from typing import Any, AsyncGenerator

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall


# ---------------------------------------------------------------------------
# 支持的模型名 → console.x.ai 实际 model 字段映射
# ---------------------------------------------------------------------------

# console.x.ai 上可用的模型（通过 grok.com SSO 免费访问）
# key = grok2api 对外暴露的模型名，value = console.x.ai 实际 model 字段
CONSOLE_MODELS: dict[str, str] = {
    "grok-4.3-console":                     "grok-4.3",
    "grok-4.3-low":                         "grok-4.3",
    "grok-4.3-medium":                      "grok-4.3",
    "grok-4.3-high":                        "grok-4.3",
    "grok-4.20-0309-reasoning-console":     "grok-4.20-0309-reasoning",
    "grok-4.20-0309-console":               "grok-4.20-0309",
    "grok-4.20-0309-non-reasoning-console": "grok-4.20-0309-non-reasoning",
    "grok-4.20-multi-agent-console":        "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-low":            "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-medium":         "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-high":           "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-xhigh":          "grok-4.20-multi-agent-0309",
    "grok-build-console":                   "grok-build-0.1",
}

# 需要附带 reasoning 字段的模型（grok-4.3 系列需要，grok-4.20 系列不需要）
_MODELS_WITH_REASONING_FIELD: frozenset[str] = frozenset({
    "grok-4.3",
    "grok-4.20-multi-agent-0309",
})

# 模型名后缀 → 固定 effort 值（优先级高于用户传入的 reasoning_effort）
_MODEL_FIXED_EFFORT: dict[str, str] = {
    "grok-4.3-low":    "low",
    "grok-4.3-medium": "medium",
    "grok-4.3-high":   "high",
    "grok-4.20-multi-agent-low":    "low",
    "grok-4.20-multi-agent-medium": "medium",
    "grok-4.20-multi-agent-high":   "high",
    "grok-4.20-multi-agent-xhigh":  "xhigh",
}

# 特殊 max_output_tokens（默认 1_000_000）
_MODEL_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "grok-4.20-multi-agent-0309": 2_000_000,
    "grok-build-0.1": 256_000,
}

# 支持 web_search / x_search 工具的模型
_MODELS_WITH_SEARCH_TOOLS: frozenset[str] = frozenset({
    "grok-4.20-multi-agent-0309",
    "grok-4.20-0309",
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
    "grok-4.3",
    "grok-build-0.1",
})

# Grok/xAI 内部工具名。它们可能以 function_call/tool card 形式出现在上游流里，
# 但对 OpenAI 客户端必须保持“内部工具”语义，不能转成客户端 tool_calls。
# 参考 xAI Tool Usage Details 的 server-side function names，并保留本项目旧
# grok.com parser 已观测到的 alias。故意不包含泛名 search，避免误伤用户自定义工具。
_CONSOLE_INTERNAL_TOOL_NAMES: frozenset[str] = frozenset({
    # Public tool types / aliases.
    "web_search",
    "x_search",
    "code_interpreter",
    "file_search",
    # SERVER_SIDE_TOOL_WEB_SEARCH function names.
    "web_search_with_snippets",
    "browse_page",
    "open_page",
    "open_page_with_find",
    # SERVER_SIDE_TOOL_IMAGE_SEARCH function names / observed aliases.
    "search_images",
    "image_search",
    "view_image",
    # SERVER_SIDE_TOOL_X_SEARCH function names.
    "x_user_search",
    "x_keyword_search",
    "x_semantic_search",
    "x_thread_fetch",
    "view_x_video",
    # Other server-side/internal helpers.
    "chatroom_send",
    "code_execution",
    "collections_search",
})

# reasoning effort 映射：OpenAI reasoning_effort → console API effort
_EFFORT_MAP: dict[str, str] = {
    "none":    "none",
    "minimal": "low",
    "low":     "low",
    "medium":  "medium",
    "high":    "high",
    "xhigh":   "xhigh",
}


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_console_payload(
    *,
    messages: list[dict[str, Any]],
    model: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    reasoning_effort: str | None = None,
    stream: bool = True,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> dict[str, Any]:
    """Build the JSON payload for POST console.x.ai/v1/responses.

    将 OpenAI messages 格式转换为 Responses API input 格式。
    """
    # 转换 messages → input 数组
    input_items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "tool":
            tool_output = _tool_message_to_console_output(msg)
            if tool_output:
                input_items.append(tool_output)
            continue

        tool_calls = msg.get("tool_calls")

        # 映射 role
        if role in ("system", "developer"):
            # system 消息作为 instructions 字段处理，这里先放入 input
            api_role = "system"
        elif role == "assistant":
            api_role = "assistant"
        else:
            api_role = "user"

        if role == "assistant" and tool_calls and not content:
            content_blocks = []
        else:
            content_blocks = _message_content_blocks(content)

        if content_blocks:
            input_items.append({"role": api_role, "content": content_blocks})

        if role == "assistant" and tool_calls:
            input_items.extend(_assistant_tool_calls_to_console(tool_calls))

    # reasoning effort：模型名固定值优先，其次用户传入，最后默认 medium
    effort = _MODEL_FIXED_EFFORT.get(model) or _EFFORT_MAP.get(reasoning_effort or "medium", "medium")

    # 获取 console 实际模型名
    console_model = CONSOLE_MODELS.get(model, model)

    payload: dict[str, Any] = {
        "model": console_model,
        "input": input_items,
        "max_output_tokens": _MODEL_MAX_OUTPUT_TOKENS.get(console_model, 1_000_000),
        "temperature": temperature,
        "top_p": top_p,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "stream": stream,
    }

    # 只有 grok-4.3 需要附带 reasoning 字段，grok-4.20 系列不需要
    if console_model in _MODELS_WITH_REASONING_FIELD:
        payload["reasoning"] = {"effort": effort}

    user_tools = _to_console_tools(tools or [])
    payload_tools = _merge_console_tools(_default_console_tools(console_model), user_tools)

    if payload_tools:
        payload["tools"] = payload_tools
        payload["tool_choice"] = _to_console_tool_choice(tool_choice) or "auto"

    logger.debug(
        "console payload built: model={} console_model={} input_items={} has_reasoning={} tool_count={}",
        model, console_model, len(input_items), console_model in _MODELS_WITH_REASONING_FIELD,
        len(payload_tools),
    )
    return payload


def _default_console_tools(console_model: str) -> list[dict[str, Any]]:
    """Return console-native tools that must remain available to Grok itself."""
    if console_model not in _MODELS_WITH_SEARCH_TOOLS:
        return []
    return [
        {"type": "web_search", "enable_image_understanding": True},
        {"type": "x_search", "enable_video_understanding": True},
    ]


def _is_console_internal_tool_name(name: str) -> bool:
    return name.strip() in _CONSOLE_INTERNAL_TOOL_NAMES


def _tool_identity(tool: dict[str, Any]) -> tuple[str, str]:
    tool_type = str(tool.get("type") or "").strip()
    if tool_type == "function":
        return (tool_type, str(tool.get("name") or "").strip())
    return (tool_type, "")


def _merge_console_tools(
    default_tools: list[dict[str, Any]],
    user_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge console tools, letting explicit client config override defaults."""
    result: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for tool in default_tools:
        ident = _tool_identity(tool)
        positions[ident] = len(result)
        result.append(tool)
    for tool in user_tools:
        ident = _tool_identity(tool)
        pos = positions.get(ident)
        if pos is None:
            positions[ident] = len(result)
            result.append(tool)
        else:
            result[pos] = tool
    return result


def _to_console_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI Chat/Responses function tools to console Responses shape."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if tool.get("type") != "function":
            converted.append(dict(tool))
            continue

        fn = tool.get("function")
        src = fn if isinstance(fn, dict) else tool
        name = str(src.get("name") or "").strip()
        if not name or _is_console_internal_tool_name(name):
            continue

        item: dict[str, Any] = {
            "type": "function",
            "name": name,
        }
        description = src.get("description")
        if description is not None:
            item["description"] = description
        parameters = src.get("parameters")
        if parameters is not None:
            item["parameters"] = parameters

        # Preserve common strict-schema flags if clients provide them.
        for key in ("strict",):
            if key in src:
                item[key] = src[key]
            elif key in tool:
                item[key] = tool[key]

        converted.append(item)
    return converted


def client_function_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    """Return names of client-declared function tools.

    Console models can emit internal tool events for built-in tools such as
    search, browsing, image viewing, or code execution. Only client function
    tools should become OpenAI tool_calls.
    """
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue
        fn = tool.get("function")
        src = fn if isinstance(fn, dict) else tool
        name = str(src.get("name") or "").strip()
        if name and not _is_console_internal_tool_name(name):
            names.add(name)
    return names


def _to_console_tool_choice(tool_choice: Any) -> Any:
    """Map OpenAI Chat tool_choice to console Responses tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return tool_choice

    choice_type = tool_choice.get("type")
    if choice_type != "function":
        return dict(tool_choice)

    fn = tool_choice.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip()
    else:
        name = str(tool_choice.get("name") or "").strip()
    if not name:
        return dict(tool_choice)
    if _is_console_internal_tool_name(name):
        return "auto"
    return {"type": "function", "name": name}


def _message_content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if isinstance(content, list):
        content_blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                content_blocks.append({"type": "input_text", "text": block.get("text", "")})
            elif btype == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
                if url:
                    content_blocks.append({"type": "input_image", "image_url": url})
            else:
                text = block.get("text") or str(block)
                content_blocks.append({"type": "input_text", "text": text})
        return content_blocks
    return [{"type": "input_text", "text": str(content)}]


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text is None:
                    text = block.get("content")
                parts.append(str(text if text is not None else block))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return str(content)


def _assistant_tool_calls_to_console(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []

    items: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        if tool_call.get("type") not in (None, "function"):
            continue
        fn = tool_call.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
        if not call_id:
            continue
        arguments = fn.get("arguments")
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            arguments = orjson.dumps(arguments).decode()
        items.append({
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        })
    return items


def _tool_message_to_console_output(msg: dict[str, Any]) -> dict[str, Any] | None:
    call_id = str(msg.get("tool_call_id") or msg.get("call_id") or "").strip()
    if not call_id:
        return None
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": _content_to_text(msg.get("content", "")),
    }


# ---------------------------------------------------------------------------
# SSE stream adapter
# ---------------------------------------------------------------------------

class ConsoleStreamAdapter:
    """Parse console.x.ai SSE events and yield text tokens.

    The console Responses stream may include two very different classes of tool
    activity:
    - console-native internal tools such as search, browsing, image viewing, or
      code execution, used by Grok to produce the final answer;
    - client-declared function tools, which must be surfaced to OpenAI clients
      as tool_calls/function_call items.

    Only the second class is exported. Native tool events are ignored without
    affecting text deltas, which prevents the empty-response regression where a
    built-in search call was mistaken for a client function call.
    """

    __slots__ = (
        "text_buf",
        "usage",
        "_done",
        "_function_calls",
        "_function_order",
        "_allowed_function_names",
        "_ignored_function_keys",
        "_function_keys_by_output_index",
    )

    def __init__(
        self,
        function_tool_names: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.text_buf: list[str] = []
        self.usage: dict[str, Any] | None = None
        self._done = False
        self._function_calls: dict[str, dict[str, Any]] = {}
        self._function_order: list[str] = []
        self._allowed_function_names = {
            str(name).strip()
            for name in (function_tool_names or ())
            if str(name).strip() and not _is_console_internal_tool_name(str(name).strip())
        }
        self._ignored_function_keys: set[str] = set()
        self._function_keys_by_output_index: dict[str, str] = {}

    def feed(self, event_type: str, data: str) -> list[str]:
        """解析一个 SSE 事件，返回文本 token 列表（通常 0 或 1 个）。"""
        if self._done:
            return []

        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError):
            return []

        if event_type == "response.output_text.delta":
            delta = obj.get("delta", "")
            if delta:
                self.text_buf.append(delta)
                return [delta]

        elif event_type == "response.output_item.added":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._upsert_function_call(item, obj)

        elif event_type == "response.function_call_arguments.delta":
            key = self._function_key(obj)
            if self._should_ignore_function_event(key, obj):
                return []
            delta = obj.get("delta", "")
            if key and isinstance(delta, str):
                info = self._ensure_function_call(key, obj)
                info["arguments"] = str(info.get("arguments") or "") + delta

        elif event_type == "response.function_call_arguments.done":
            key = self._function_key(obj)
            if self._should_ignore_function_event(key, obj):
                return []
            args = obj.get("arguments")
            if key and isinstance(args, str):
                info = self._ensure_function_call(key, obj)
                info["arguments"] = args

        elif event_type == "response.output_item.done":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._upsert_function_call(item, obj, completed=True)

        elif event_type == "response.completed":
            tokens: list[str] = []
            resp = obj.get("response", {})
            self.usage = resp.get("usage") if isinstance(resp, dict) else None
            output = resp.get("output") if isinstance(resp, dict) else None
            if isinstance(output, list):
                for item in output:
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        self._upsert_function_call(item, {}, completed=True)
                # Some upstream streams only include final text in response.completed.
                # Use it as a fallback, but never duplicate already-streamed deltas.
                if not self.text_buf:
                    text = _response_output_text(output)
                    if text:
                        self.text_buf.append(text)
                        tokens.append(text)
            self._done = True
            return tokens

        elif event_type == "error":
            msg = obj.get("message") or str(obj)
            raise UpstreamError(f"Console API error: {msg}", status=502)

        return []

    def _function_key(self, obj: dict[str, Any]) -> str:
        raw = obj.get("item_id")
        if raw:
            return str(raw)
        raw = obj.get("output_index")
        if raw is None:
            return ""
        idx_key = str(raw)
        return self._function_keys_by_output_index.get(idx_key) or f"output:{idx_key}"

    def _allows_function_name(self, name: str) -> bool:
        name = name.strip()
        return bool(name) and name in self._allowed_function_names

    def _forget_output_index_for_key(self, key: str) -> None:
        for idx, mapped_key in list(self._function_keys_by_output_index.items()):
            if mapped_key == key:
                self._function_keys_by_output_index.pop(idx, None)

    def _ignore_function_key(self, key: str) -> None:
        if not key:
            return
        self._ignored_function_keys.add(key)
        self._function_calls.pop(key, None)
        self._forget_output_index_for_key(key)
        if key in self._function_order:
            self._function_order = [item for item in self._function_order if item != key]

    def _should_ignore_function_event(self, key: str, obj: dict[str, Any]) -> bool:
        if not self._allowed_function_names:
            self._ignore_function_key(key)
            return True
        if key and key in self._ignored_function_keys:
            return True
        name = str(obj.get("name") or "").strip()
        if name and not self._allows_function_name(name):
            self._ignore_function_key(key)
            return True
        return False

    def _ensure_function_call(self, key: str, obj: dict[str, Any]) -> dict[str, Any]:
        info = self._function_calls.get(key)
        if info is None:
            info = {
                "id": key,
                "type": "function_call",
                "call_id": "",
                "name": "",
                "arguments": "",
                "status": "in_progress",
            }
            self._function_calls[key] = info
            self._function_order.append(key)
        output_index = obj.get("output_index")
        if output_index is not None:
            idx_key = str(output_index)
            info["output_index"] = output_index
            self._function_keys_by_output_index[idx_key] = key
        return info

    def _merge_function_keys(self, source_key: str, target_key: str) -> None:
        if not source_key or source_key == target_key:
            return
        source = self._function_calls.pop(source_key, None)
        if source is not None:
            target = self._function_calls.get(target_key)
            if target is None:
                self._function_calls[target_key] = source
                for i, key in enumerate(self._function_order):
                    if key == source_key:
                        self._function_order[i] = target_key
                        break
                if target_key not in self._function_order:
                    self._function_order.append(target_key)
            else:
                for field, value in source.items():
                    if field == "arguments":
                        if not target.get("arguments"):
                            target[field] = value
                    elif value and not target.get(field):
                        target[field] = value
                self._function_order = [
                    key for key in self._function_order if key != source_key
                ]
        for idx, mapped_key in list(self._function_keys_by_output_index.items()):
            if mapped_key == source_key:
                self._function_keys_by_output_index[idx] = target_key
        if source_key in self._ignored_function_keys:
            self._ignored_function_keys.add(target_key)

    def _upsert_function_call(
        self,
        item: dict[str, Any],
        event_obj: dict[str, Any],
        *,
        completed: bool = False,
    ) -> None:
        event_key = self._function_key(event_obj)
        item_key = str(item.get("id") or item.get("call_id") or "")
        key = item_key or event_key
        if not key:
            return
        if event_key and item_key and event_key != item_key:
            self._merge_function_keys(event_key, item_key)
            key = item_key
        if key in self._ignored_function_keys:
            return

        name = str(item.get("name") or "").strip()
        if name and not self._allows_function_name(name):
            self._ignore_function_key(key)
            return

        info = self._ensure_function_call(key, event_obj)
        for field in ("id", "call_id", "name"):
            if item.get(field):
                info[field] = item[field]
        item_args = item.get("arguments")
        if isinstance(item_args, str) and (item_args or not info.get("arguments")):
            info["arguments"] = item_args
        if completed or item.get("status") == "completed":
            info["status"] = "completed"

    @property
    def full_text(self) -> str:
        return "".join(self.text_buf)

    @property
    def function_call_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key in self._function_order:
            info = self._function_calls.get(key) or {}
            name = str(info.get("name") or "").strip()
            if not self._allows_function_name(name):
                continue
            items.append({
                "id": str(info.get("id") or key),
                "type": "function_call",
                "call_id": str(info.get("call_id") or key),
                "name": name,
                "arguments": str(info.get("arguments") or "{}"),
                "status": str(info.get("status") or "completed"),
            })
        return items

    @property
    def parsed_tool_calls(self) -> list[ParsedToolCall]:
        return [
            ParsedToolCall(
                call_id=str(item.get("call_id") or item.get("id")),
                name=str(item["name"]),
                arguments=str(item.get("arguments") or "{}"),
            )
            for item in self.function_call_items
        ]


def _response_output_text(output: list[Any]) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            if content:
                parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "text", "input_text"):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts)


def classify_console_line(line: str) -> tuple[str, str]:
    """Parse a raw SSE line into (event_type, data).

    console.x.ai 使用标准 SSE 格式:
        event: response.output_text.delta
        data: {...}
    """
    line = line.strip()
    if not line:
        return "skip", ""
    if line.startswith("event:"):
        return "event", line[6:].strip()
    if line.startswith("data:"):
        data = line[5:].strip()
        if data == "[DONE]":
            return "done", ""
        return "data", data
    return "skip", ""


async def stream_console_chat(
    token: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 120.0,
) -> AsyncGenerator[tuple[str, str], None]:
    """POST to console.x.ai/v1/responses and yield (event_type, data) pairs.

    走现有的 proxy lease + curl-cffi 体系，与 grok.com 共用 CF clearance。
    """
    from app.dataplane.proxy import get_proxy_runtime
    from app.dataplane.proxy.adapters.headers import build_console_headers
    from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
    from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_RESPONSES

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    headers = build_console_headers(token, lease=lease)
    payload_bytes = orjson.dumps(payload)
    session_kwargs = build_session_kwargs(lease=lease)

    async with ResettableSession(**session_kwargs) as session:
        try:
            response = await session.post(
                CONSOLE_RESPONSES,
                headers=headers,
                data=payload_bytes,
                timeout=timeout_s,
                stream=True,
            )
        except Exception as exc:
            await proxy.feedback(lease, _transport_error_feedback())
            raise UpstreamError(f"Console transport failed: {exc}", status=502) from exc

        if response.status_code != 200:
            try:
                body = response.content.decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            await proxy.feedback(lease, _status_feedback(response.status_code))
            raise UpstreamError(
                f"Console API returned {response.status_code}",
                status=response.status_code,
                body=body,
            )

        await proxy.feedback(lease, _success_feedback())

        current_event = ""
        try:
            async for raw_line in response.aiter_lines():
                # curl-cffi 的 aiter_lines 返回 bytes，先解码为 str
                if isinstance(raw_line, bytes):
                    try:
                        raw_line = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        raw_line = raw_line.decode("utf-8", errors="replace")
                kind, value = classify_console_line(raw_line)
                if kind == "event":
                    current_event = value
                elif kind == "data":
                    yield current_event, value
                    current_event = ""
                elif kind == "done":
                    return
        except Exception as exc:
            raise UpstreamError(f"Console stream read failed: {exc}", status=502) from exc


def _success_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)

def _transport_error_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR)

def _status_feedback(status: int):
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    if status == 403:
        kind = ProxyFeedbackKind.CHALLENGE
    elif status == 429:
        kind = ProxyFeedbackKind.RATE_LIMITED
    elif status >= 500:
        kind = ProxyFeedbackKind.UPSTREAM_5XX
    else:
        kind = ProxyFeedbackKind.FORBIDDEN
    return ProxyFeedback(kind=kind, status_code=status)


__all__ = [
    "CONSOLE_MODELS",
    "build_console_payload",
    "client_function_tool_names",
    "ConsoleStreamAdapter",
    "classify_console_line",
    "stream_console_chat",
]
