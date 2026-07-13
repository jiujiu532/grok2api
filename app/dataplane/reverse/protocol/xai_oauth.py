"""xAI OAuth and Grok Build Responses transport."""

from collections.abc import AsyncGenerator
from typing import Any

import orjson

from app.control.proxy.models import (
    ProxyFeedback,
    ProxyFeedbackKind,
)
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.session import ResettableSession
from app.platform.errors import UpstreamError

OAUTH_ISSUER = "https://auth.x.ai"
OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OAUTH_DEVICE_URL = f"{OAUTH_ISSUER}/oauth2/device/code"
OAUTH_TOKEN_URL = f"{OAUTH_ISSUER}/oauth2/token"
OAUTH_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access "
    "conversations:read conversations:write"
)
BUILD_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
BUILD_RESPONSES_URL = f"{BUILD_BASE_URL}/responses"
BUILD_MODELS_URL = f"{BUILD_BASE_URL}/models"
BUILD_USER_URL = f"{BUILD_BASE_URL}/user?include=subscription"
BUILD_BILLING_URL = f"{BUILD_BASE_URL}/billing?format=credits"
API_BASE_URL = "https://api.x.ai/v1"
API_RESPONSES_URL = f"{API_BASE_URL}/responses"
API_MODELS_URL = f"{API_BASE_URL}/models"
API_LANGUAGE_MODELS_URL = f"{API_BASE_URL}/language-models"

_MAX_JSON_BYTES = 1 << 20
_MAX_ERROR_BYTES = 4096


def build_oauth_headers(
    *,
    access_token: str = "",
    model: str = "",
    conv_id: str = "",
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "grok-pager/0.2.93 grok-shell/0.2.93 (linux; x86_64)",
    }
    if access_token:
        headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-grok-client-version": "0.2.93",
                "x-grok-client-identifier": "grok2api",
            }
        )
    if model:
        headers["x-grok-model-override"] = model
    if conv_id:
        headers["x-grok-conv-id"] = str(conv_id)[:200]
    return headers


def _proxy_kind(status: int | None) -> ProxyFeedbackKind:
    if status == 401:
        return ProxyFeedbackKind.UNAUTHORIZED
    if status == 403:
        return ProxyFeedbackKind.FORBIDDEN
    if status == 429:
        return ProxyFeedbackKind.RATE_LIMITED
    if status and status >= 500:
        return ProxyFeedbackKind.UPSTREAM_5XX
    return ProxyFeedbackKind.TRANSPORT_ERROR


async def post_oauth_form(
    url: str,
    data: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    """POST a form to a fixed xAI OAuth endpoint and return JSON without redirects."""
    if url not in {OAUTH_DEVICE_URL, OAUTH_TOKEN_URL}:
        raise ValueError("untrusted xAI OAuth endpoint")

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(clearance_origin=OAUTH_ISSUER)
    try:
        async with ResettableSession(lease=lease) as session:
            response = await session.post(
                url,
                data=data,
                headers={
                    **build_oauth_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=False,
            )
            raw = bytes(response.content or b"")
            if len(raw) > _MAX_JSON_BYTES:
                raise UpstreamError("xAI OAuth response is too large", status=502)
            try:
                payload = orjson.loads(raw) if raw else {}
            except orjson.JSONDecodeError as exc:
                raise UpstreamError(
                    "xAI OAuth returned invalid JSON",
                    status=502,
                    body=raw[:_MAX_ERROR_BYTES].decode("utf-8", "replace"),
                ) from exc
        await proxy.feedback(
            lease,
            ProxyFeedback(
                kind=(
                    ProxyFeedbackKind.SUCCESS
                    if response.status_code < 500
                    else ProxyFeedbackKind.UPSTREAM_5XX
                ),
                status_code=response.status_code,
            ),
        )
        return response.status_code, payload if isinstance(payload, dict) else {}
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=_proxy_kind(status), status_code=status),
        )
        raise


