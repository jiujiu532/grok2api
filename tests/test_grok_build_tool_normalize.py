import unittest

from app.products.openai.grok_build import (
    _normalize_tools_for_upstream,
    _responses_payload,
    _normalize_effort,
)


class NormalizeCustomToolsTests(unittest.TestCase):
    def test_custom_tool_becomes_function(self):
        tools = [
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Edit files.",
                "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/s"},
            }
        ]
        out = _normalize_tools_for_upstream(tools)
        self.assertEqual(len(out), 1)
        tool = out[0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["name"], "apply_patch")
        self.assertEqual(tool["description"], "Edit files.")
        self.assertEqual(tool["parameters"]["required"], ["input"])
        self.assertNotIn("format", tool)

    def test_function_tool_passthrough(self):
        tools = [
            {
                "type": "function",
                "name": "shell",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        self.assertEqual(_normalize_tools_for_upstream(tools), tools)

    def test_supported_kept_unsupported_dropped(self):
        # web_search is supported (kept); tool_search / nameless custom rejected (dropped).
        tools = [
            {"type": "function", "name": "shell", "parameters": {"type": "object"}},
            {"type": "web_search", "external_web_access": True},
            {"type": "tool_search", "execution": {}},
            {"type": "custom", "description": "no name"},
        ]
        out = _normalize_tools_for_upstream(tools)
        self.assertEqual([t["type"] for t in out], ["function", "web_search"])
        self.assertEqual(out[0]["name"], "shell")

    def test_responses_payload_strips_external_web_access_and_additional_tools(self):
        payload = {
            "model": "grok-4.5",
            "input": [
                {"type": "additional_tools", "tools": [{"type": "x"}]},
                {"type": "message", "role": "user", "content": "hi"},
            ],
            "stream": True,
            "tools": [
                {"type": "web_search", "external_web_access": True, "search_content_types": ["text"]},
                {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
                {"type": "tool_search", "execution": {}},
            ],
        }
        body = _responses_payload(payload)
        # external_web_access removed everywhere
        import json
        self.assertNotIn("external_web_access", json.dumps(body))
        # additional_tools input item filtered out
        self.assertEqual([i["type"] for i in body["input"]], ["message"])
        # tool_search dropped, web_search kept, custom→function
        self.assertEqual({t["type"] for t in body["tools"]}, {"web_search", "function"})

    def test_responses_payload_drops_dangling_tool_choice(self):
        payload = {
            "model": "grok-4.5",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "stream": True,
            "tools": [{"type": "tool_search", "execution": {}}],  # all dropped
            "tool_choice": {"type": "function", "name": "gone"},
        }
        body = _responses_payload(payload)
        self.assertNotIn("tool_choice", body)

    def test_responses_payload_drops_reasoning_history_and_include(self):
        # Multi-turn: echoed reasoning items + include:reasoning.encrypted_content
        # make xAI OAuth upstream return HTTP 400.
        payload = {
            "model": "grok-4.5",
            "input": [
                {"type": "message", "role": "user", "content": "hi"},
                {"type": "reasoning", "summary": [], "encrypted_content": "xxx"},
                {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"},
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ],
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        body = _responses_payload(payload)
        self.assertEqual(
            [i["type"] for i in body["input"]],
            ["message", "function_call", "function_call_output"],
        )
        self.assertNotIn("include", body)

    def test_effort_normalization(self):
        # xAI accepts low/medium/high/xhigh/minimal; rejects max/none.
        self.assertEqual(_normalize_effort("high"), "high")
        self.assertEqual(_normalize_effort("xhigh"), "xhigh")
        self.assertEqual(_normalize_effort("minimal"), "minimal")
        self.assertEqual(_normalize_effort("max"), "high")
        self.assertIsNone(_normalize_effort("none"))

    def test_responses_payload_downgrades_max_effort(self):
        payload = {
            "model": "grok-4.5",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "stream": True,
            "reasoning": {"effort": "max"},
        }
        body = _responses_payload(payload)
        self.assertEqual(body["reasoning"]["effort"], "high")

    def test_responses_payload_drops_none_effort(self):
        payload = {
            "model": "grok-4.5",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "stream": True,
            "reasoning": {"effort": "none"},
        }
        body = _responses_payload(payload)
        self.assertNotIn("reasoning", body)

    def test_responses_payload_normalizes_custom_tools(self):
        payload = {
            "model": "grok-4.5",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "stream": True,
            "tools": [
                {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
                {"type": "function", "name": "shell", "parameters": {"type": "object"}},
            ],
        }
        body = _responses_payload(payload)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["name"], "apply_patch")
        self.assertEqual(body["tools"][1]["type"], "function")
        self.assertEqual(body["tools"][1]["name"], "shell")


if __name__ == "__main__":
    unittest.main()
