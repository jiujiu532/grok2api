import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import orjson

from app.platform.tokens import estimate_tool_call_tokens
from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    build_console_payload,
    client_function_tool_names,
)


def _data(obj: dict) -> str:
    return orjson.dumps(obj).decode()


class _FakeConfig:
    def get(self, key: str, default=None):
        return default

    def get_float(self, key: str, default: float) -> float:
        return default


class _FakeDirectory:
    async def release(self, acct) -> None:
        pass

    async def feedback(self, *args, **kwargs) -> None:
        pass


class _FakeLogger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass


async def _fake_reserve_account(*args, **kwargs):
    return SimpleNamespace(token="token-test"), 5


async def _noop_async(*args, **kwargs) -> None:
    pass


def _chat_stream_payloads(frames: list[str]) -> list[dict]:
    payloads: list[dict] = []
    for frame in frames:
        for line in frame.splitlines():
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                continue
            payloads.append(orjson.loads(data))
    return payloads


class ConsoleStreamAdapterToolFilteringTests(unittest.TestCase):
    def test_ignores_builtin_tool_events_when_client_function_tools_are_active(self) -> None:
        adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})

        adapter.feed(
            "response.output_item.added",
            _data({
                "output_index": 0,
                "item": {
                    "id": "builtin_1",
                    "type": "function_call",
                    "call_id": "call_builtin",
                    "name": "web_search",
                    "arguments": "",
                    "status": "in_progress",
                },
            }),
        )
        adapter.feed(
            "response.function_call_arguments.done",
            _data({
                "item_id": "builtin_1",
                "output_index": 0,
                "arguments": '{"query":"latest news"}',
            }),
        )
        tokens = adapter.feed(
            "response.output_text.delta",
            _data({"delta": "Here is the answer after search."}),
        )

        self.assertEqual(tokens, ["Here is the answer after search."])
        self.assertEqual(adapter.full_text, "Here is the answer after search.")
        self.assertEqual(adapter.function_call_items, [])
        self.assertEqual(adapter.parsed_tool_calls, [])

    def test_default_adapter_ignores_all_function_calls_and_keeps_text(self) -> None:
        adapter = ConsoleStreamAdapter()

        adapter.feed(
            "response.output_item.added",
            _data({
                "output_index": 0,
                "item": {
                    "id": "builtin_1",
                    "type": "function_call",
                    "call_id": "call_builtin",
                    "name": "x_search",
                    "arguments": "",
                },
            }),
        )
        tokens = adapter.feed(
            "response.output_text.delta",
            _data({"delta": "final text"}),
        )

        self.assertEqual(tokens, ["final text"])
        self.assertEqual(adapter.function_call_items, [])

    def test_collects_client_declared_function_tool_calls(self) -> None:
        adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})

        adapter.feed(
            "response.output_item.added",
            _data({
                "output_index": 0,
                "item": {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup_order",
                    "arguments": "",
                    "status": "in_progress",
                },
            }),
        )
        adapter.feed(
            "response.function_call_arguments.delta",
            _data({
                "item_id": "fc_1",
                "output_index": 0,
                "delta": '{"order_id":"A',
            }),
        )
        adapter.feed(
            "response.function_call_arguments.done",
            _data({
                "item_id": "fc_1",
                "output_index": 0,
                "arguments": '{"order_id":"A123"}',
            }),
        )

        self.assertEqual(
            adapter.function_call_items,
            [{
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup_order",
                "arguments": '{"order_id":"A123"}',
                "status": "in_progress",
            }],
        )
        self.assertEqual(adapter.parsed_tool_calls[0].name, "lookup_order")

    def test_collects_arguments_when_delta_arrives_before_item_id(self) -> None:
        adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})

        adapter.feed(
            "response.function_call_arguments.delta",
            _data({"output_index": 0, "delta": '{"order_id":"A'}),
        )
        adapter.feed(
            "response.output_item.done",
            _data({
                "output_index": 0,
                "item": {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup_order",
                    "arguments": '{"order_id":"A123"}',
                    "status": "completed",
                },
            }),
        )

        self.assertEqual(len(adapter.function_call_items), 1)
        self.assertEqual(adapter.function_call_items[0]["id"], "fc_1")
        self.assertEqual(adapter.function_call_items[0]["arguments"], '{"order_id":"A123"}')

    def test_console_chat_stream_buffers_text_when_late_tool_call_arrives(self) -> None:
        async def fake_stream_console_chat(*args, **kwargs):
            yield "response.output_text.delta", _data({"delta": "preface"})
            yield "response.output_item.done", _data({
                "output_index": 0,
                "item": {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": "{}",
                    "status": "completed",
                },
            })
            yield "response.completed", _data({"response": {}})

        async def run() -> list[str]:
            from app.products.openai import console_chat

            with (
                patch("app.dataplane.account._directory", _FakeDirectory()),
                patch.object(console_chat, "logger", _FakeLogger()),
                patch.object(console_chat, "get_config", return_value=_FakeConfig()),
                patch.object(console_chat, "selection_max_retries", return_value=0),
                patch.object(console_chat, "reserve_account", _fake_reserve_account),
                patch.object(console_chat, "stream_console_chat", fake_stream_console_chat),
                patch.object(console_chat, "_quota_sync", _noop_async),
                patch.object(console_chat, "_fail_sync", _noop_async),
            ):
                gen = await console_chat.completions(
                    model="grok-build-console",
                    messages=[{"role": "user", "content": "use bash"}],
                    stream=True,
                    tools=[{"type": "function", "function": {"name": "bash"}}],
                )
                return [frame async for frame in gen]

        frames = asyncio.run(run())
        payloads = _chat_stream_payloads(frames)

        self.assertNotIn("preface", "".join(frames))
        self.assertGreaterEqual(frames.count(": heartbeat\n\n"), 2)
        self.assertTrue(any(
            (payload["choices"][0].get("delta") or {}).get("tool_calls")
            for payload in payloads
        ))
        self.assertTrue(any(
            payload["choices"][0].get("finish_reason") == "tool_calls"
            for payload in payloads
        ))
        final_usage = next(
            payload["usage"] for payload in payloads
            if payload["choices"][0].get("finish_reason") == "tool_calls"
        )
        self.assertEqual(
            final_usage["completion_tokens"],
            estimate_tool_call_tokens([SimpleNamespace(name="bash", arguments="{}", call_id="call_1")]),
        )

    def test_console_chat_non_stream_tool_call_estimates_usage_without_upstream_usage(self) -> None:
        async def fake_stream_console_chat(*args, **kwargs):
            yield "response.output_text.delta", _data({"delta": "preface"})
            yield "response.output_item.done", _data({
                "output_index": 0,
                "item": {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": "{}",
                    "status": "completed",
                },
            })
            yield "response.completed", _data({"response": {}})

        async def run() -> dict:
            from app.products.openai import console_chat

            with (
                patch("app.dataplane.account._directory", _FakeDirectory()),
                patch.object(console_chat, "logger", _FakeLogger()),
                patch.object(console_chat, "get_config", return_value=_FakeConfig()),
                patch.object(console_chat, "selection_max_retries", return_value=0),
                patch.object(console_chat, "reserve_account", _fake_reserve_account),
                patch.object(console_chat, "stream_console_chat", fake_stream_console_chat),
                patch.object(console_chat, "_quota_sync", _noop_async),
                patch.object(console_chat, "_fail_sync", _noop_async),
            ):
                return await console_chat.completions(
                    model="grok-build-console",
                    messages=[{"role": "user", "content": "use bash"}],
                    stream=False,
                    tools=[{"type": "function", "function": {"name": "bash"}}],
                )

        response = asyncio.run(run())

        self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(
            response["usage"]["completion_tokens"],
            estimate_tool_call_tokens([SimpleNamespace(name="bash", arguments="{}", call_id="call_1")]),
        )

    def test_responses_console_route_does_not_inject_legacy_xml_tool_prompt(self) -> None:
        async def fake_console_create(**kwargs):
            return {"messages": kwargs["messages"]}

        async def run() -> dict:
            from app.products.openai import console_responses, responses

            with (
                patch("app.dataplane.account._directory", _FakeDirectory()),
                patch.object(responses, "get_config", return_value=_FakeConfig()),
                patch.object(
                    responses,
                    "build_tool_system_prompt",
                    side_effect=AssertionError("console route must use native tools only"),
                ),
                patch.object(console_responses, "create", fake_console_create),
            ):
                return await responses.create(
                    model="grok-build-console",
                    input_val="use bash",
                    instructions=None,
                    stream=False,
                    emit_think=False,
                    temperature=0.7,
                    top_p=0.95,
                    tools=[{"type": "function", "function": {"name": "bash"}}],
                )

        result = asyncio.run(run())

        self.assertEqual(result["messages"], [{"role": "user", "content": "use bash"}])

    def test_console_responses_stream_buffers_text_when_late_function_call_arrives(self) -> None:
        async def fake_stream_console_chat(*args, **kwargs):
            yield "response.output_text.delta", _data({"delta": "preface"})
            yield "response.output_item.done", _data({
                "output_index": 0,
                "item": {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": "{}",
                    "status": "completed",
                },
            })
            yield "response.completed", _data({"response": {}})

        async def run() -> list[str]:
            from app.products.openai import console_responses

            with (
                patch("app.dataplane.account._directory", _FakeDirectory()),
                patch.object(console_responses, "logger", _FakeLogger()),
                patch.object(console_responses, "get_config", return_value=_FakeConfig()),
                patch.object(console_responses, "selection_max_retries", return_value=0),
                patch.object(console_responses, "reserve_account", _fake_reserve_account),
                patch.object(console_responses, "stream_console_chat", fake_stream_console_chat),
                patch.object(console_responses, "_quota_sync", _noop_async),
                patch.object(console_responses, "_fail_sync", _noop_async),
            ):
                gen = await console_responses.create(
                    model="grok-build-console",
                    messages=[{"role": "user", "content": "use bash"}],
                    stream=True,
                    emit_think=False,
                    temperature=0.7,
                    top_p=0.95,
                    response_id="resp_test",
                    reasoning_id="rs_test",
                    message_id="msg_test",
                    tools=[{"type": "function", "function": {"name": "bash"}}],
                )
                return [frame async for frame in gen]

        frames = asyncio.run(run())
        joined = "".join(frames)

        payloads = _chat_stream_payloads(frames)
        completed = next(payload for payload in payloads if payload.get("type") == "response.completed")

        self.assertNotIn("preface", joined)
        self.assertGreaterEqual(frames.count(": heartbeat\n\n"), 2)
        self.assertIn("response.function_call_arguments.done", joined)
        self.assertIn('"type":"function_call"', joined)
        self.assertEqual(
            completed["response"]["usage"]["output_tokens"],
            estimate_tool_call_tokens([SimpleNamespace(name="bash", arguments="{}", call_id="call_1")]),
        )

    def test_console_responses_non_stream_function_call_estimates_usage_without_upstream_usage(self) -> None:
        async def fake_stream_console_chat(*args, **kwargs):
            yield "response.output_text.delta", _data({"delta": "preface"})
            yield "response.output_item.done", _data({
                "output_index": 0,
                "item": {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": "{}",
                    "status": "completed",
                },
            })
            yield "response.completed", _data({"response": {}})

        async def run() -> dict:
            from app.products.openai import console_responses

            with (
                patch("app.dataplane.account._directory", _FakeDirectory()),
                patch.object(console_responses, "logger", _FakeLogger()),
                patch.object(console_responses, "get_config", return_value=_FakeConfig()),
                patch.object(console_responses, "selection_max_retries", return_value=0),
                patch.object(console_responses, "reserve_account", _fake_reserve_account),
                patch.object(console_responses, "stream_console_chat", fake_stream_console_chat),
                patch.object(console_responses, "_quota_sync", _noop_async),
                patch.object(console_responses, "_fail_sync", _noop_async),
            ):
                return await console_responses.create(
                    model="grok-build-console",
                    messages=[{"role": "user", "content": "use bash"}],
                    stream=False,
                    emit_think=False,
                    temperature=0.7,
                    top_p=0.95,
                    response_id="resp_test",
                    reasoning_id="rs_test",
                    message_id="msg_test",
                    tools=[{"type": "function", "function": {"name": "bash"}}],
                )

        response = asyncio.run(run())

        self.assertEqual(response["output"][0]["type"], "function_call")
        self.assertEqual(
            response["usage"]["output_tokens"],
            estimate_tool_call_tokens([SimpleNamespace(name="bash", arguments="{}", call_id="call_1")]),
        )

    def test_filters_builtin_calls_from_completed_output_and_uses_message_text_fallback(self) -> None:
        adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})

        tokens = adapter.feed(
            "response.completed",
            _data({
                "response": {
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "output": [
                        {
                            "id": "builtin_1",
                            "type": "function_call",
                            "call_id": "call_builtin",
                            "name": "x_search",
                            "arguments": '{"query":"grok"}',
                            "status": "completed",
                        },
                        {
                            "id": "msg_1",
                            "type": "message",
                            "content": [{"type": "output_text", "text": "answer"}],
                        },
                    ],
                },
            }),
        )

        self.assertEqual(tokens, ["answer"])
        self.assertEqual(adapter.full_text, "answer")
        self.assertEqual(adapter.function_call_items, [])

    def test_keeps_native_search_tools_when_user_function_tools_are_present(self) -> None:
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup_order",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

        payload = build_console_payload(
            messages=[{"role": "user", "content": "lookup order"}],
            model="grok-build-console",
            tools=tools,
        )

        self.assertEqual(client_function_tool_names(tools), {"lookup_order"})
        self.assertEqual(
            payload["tools"],
            [
                {"type": "web_search", "enable_image_understanding": True},
                {"type": "x_search", "enable_video_understanding": True},
                {
                    "type": "function",
                    "name": "lookup_order",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        )
        self.assertEqual(payload["tool_choice"], "auto")

    def test_null_content_does_not_become_literal_none_text(self) -> None:
        payload = build_console_payload(
            messages=[{"role": "user", "content": None}],
            model="grok-build-console",
        )

        self.assertEqual(payload["input"], [])

    def test_preserves_existing_message_content_conversion(self) -> None:
        payload = build_console_payload(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    {"type": "unknown", "text": "fallback"},
                    "ignored like upstream",
                ],
            }],
            model="grok-build-console",
        )

        self.assertEqual(
            payload["input"],
            [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hello"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    {"type": "input_text", "text": "fallback"},
                ],
            }],
        )

    def test_maps_tool_call_roundtrip_messages_to_console_input(self) -> None:
        payload = build_console_payload(
            messages=[
                {"role": "user", "content": "lookup order"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup_order",
                            "arguments": '{"order_id":"A123"}',
                        },
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "shipped"},
            ],
            model="grok-build-console",
            tools=[{
                "type": "function",
                "function": {"name": "lookup_order", "parameters": {"type": "object"}},
            }],
        )

        self.assertEqual(
            payload["input"],
            [
                {"role": "user", "content": [{"type": "input_text", "text": "lookup order"}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup_order",
                    "arguments": '{"order_id":"A123"}',
                    "status": "completed",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "shipped"},
            ],
        )

    def test_assistant_tool_call_history_skips_empty_content_from_router_shape(self) -> None:
        for assistant_message in (
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_order", "arguments": "{}"},
                }],
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_order", "arguments": "{}"},
                }],
            },
        ):
            with self.subTest(assistant_message=assistant_message):
                payload = build_console_payload(
                    messages=[assistant_message],
                    model="grok-build-console",
                    tools=[{"type": "function", "function": {"name": "lookup_order"}}],
                )

                self.assertEqual(
                    payload["input"],
                    [{
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup_order",
                        "arguments": "{}",
                        "status": "completed",
                    }],
                )

    def test_internal_tool_names_are_not_treated_as_client_function_tools(self) -> None:
        tools = [
            {"type": "function", "function": {"name": name}}
            for name in (
                "web_search",
                "web_search_with_snippets",
                "browse_page",
                "open_page",
                "open_page_with_find",
                "search_images",
                "image_search",
                "view_image",
                "x_search",
                "x_user_search",
                "x_keyword_search",
                "x_semantic_search",
                "x_thread_fetch",
                "view_x_video",
                "code_execution",
                "code_interpreter",
                "collections_search",
                "file_search",
                "chatroom_send",
            )
        ]

        self.assertEqual(client_function_tool_names(tools), set())
        payload = build_console_payload(
            messages=[{"role": "user", "content": "search"}],
            model="grok-build-console",
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "web_search"}},
        )

        self.assertEqual(payload["tools"][0]["type"], "web_search")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertFalse(any(tool.get("type") == "function" for tool in payload["tools"]))

    def test_user_tool_named_search_is_still_a_client_function_tool(self) -> None:
        tools = [{"type": "function", "function": {"name": "search"}}]

        self.assertEqual(client_function_tool_names(tools), {"search"})
        payload = build_console_payload(
            messages=[{"role": "user", "content": "search local index"}],
            model="grok-build-console",
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "search"}},
        )

        self.assertIn({"type": "function", "name": "search"}, payload["tools"])
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "search"})

    def test_passes_through_explicit_builtin_console_tools(self) -> None:
        payload = build_console_payload(
            messages=[{"role": "user", "content": "calculate and search docs"}],
            model="grok-build-console",
            tools=[
                {"type": "code_interpreter"},
                {"type": "collections_search", "collection_ids": ["col_1"]},
            ],
        )

        self.assertIn({"type": "code_interpreter"}, payload["tools"])
        self.assertIn(
            {"type": "collections_search", "collection_ids": ["col_1"]},
            payload["tools"],
        )

    def test_explicit_builtin_tool_config_overrides_default_search_config(self) -> None:
        payload = build_console_payload(
            messages=[{"role": "user", "content": "search x.ai"}],
            model="grok-build-console",
            tools=[{
                "type": "web_search",
                "filters": {"allowed_domains": ["x.ai"]},
            }],
        )

        self.assertEqual(
            payload["tools"][0],
            {"type": "web_search", "filters": {"allowed_domains": ["x.ai"]}},
        )
        self.assertEqual(payload["tools"][1]["type"], "x_search")

    def test_ignores_internal_function_events_not_declared_by_client(self) -> None:
        for name in (
            "web_search_with_snippets",
            "open_page",
            "open_page_with_find",
            "x_user_search",
            "x_thread_fetch",
            "search_images",
            "image_search",
            "code_execution",
            "view_image",
        ):
            with self.subTest(name=name):
                adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})
                adapter.feed(
                    "response.output_item.added",
                    _data({
                        "output_index": 0,
                        "item": {
                            "id": f"builtin_{name}",
                            "type": "function_call",
                            "call_id": f"call_{name}",
                            "name": name,
                            "arguments": "",
                        },
                    }),
                )
                tokens = adapter.feed(
                    "response.output_text.delta",
                    _data({"delta": f"answer after {name}"}),
                )

                self.assertEqual(tokens, [f"answer after {name}"])
                self.assertEqual(adapter.function_call_items, [])


if __name__ == "__main__":
    unittest.main()
