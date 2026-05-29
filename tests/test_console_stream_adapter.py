import unittest

import orjson

from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    build_console_payload,
    client_function_tool_names,
)


def _data(obj: dict) -> str:
    return orjson.dumps(obj).decode()


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

    def test_filters_builtin_calls_from_completed_output(self) -> None:
        adapter = ConsoleStreamAdapter(function_tool_names={"lookup_order"})

        adapter.feed(
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
                            "id": "fc_1",
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "lookup_order",
                            "arguments": '{"order_id":"A123"}',
                            "status": "completed",
                        },
                    ],
                },
            }),
        )

        self.assertEqual(len(adapter.function_call_items), 1)
        self.assertEqual(adapter.function_call_items[0]["name"], "lookup_order")

    def test_user_function_tools_do_not_enable_default_search_tools(self) -> None:
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
        self.assertEqual(payload["tools"], [{
            "type": "function",
            "name": "lookup_order",
            "parameters": {"type": "object", "properties": {}},
        }])


if __name__ == "__main__":
    unittest.main()
