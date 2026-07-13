import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orjson

from app.control.account import oauth as oauth_mod
from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.control.account.oauth import GrokOAuthService
from app.control.account.oauth import (
    OAuthAccountLease,
    is_oauth_manageable,
    oauth_model_ids,
)
from app.control.model import registry as model_registry
from app.control.account.state_machine import is_sso_maintainable
from app.dataplane.reverse.protocol.xai_oauth import (
    API_RESPONSES_URL,
    BUILD_RESPONSES_URL,
)
from app.dataplane.account.sync import _record_to_slot_args
from app.platform.errors import RateLimitError, UpstreamError, ValidationError
from app.products.openai import grok_build
from app.products.web.admin import tokens as admin_tokens

_ROOT = Path(__file__).resolve().parents[1]


def _b64(value: dict) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(value)).rstrip(b"=").decode()


def _jwt(**claims) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}."


def _session(device_code: str = "device-1") -> str:
    return oauth_mod._encode_session(
        {
            "device_code": device_code,
            "exp": int(time.time()) + 600,
            "interval": 1,
        }
    )


async def _metadata(url: str, _token: str) -> dict:
    if url == oauth_mod.BUILD_USER_URL:
        return {"subscriptionTier": "GrokPro", "hasGrokCodeAccess": True}
    if url == oauth_mod.BUILD_BILLING_URL:
        return {
            "config": {
                "creditUsagePercent": 5,
                "currentPeriod": {"start": "2026-07-11", "end": "2026-07-18"},
                "productUsage": [{"product": "GrokImagine", "usagePercent": 5}],
            }
        }
    if url == oauth_mod.BUILD_MODELS_URL:
        return {"data": [{"id": "grok-4.5"}, {"id": "grok-composer-2.5-fast"}]}
    if url == oauth_mod.API_MODELS_URL:
        return {"data": [{"id": "grok-4.5"}, {"id": "grok-4.3"}]}
    return {"models": [{"id": "grok-4.5"}, {"id": "grok-4.3"}]}


class OAuthSessionTests(unittest.TestCase):
    def test_signed_session_round_trip_and_tamper_rejection(self):
        session_id = _session()

        self.assertEqual(oauth_mod._decode_session(session_id)["device_code"], "device-1")

        tampered = ("A" if session_id[0] != "A" else "B") + session_id[1:]
        with self.assertRaises(ValidationError):
            oauth_mod._decode_session(tampered)

    def test_expired_session_is_rejected(self):
        session_id = oauth_mod._encode_session({"exp": int(time.time()) - 1})

        with self.assertRaises(ValidationError) as caught:
            oauth_mod._decode_session(session_id)

        self.assertEqual(caught.exception.code, "oauth_session_expired")


class OAuthAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = LocalAccountRepository(Path(self.tmp.name) / "accounts.db")
        await self.repo.initialize()
        self.service = GrokOAuthService(self.repo)

    async def asyncTearDown(self):
        await self.repo.close()
        self.tmp.cleanup()

    async def test_device_start_and_pending_do_not_expose_device_code(self):
        start_payload = {
            "device_code": "secret-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "expires_in": 900,
            "interval": 5,
        }
        with patch.object(
            oauth_mod,
            "post_oauth_form",
            AsyncMock(
                side_effect=[
                    (200, start_payload),
                    (400, {"error": "authorization_pending"}),
                ]
            ),
        ):
            started = await self.service.start_device_login()
            pending = await self.service.poll_device_login(started["session_id"])

        self.assertNotIn("device_code", started)
        self.assertEqual(started["status"], "pending")
        self.assertEqual(pending, {"status": "pending", "interval": 5})

    async def test_successful_import_is_stable_private_and_updates_in_place(self):
        access_1 = _jwt(sub="user-1", email="user@example.com", team_id="team-1")
        token_payload_1 = {
            "access_token": access_1,
            "refresh_token": "refresh-secret-1",
            "id_token": _jwt(sub="user-1", email="user@example.com"),
            "expires_in": 3600,
        }
        with (
            patch.object(
                oauth_mod,
                "post_oauth_form",
                AsyncMock(return_value=(200, token_payload_1)),
            ),
            patch.object(
                oauth_mod,
                "get_oauth_json",
                AsyncMock(side_effect=_metadata),
            ),
        ):
            first = await self.service.poll_device_login(_session())

        account_id = first["account_id"]
        record = (await self.repo.get_accounts([account_id]))[0]
        self.assertEqual(first["status"], "success")
        self.assertFalse(first["updated"])
        self.assertEqual(record.tags, ["grok-build", "oauth"])
        self.assertEqual(record.ext["oauth_access_token"], access_1)
        self.assertEqual(record.ext["oauth_refresh_token"], "refresh-secret-1")
        self.assertEqual(record.ext["oauth_subscription_label"], "SuperGrok")
        self.assertEqual(record.ext["oauth_credit_usage_percent"], 5)
        self.assertEqual(
            record.ext["oauth_build_models"],
            ["grok-4.5", "grok-composer-2.5-fast"],
        )
        self.assertEqual(
            oauth_model_ids(record),
            frozenset({"grok-4.5", "grok-composer-2.5-fast", "grok-4.3"}),
        )

        response = await admin_tokens.list_tokens(repo=self.repo)
        public_body = response.body.decode()
        self.assertIn('"auth_type":"oauth"', public_body)
        self.assertIn('"quota":{}', public_body)
        self.assertNotIn(access_1, public_body)
        self.assertNotIn("refresh-secret-1", public_body)

        await self.repo.patch_accounts(
            [
                AccountPatch(
                    token=account_id,
                    usage_use_delta=7,
                    usage_fail_delta=2,
                )
            ]
        )
        access_2 = _jwt(sub="user-1", email="user@example.com", team_id="team-1")
        with (
            patch.object(
                oauth_mod,
                "post_oauth_form",
                AsyncMock(
                    return_value=(
                        200,
                        {
                            **token_payload_1,
                            "access_token": access_2,
                            "refresh_token": "refresh-secret-2",
                        },
                    )
                ),
            ),
            patch.object(
                oauth_mod,
                "get_oauth_json",
                AsyncMock(side_effect=_metadata),
            ),
        ):
            second = await self.service.poll_device_login(_session("device-2"))

        updated = (await self.repo.get_accounts([account_id]))[0]
        self.assertTrue(second["updated"])
        self.assertEqual(second["account_id"], account_id)
        self.assertEqual(updated.usage_use_count, 7)
        self.assertEqual(updated.usage_fail_count, 0)
        self.assertEqual(updated.ext["oauth_refresh_token"], "refresh-secret-2")
        self.assertEqual(updated.state_reason, "oauth_reauthorized")

    async def test_refresh_rotation_and_terminal_failure_are_persisted_once(self):
        account_id = "oauth:refresh-test"
        await self.repo.upsert_accounts(
            [
                AccountUpsert(
                    token=account_id,
                    tags=["oauth", "grok-build"],
                    ext={
                        "oauth_access_token": "expired",
                        "oauth_refresh_token": "refresh-old",
                        "oauth_expires_at": 0,
                    },
                )
            ]
        )
        with patch.object(
            oauth_mod,
            "post_oauth_form",
            AsyncMock(
                return_value=(
                    200,
                    {
                        "access_token": "access-new",
                        "refresh_token": "refresh-new",
                        "expires_in": 3600,
                    },
                )
            ),
        ):
            self.assertEqual(
                await self.service.access_token(account_id, force_refresh=True),
                "access-new",
            )

        refreshed = (await self.repo.get_accounts([account_id]))[0]
        self.assertEqual(refreshed.ext["oauth_refresh_token"], "refresh-new")
        self.assertEqual(refreshed.state_reason, "oauth_refreshed")

        with patch.object(
            oauth_mod,
            "post_oauth_form",
            AsyncMock(
                return_value=(
                    400,
                    {"error": "invalid_grant", "error_description": "expired"},
                )
            ),
        ):
            with self.assertRaises(UpstreamError):
                await self.service.access_token(account_id, force_refresh=True)

        expired = (await self.repo.get_accounts([account_id]))[0]
        self.assertEqual(expired.status, AccountStatus.EXPIRED)
        self.assertEqual(expired.state_reason, "invalid_grant")
        self.assertEqual(expired.usage_fail_count, 1)

    async def test_acquire_skips_broken_oauth_account(self):
        await self.repo.upsert_accounts(
            [
                AccountUpsert(
                    token="oauth:broken",
                    tags=["oauth"],
                    ext={"oauth_access_token": "expired", "oauth_expires_at": 0},
                ),
                AccountUpsert(
                    token="oauth:healthy",
                    tags=["oauth"],
                    ext={
                        "oauth_access_token": "healthy-access",
                        "oauth_refresh_token": "healthy-refresh",
                        "oauth_expires_at": oauth_mod.now_ms() + 3_600_000,
                    },
                ),
            ]
        )

        lease = await self.service.acquire()
        await self.service.release(lease)

        self.assertEqual(lease.account_id, "oauth:healthy")
        broken = (await self.repo.get_accounts(["oauth:broken"]))[0]
        self.assertEqual(broken.status, AccountStatus.EXPIRED)
        self.assertEqual(broken.usage_fail_count, 1)

    async def test_runtime_and_sso_selectors_isolate_oauth_accounts(self):
        await self.repo.upsert_accounts(
            [AccountUpsert(token="oauth:isolation", tags=["oauth"])]
        )
        record = (await self.repo.get_accounts(["oauth:isolation"]))[0]
        args = _record_to_slot_args(record)

        self.assertFalse(is_sso_maintainable(record))
        self.assertTrue(
            all(
                args[key] == -1
                for key in (
                    "quota_auto",
                    "quota_fast",
                    "quota_expert",
                    "quota_heavy",
                    "quota_grok_4_3",
                    "quota_console",
                )
            )
        )
        self.assertTrue(
            all(
                value == 0
                for key, value in args.items()
                if key.startswith(("total_", "window_", "reset_"))
            )
        )

    async def test_no_oauth_account_is_a_rate_limit_error(self):
        with self.assertRaises(RateLimitError):
            await self.service.acquire()

    async def test_oauth_cooldown_becomes_manageable_after_deadline(self):
        await self.repo.upsert_accounts(
            [
                AccountUpsert(
                    token="oauth:cooldown",
                    tags=["oauth"],
                    ext={"cooldown_until": oauth_mod.now_ms() - 1},
                )
            ]
        )
        await self.repo.patch_accounts(
            [
                AccountPatch(
                    token="oauth:cooldown",
                    status=AccountStatus.COOLING,
                )
            ]
        )
        record = (await self.repo.get_accounts(["oauth:cooldown"]))[0]

        self.assertTrue(is_oauth_manageable(record))


class GrokBuildAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_catalog_selects_build_or_api_upstream(self):
        lease = OAuthAccountLease(
            account_id="oauth:test",
            access_token="secret",
            build_models=frozenset({"grok-4.5", "grok-composer-2.5-fast"}),
            api_models=frozenset({"grok-4.5", "grok-4.3"}),
            language_models=frozenset({"grok-4.5", "grok-4.3"}),
        )

        self.assertEqual(grok_build._responses_url(lease, "grok-4.5"), BUILD_RESPONSES_URL)
        self.assertEqual(grok_build._responses_url(lease, "grok-4.3"), API_RESPONSES_URL)
        payload = {"model": "grok-4.3", "reasoning": {"effort": "low"}}
        self.assertNotIn(
            "reasoning",
            grok_build._upstream_payload(payload, API_RESPONSES_URL),
        )
        self.assertIs(
            grok_build._upstream_payload(payload, BUILD_RESPONSES_URL),
            payload,
        )
        with self.assertRaises(UpstreamError):
            grok_build._responses_url(lease, "unknown")

    async def test_registry_reuses_shared_model_ids_for_sso_and_oauth(self):
        shared = model_registry.resolve("grok-4.20-0309-reasoning")

        self.assertTrue(shared.is_chat())
        self.assertTrue(shared.is_oauth_chat())
        self.assertTrue(model_registry.resolve("grok-composer-2.5-fast").is_oauth_chat())
        self.assertTrue(model_registry.resolve("grok-build-0.1").is_oauth_chat())

    async def test_chat_conversion_preserves_tools_and_sse_events(self):
        body = grok_build._chat_to_responses(
            model="grok-4.5",
            messages=[
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "weather"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "weather", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "sunny"},
            ],
            stream=True,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "weather"}},
            temperature=0,
            top_p=0,
            reasoning_effort="minimal",
            max_tokens=64,
        )

        self.assertEqual(body["instructions"], "Be concise")
        self.assertEqual(body["tools"][0]["name"], "weather")
        self.assertEqual(body["tool_choice"], {"type": "function", "name": "weather"})
        self.assertEqual(body["reasoning"], {"effort": "minimal"})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["top_p"], 0)

        async def lines():
            yield "event: response.output_text.delta"
            yield 'data: {"delta":"hi"}'
            yield ""

        self.assertEqual(
            [item async for item in grok_build._events(lines())],
            [("response.output_text.delta", {"delta": "hi"})],
        )


class OAuthSurfaceTests(unittest.TestCase):
    def test_admin_page_and_openapi_expose_oauth_without_secrets(self):
        html = (_ROOT / "app/statics/admin/account.html").read_text(encoding="utf-8")

        self.assertIn("openOAuth()", html)
        self.assertIn("'/oauth/device/start'", html)
        self.assertIn("`/oauth/device/${encodeURIComponent(_oauthSession)}`", html)
        self.assertIn("'/oauth/accounts/metadata'", html)
        self.assertIn("oauthQuotaHtml(t)", html)
        self.assertNotIn("oauth_access_token", html)
        self.assertNotIn("oauth_refresh_token", html)

        from app.main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/admin/api/oauth/device/start", paths)
        self.assertIn("/admin/api/oauth/device/{session_id}", paths)
        self.assertIn("/admin/api/oauth/accounts/metadata", paths)


if __name__ == "__main__":
    unittest.main()