async def get_oauth_json(
    url: str,
    access_token: str,
    *,
    timeout_s: float = 30,
) -> dict[str, Any]:
    """GET a fixed xAI OAuth metadata endpoint."""
    allowed = {
        BUILD_MODELS_URL,
        BUILD_USER_URL,
        BUILD_BILLING_URL,
        API_MODELS_URL,
        API_LANGUAGE_MODELS_URL,
    }
    if url not in allowed:
        raise ValueError("untrusted xAI OAuth metadata endpoint")

    origin = f"{url.split('/', 3)[0]}//{url.split('/', 3)[2]}"
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(clearance_origin=origin)
    try:
        async with ResettableSession(lease=lease) as session:
            response = await session.get(
                url,
                headers=build_oauth_headers(access_token=access_token),
                timeout=timeout_s,
                allow_redirects=False,
            )
            raw = bytes(response.content or b"")
            if len(raw) > _MAX_JSON_BYTES:
                raise UpstreamError("xAI OAuth metadata response is too large", status=502)
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamError(
                    f"xAI OAuth metadata returned {response.status_code}",
                    status=response.status_code,
                    body=raw[:_MAX_ERROR_BYTES].decode("utf-8", "replace"),
                )
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise UpstreamError(
                    "xAI OAuth metadata returned invalid JSON",
                    status=502,
                    body=raw[:_MAX_ERROR_BYTES].decode("utf-8", "replace"),
                ) from exc
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=_proxy_kind(status), status_code=status),
        )
        raise


async def request_oauth_responses(
    access_token: str,
    payload: dict[str, Any],
    *,
    url: str,
    timeout_s: float,
    conv_id: str = "",
) -> dict[str, Any]:
    """Call a fixed xAI OAuth Responses endpoint and return one JSON response."""
    if url not in {BUILD_RESPONSES_URL, API_RESPONSES_URL}:
        raise ValueError("untrusted xAI OAuth Responses endpoint")

    model = str(payload.get("model") or "")
    origin = f"{url.split('/', 3)[0]}//{url.split('/', 3)[2]}"
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(clearance_origin=origin)
    try:
        async with ResettableSession(lease=lease) as session:
            response = await session.post(
                url,
                data=orjson.dumps(payload),
                headers={
                    **build_oauth_headers(
                        access_token=access_token,
                        model=model if url == BUILD_RESPONSES_URL else "",
                        conv_id=conv_id,
                    ),
                    "Content-Type": "application/json",
                },
                timeout=timeout_s,
                allow_redirects=False,
            )
            raw = bytes(response.content or b"")
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamError(
                    f"xAI OAuth upstream returned {response.status_code}",
                    status=response.status_code,
                    body=raw[:_MAX_ERROR_BYTES].decode("utf-8", "replace"),
                )
            try:
                body = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise UpstreamError(
                    "xAI OAuth upstream returned invalid JSON",
                    status=502,
                    body=raw[:_MAX_ERROR_BYTES].decode("utf-8", "replace"),
                ) from exc
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )
        return body if isinstance(body, dict) else {}
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=_proxy_kind(status), status_code=status),
        )
        raise


async def stream_oauth_responses(
    access_token: str,
    payload: dict[str, Any],
    *,
    url: str,
    timeout_s: float,
    conv_id: str = "",
) -> AsyncGenerator[str, None]:
    """Yield raw SSE lines from a fixed xAI OAuth Responses endpoint."""
    if url not in {BUILD_RESPONSES_URL, API_RESPONSES_URL}:
        raise ValueError("untrusted xAI OAuth Responses endpoint")

    model = str(payload.get("model") or "")
    origin = f"{url.split('/', 3)[0]}//{url.split('/', 3)[2]}"
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(clearance_origin=origin)
    try:
        async with ResettableSession(lease=lease) as session:
            response = await session.post(
                url,
                data=orjson.dumps(payload),
                headers={
                    **build_oauth_headers(
                        access_token=access_token,
                        model=model if url == BUILD_RESPONSES_URL else "",
                        conv_id=conv_id,
                    ),
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                },
                timeout=timeout_s,
                stream=True,
                allow_redirects=False,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raw = bytes(response.content or b"")
                raise UpstreamError(
                    f"xAI OAuth upstream returned {response.status_code}",
                    status=response.status_code,
                    body=raw[:_MAX_ERROR_BYTES].decode("utf-8", "replace"),
                )
            async for line in response.aiter_lines():
                yield line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=_proxy_kind(status), status_code=status),
        )
        raise


__all__ = [
    "OAUTH_ISSUER",
    "OAUTH_CLIENT_ID",
    "OAUTH_DEVICE_URL",
    "OAUTH_TOKEN_URL",
    "OAUTH_SCOPE",
    "BUILD_BASE_URL",
    "BUILD_RESPONSES_URL",
    "BUILD_MODELS_URL",
    "BUILD_USER_URL",
    "BUILD_BILLING_URL",
    "API_BASE_URL",
    "API_RESPONSES_URL",
    "API_MODELS_URL",
    "API_LANGUAGE_MODELS_URL",
    "build_oauth_headers",
    "post_oauth_form",
    "get_oauth_json",
    "request_oauth_responses",
    "stream_oauth_responses",
]
