import importlib
import types
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError as PydanticValidationError

from app.products.openai.schemas import (
    ChatCompletionRequest,
    MessageItem,
    ResponsesCreateRequest,
)

router = importlib.import_module("app.products.openai.router")
anthropic_router = importlib.import_module("app.products.anthropic.router")


class _ChatSpec:
    enabled = True

    @staticmethod
    def is_image_edit():
        return False

    @staticmethod
    def is_image():
        return False

    @staticmethod
    def is_video():
        return False

    @staticmethod
    def is_chat():
        return True

    @staticmethod
    def is_oauth_chat():
        return False


class OpenAIRequestDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_preserves_explicit_zero_sampling_values(self):
        run = AsyncMock(return_value={"object": "chat.completion"})
        req = ChatCompletionRequest(
            model="test-model",
            messages=[MessageItem(role="user", content="hello")],
            stream=False,
            temperature=0,
            top_p=0,
        )

        with (
            patch.object(router.model_registry, "get", return_value=_ChatSpec()),
            patch.object(
                router,
                "_available_accounts",
                AsyncMock(return_value=(frozenset({"basic"}), frozenset())),
            ),
            patch.object(router, "_model_available", return_value=True),
            patch.object(router, "chat_completions", run),
        ):
            await router.chat_completions_endpoint(
                req,
                types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace())),
            )

        self.assertEqual(run.await_args.kwargs["temperature"], 0)
        self.assertEqual(run.await_args.kwargs["top_p"], 0)

    async def test_responses_preserves_explicit_zero_sampling_values(self):
        run = AsyncMock(return_value={"object": "response"})
        req = ResponsesCreateRequest(
            model="test-model",
            input="hello",
            stream=False,
            temperature=0,
            top_p=0,
        )

        with (
            patch.object(router.model_registry, "get", return_value=_ChatSpec()),
            patch.object(
                router,
                "_available_accounts",
                AsyncMock(return_value=(frozenset({"basic"}), frozenset())),
            ),
            patch.object(router, "_model_available", return_value=True),
            patch("app.products.openai.responses.create", run),
        ):
            await router.responses_endpoint(
                req,
                types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace())),
            )

        self.assertEqual(run.await_args.kwargs["temperature"], 0)
        self.assertEqual(run.await_args.kwargs["top_p"], 0)

    def test_responses_rejects_out_of_range_sampling_values(self):
        with self.assertRaises(PydanticValidationError):
            ResponsesCreateRequest(model="test", input="x", temperature=-0.1)
        with self.assertRaises(PydanticValidationError):
            ResponsesCreateRequest(model="test", input="x", top_p=1.1)

    def test_chat_rejects_out_of_range_sampling_values(self):
        with self.assertRaises(PydanticValidationError):
            ChatCompletionRequest(
                model="test",
                messages=[MessageItem(role="user", content="x")],
                temperature=2.1,
            )
        with self.assertRaises(PydanticValidationError):
            ChatCompletionRequest(
                model="test",
                messages=[MessageItem(role="user", content="x")],
                top_p=-0.1,
            )

    async def test_anthropic_preserves_explicit_zero_sampling_values(self):
        run = AsyncMock(return_value={"type": "message"})
        req = anthropic_router.MessagesRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
            temperature=0,
            top_p=0,
        )

        with (
            patch.object(
                anthropic_router.model_registry,
                "get",
                return_value=_ChatSpec(),
            ),
            patch("app.products.anthropic.messages.create", run),
        ):
            await anthropic_router.messages_endpoint(req)

        self.assertEqual(run.await_args.kwargs["temperature"], 0)
        self.assertEqual(run.await_args.kwargs["top_p"], 0)

    def test_anthropic_rejects_out_of_range_sampling_values(self):
        with self.assertRaises(PydanticValidationError):
            anthropic_router.MessagesRequest(
                model="test",
                messages=[],
                temperature=2.1,
            )
        with self.assertRaises(PydanticValidationError):
            anthropic_router.MessagesRequest(
                model="test",
                messages=[],
                top_p=-0.1,
            )


if __name__ == "__main__":
    unittest.main()
