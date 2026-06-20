from types import SimpleNamespace
import unittest
from unittest.mock import patch

import orjson

from app.products.anthropic import messages as anthropic_messages
from app.products.anthropic import console_messages
from app.products.anthropic import router as anthropic_router
from app.control.model import registry as model_registry
from app.dataplane.reverse.protocol import xai_chat


def _data(obj: dict) -> str:
    return orjson.dumps(obj).decode()


def _anthropic_payloads(frames: list[str]) -> list[dict]:
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


class _FakeConfig:
    def get(self, key: str, default=None):
        return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        return default

    def get_float(self, key: str, default: float) -> float:
        return default


class _FakeDirectory:
    async def release(self, acct) -> None:
        pass

    async def feedback(self, *args, **kwargs) -> None:
        pass


async def _fake_reserve_account(*args, **kwargs):
    return SimpleNamespace(token="token-test"), 1


async def _noop_async(*args, **kwargs) -> None:
    pass


class AnthropicClaudeCodeCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_grok_stream_message_delta_repeats_input_tokens(self) -> None:
        async def fake_stream_chat(*args, **kwargs):
            yield "data: " + _data({
                "result": {
                    "response": {
                        "token": "hello",
                        "isThinking": False,
                        "messageTag": "final",
                    },
                },
            })
            yield "data: " + _data({
                "result": {"response": {"isSoftStop": True}},
            })

        with (
            patch("app.dataplane.account._directory", _FakeDirectory()),
            patch.object(anthropic_messages, "get_config", return_value=_FakeConfig()),
            patch.object(xai_chat, "get_config", return_value=_FakeConfig()),
            patch.object(anthropic_messages, "selection_max_retries", return_value=0),
            patch.object(anthropic_messages, "reserve_account", _fake_reserve_account),
            patch.object(anthropic_messages, "_stream_chat", fake_stream_chat),
            patch.object(anthropic_messages, "_quota_sync", _noop_async),
            patch.object(anthropic_messages, "_fail_sync", _noop_async),
        ):
            stream = await anthropic_messages.create(
                model="grok-4.3-fast",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
            )
            frames = [frame async for frame in stream]

        payloads = _anthropic_payloads(frames)
        deltas = [payload for payload in payloads if payload.get("type") == "message_delta"]
        self.assertTrue(deltas)
        usage = deltas[-1]["usage"]
        self.assertGreater(usage["input_tokens"], 0)
        self.assertGreaterEqual(usage["output_tokens"], 0)

    async def test_console_stream_message_delta_repeats_input_tokens(self) -> None:
        async def fake_stream_console_chat(*args, **kwargs):
            yield "response.output_text.delta", _data({"delta": "hello"})
            yield "response.completed", _data({"response": {}})

        with (
            patch("app.dataplane.account._directory", _FakeDirectory()),
            patch.object(console_messages, "get_config", return_value=_FakeConfig()),
            patch.object(console_messages, "selection_max_retries", return_value=0),
            patch.object(console_messages, "reserve_account", _fake_reserve_account),
            patch.object(console_messages, "stream_console_chat", fake_stream_console_chat),
            patch.object(console_messages, "_quota_sync", _noop_async),
            patch.object(console_messages, "_fail_sync", _noop_async),
        ):
            stream = await console_messages.create(
                model="grok-4.3-console",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
                msg_id="msg_test",
            )
            frames = [frame async for frame in stream]

        payloads = _anthropic_payloads(frames)
        deltas = [payload for payload in payloads if payload.get("type") == "message_delta"]
        self.assertTrue(deltas)
        usage = deltas[-1]["usage"]
        self.assertGreater(usage["input_tokens"], 0)
        self.assertGreaterEqual(usage["output_tokens"], 0)

    async def test_count_tokens_route_returns_positive_input_tokens(self) -> None:
        route = next(
            (
                route
                for route in anthropic_router.router.routes
                if getattr(route, "path", "") == "/v1/messages/count_tokens"
            ),
            None,
        )
        self.assertIsNotNone(route)
        if route is None:
            return

        request = anthropic_router.CountTokensRequest(
            model="grok-4.3-fast",
            system="You are concise.",
            messages=[{"role": "user", "content": "hello"}],
        )
        response = await route.endpoint(request)
        body = orjson.loads(response.body)

        self.assertGreater(body["input_tokens"], 0)


class ModelAliasTests(unittest.TestCase):
    def test_grok_4_2_fast_is_available_as_compatibility_alias(self) -> None:
        spec = model_registry.get("grok-4.2-fast")

        self.assertIsNotNone(spec)
        self.assertTrue(spec.enabled)


if __name__ == "__main__":
    unittest.main()
