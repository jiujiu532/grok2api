"""Grok OAuth device login, credential refresh, and account selection."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

import orjson

from app.dataplane.reverse.protocol.xai_oauth import (
    API_LANGUAGE_MODELS_URL,
    API_MODELS_URL,
    BUILD_BILLING_URL,
    BUILD_MODELS_URL,
    BUILD_USER_URL,
    OAUTH_CLIENT_ID,
    OAUTH_DEVICE_URL,
    OAUTH_ISSUER,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    get_oauth_json,
    post_oauth_form,
)
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, RateLimitError, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms

from .commands import AccountPatch, AccountUpsert
from .enums import AccountStatus
from .models import AccountRecord
from .repository import AccountRepository

_SESSION_MAX_AGE_S = 15 * 60
_ACCESS_REFRESH_SKEW_MS = 120_000
_METADATA_MAX_AGE_MS = 5 * 60 * 1000
_OAUTH_TAG = "oauth"
_TERMINAL_REFRESH_ERRORS = {
    "access_denied",
    "invalid_client",
    "invalid_grant",
    "refresh_token_reused",
    "unauthorized_client",
}


@dataclass(slots=True)
class OAuthAccountLease:
    account_id: str
    access_token: str
    build_models: frozenset[str]
    api_models: frozenset[str]
    language_models: frozenset[str]


def is_oauth_record(record: AccountRecord) -> bool:
    return _OAUTH_TAG in record.tags


def is_oauth_manageable(record: AccountRecord, *, now: int | None = None) -> bool:
    if not is_oauth_record(record) or record.is_deleted():
        return False
    if record.status == AccountStatus.ACTIVE:
        return True
    if record.status != AccountStatus.COOLING:
        return False
    cooldown_until = int(
        record.ext.get("cooldown_until")
        or record.ext.get("oauth_cooldown_until")
        or 0
    )
    return bool(cooldown_until and cooldown_until <= (now if now is not None else now_ms()))


def oauth_model_sets(
    record: AccountRecord,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    ext = record.ext

    def values(key: str) -> frozenset[str]:
        raw = ext.get(key)
        return frozenset(str(item) for item in raw if item) if isinstance(raw, list) else frozenset()

    build = values("oauth_build_models")
    api = values("oauth_api_models")
    language = values("oauth_language_models")
    if not build and not api and not language and not ext.get("oauth_metadata_synced_at"):
        build = frozenset({"grok-4.5", "grok-composer-2.5-fast"})
    return build, api, language


def oauth_model_ids(record: AccountRecord) -> frozenset[str]:
    build, api, language = oauth_model_sets(record)
    return build | api | language


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _session_secret() -> bytes:
    app_key = str(get_config("app.app_key", "grok2api") or "grok2api")
    return hashlib.sha256(f"grok2api:xai-oauth:{app_key}".encode()).digest()


def _encode_session(payload: dict[str, Any]) -> str:
    body = _b64encode(orjson.dumps(payload))
    signature = _b64encode(hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _decode_session(session_id: str) -> dict[str, Any]:
    try:
        body, signature = session_id.split(".", 1)
        expected = hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            raise ValueError("signature mismatch")
        payload = orjson.loads(_b64decode(body))
        expires_at = int(payload.get("exp") or 0) if isinstance(payload, dict) else 0
    except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
        raise ValidationError(
            "OAuth 会话无效，请重新发起登录",
            param="session_id",
            code="oauth_session_invalid",
        ) from exc
    if not isinstance(payload, dict) or expires_at <= int(time.time()):
        raise ValidationError(
            "OAuth 会话已过期，请重新发起登录",
            param="session_id",
            code="oauth_session_expired",
        )
    return payload


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".", 2)[1]
        parsed = orjson.loads(_b64decode(payload))
        return parsed if isinstance(parsed, dict) else {}
    except (IndexError, ValueError, orjson.JSONDecodeError):
        return {}


def _oauth_account_id(claims: dict[str, Any], refresh_token: str) -> str:
    identity = (
        str(claims.get("team_id") or "").strip()
        or str(claims.get("sub") or claims.get("user_id") or "").strip()
        or str(claims.get("email") or "").strip().lower()
        or hashlib.sha256(refresh_token.encode()).hexdigest()
    )
    digest = hashlib.sha256(f"{OAUTH_ISSUER}|{OAUTH_CLIENT_ID}|{identity}".encode()).hexdigest()
    return f"oauth:{digest[:40]}"


def _token_error(payload: dict[str, Any], fallback: str) -> str:
    return str(payload.get("error_description") or payload.get("error") or fallback).replace(
        "\n", " "
    )[:300]


def _model_ids(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("data") or payload.get("models") or []
    if not isinstance(rows, list):
        return []
    ids = []
    for row in rows:
        value = row.get("id") or row.get("name") if isinstance(row, dict) else row
        if value:
            ids.append(str(value))
    return list(dict.fromkeys(ids))


def _subscription_label(raw: str) -> str:
    normalized = "".join(ch for ch in raw.lower() if ch.isalnum())
    if normalized in {"grokpro", "subscriptiontiergrokpro", "supergrok"}:
        return "SuperGrok"
    if "heavy" in normalized or "supergrokpro" in normalized:
        return "SuperGrok Heavy"
    if "lite" in normalized:
        return "SuperGrok Lite"
    return raw


def _public_metadata(record: AccountRecord) -> dict[str, Any]:
    ext = record.ext
    return {
        "account_id": record.token,
        "oauth_plan": str(ext.get("oauth_subscription_label") or ""),
        "oauth_plan_raw": str(ext.get("oauth_subscription_tier") or ""),
        "oauth_has_grok_code_access": bool(ext.get("oauth_has_grok_code_access")),
        "oauth_usage_percent": ext.get("oauth_credit_usage_percent"),
        "oauth_period_start": ext.get("oauth_billing_period_start"),
        "oauth_period_end": ext.get("oauth_billing_period_end"),
        "oauth_product_usage": ext.get("oauth_product_usage") or [],
        "oauth_build_models": ext.get("oauth_build_models") or [],
        "oauth_api_models": ext.get("oauth_api_models") or [],
        "oauth_language_models": ext.get("oauth_language_models") or [],
        "oauth_metadata_synced_at": ext.get("oauth_metadata_synced_at"),
    }


class GrokOAuthService:
    """Owns OAuth credentials while reusing the existing account repository."""

    def __init__(self, repository: AccountRepository) -> None:
        self._repo = repository
        self._lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, int] = {}

    async def start_device_login(self) -> dict[str, Any]:
        status, payload = await post_oauth_form(
            OAUTH_DEVICE_URL,
            {"client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPE},
        )
        if status != 200:
            raise UpstreamError(
                f"xAI OAuth 登录初始化失败：{_token_error(payload, str(status))}",
                status=502,
            )
        required = ("device_code", "user_code", "verification_uri", "expires_in")
        if any(not payload.get(key) for key in required):
            raise UpstreamError("xAI OAuth 返回缺少必要字段", status=502)

        expires_in = min(max(int(payload["expires_in"]), 60), _SESSION_MAX_AGE_S)
        interval = min(max(int(payload.get("interval") or 5), 1), 30)
        session_id = _encode_session(
            {
                "device_code": str(payload["device_code"]),
                "exp": int(time.time()) + expires_in,
                "interval": interval,
                "nonce": secrets.token_hex(8),
            }
        )
        return {
            "status": "pending",
            "session_id": session_id,
            "user_code": str(payload["user_code"]),
            "verification_uri": str(payload["verification_uri"]),
            "verification_uri_complete": str(
                payload.get("verification_uri_complete") or payload["verification_uri"]
            ),
            "expires_in": expires_in,
            "interval": interval,
        }

    async def poll_device_login(self, session_id: str) -> dict[str, Any]:
        session = _decode_session(session_id)
        status, payload = await post_oauth_form(
            OAUTH_TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": OAUTH_CLIENT_ID,
                "device_code": str(session["device_code"]),
            },
        )
        error = str(payload.get("error") or "")
        if error in {"authorization_pending", "slow_down"}:
            return {
                "status": "pending",
                "interval": int(session["interval"]) + (5 if error == "slow_down" else 0),
            }
        if error in {"expired_token", "access_denied"}:
            return {"status": "expired" if error == "expired_token" else "denied"}
        if status != 200:
            raise UpstreamError(
                f"xAI OAuth 登录失败：{_token_error(payload, str(status))}",
                status=502,
            )

        access_token = str(payload.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise UpstreamError("xAI OAuth 未返回可持久化凭证", status=502)

        claims = {
            **_jwt_claims(str(payload.get("id_token") or "")),
            **_jwt_claims(access_token),
        }
        account_id = _oauth_account_id(claims, refresh_token)
        expires_in = max(int(payload.get("expires_in") or 3600), 60)
        ext = {
            "auth_type": "oauth",
            "oauth_issuer": OAUTH_ISSUER,
            "oauth_client_id": OAUTH_CLIENT_ID,
            "oauth_access_token": access_token,
            "oauth_refresh_token": refresh_token,
            "oauth_id_token": str(payload.get("id_token") or ""),
            "oauth_expires_at": now_ms() + expires_in * 1000,
            "oauth_email": str(claims.get("email") or ""),
            "oauth_user_id": str(claims.get("sub") or claims.get("user_id") or ""),
            "oauth_team_id": str(claims.get("team_id") or ""),
        }
        existing = await self._repo.get_accounts([account_id])
        if existing:
            await self._repo.patch_accounts(
                [
                    AccountPatch(
                        token=account_id,
                        status=AccountStatus.ACTIVE,
                        tags=[_OAUTH_TAG, "grok-build"],
                        state_reason="oauth_reauthorized",
                        ext_merge=ext,
                        clear_failures=True,
                    )
                ]
            )
        else:
            await self._repo.upsert_accounts(
                [
                    AccountUpsert(
                        token=account_id,
                        pool="basic",
                        tags=[_OAUTH_TAG, "grok-build"],
                        ext=ext,
                    )
                ]
            )
        try:
            metadata = await self.sync_metadata(account_id, access_token=access_token)
        except AppError as exc:
            logger.warning(
                "OAuth account imported but metadata sync failed: account={} error={}",
                account_id,
                exc.message,
            )
            metadata = {}
        return {
            "status": "success",
            "account_id": account_id,
            "email": ext["oauth_email"],
            "updated": bool(existing),
            "metadata": metadata,
        }

    async def sync_metadata(
        self,
        account_id: str,
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = access_token or await self.access_token(account_id)
        urls = (
            BUILD_USER_URL,
            BUILD_BILLING_URL,
            BUILD_MODELS_URL,
            API_MODELS_URL,
            API_LANGUAGE_MODELS_URL,
        )
        results = await asyncio.gather(
            *(get_oauth_json(url, token) for url in urls),
            return_exceptions=True,
        )
        payloads = [
            result if isinstance(result, dict) else {}
            for result in results
        ]
        if not any(payloads):
            first_error = next(
                (result for result in results if isinstance(result, AppError)),
                None,
            )
            if first_error is not None:
                raise first_error
            raise UpstreamError("xAI OAuth 元数据同步失败", status=502)

        user, billing, build_models, api_models, language_models = payloads
        tier = str(user.get("subscriptionTier") or "")
        billing_config = (
            billing.get("config") if isinstance(billing.get("config"), dict) else {}
        )
        period = (
            billing_config.get("currentPeriod")
            if isinstance(billing_config.get("currentPeriod"), dict)
            else {}
        )
        product_usage = billing_config.get("productUsage")
        safe_products = [
            {
                "product": str(item.get("product") or ""),
                **(
                    {"usage_percent": float(item["usagePercent"])}
                    if isinstance(item, dict)
                    and isinstance(item.get("usagePercent"), int | float)
                    else {}
                ),
            }
            for item in product_usage
            if isinstance(item, dict) and item.get("product")
        ] if isinstance(product_usage, list) else []
        usage_percent = billing_config.get("creditUsagePercent")
        if not isinstance(usage_percent, int | float):
            usage_percent = None

        ext_merge = {
            "oauth_subscription_tier": tier,
            "oauth_subscription_label": _subscription_label(tier),
            "oauth_has_grok_code_access": bool(user.get("hasGrokCodeAccess")),
            "oauth_credit_usage_percent": (
                float(usage_percent) if usage_percent is not None else None
            ),
            "oauth_billing_period_start": (
                period.get("start") or billing_config.get("billingPeriodStart")
            ),
            "oauth_billing_period_end": (
                period.get("end") or billing_config.get("billingPeriodEnd")
            ),
            "oauth_product_usage": safe_products,
            "oauth_build_models": _model_ids(build_models),
            "oauth_api_models": _model_ids(api_models),
            "oauth_language_models": _model_ids(language_models),
            "oauth_metadata_synced_at": now_ms(),
        }
        await self._repo.patch_accounts(
            [AccountPatch(token=account_id, ext_merge=ext_merge)]
        )
        record = (await self._repo.get_accounts([account_id]))[0]
        return _public_metadata(record)

    async def account_metadata(self) -> list[dict[str, Any]]:
        snapshot = await self._repo.runtime_snapshot()
        records = [record for record in snapshot.items if is_oauth_record(record)]
        semaphore = asyncio.Semaphore(4)

        async def load(record: AccountRecord) -> dict[str, Any]:
            cached = _public_metadata(record)
            synced_at = int(cached.get("oauth_metadata_synced_at") or 0)
            if synced_at > now_ms() - _METADATA_MAX_AGE_MS:
                return cached
            async with semaphore:
                try:
                    return await self.sync_metadata(record.token)
                except AppError as exc:
                    return {**cached, "error": exc.message}

        return list(await asyncio.gather(*(load(record) for record in records)))

    async def acquire(
        self,
        *,
        model: str = "",
        exclude: set[str] | None = None,
    ) -> OAuthAccountLease:
        attempted = set(exclude or ())
        last_error: AppError | None = None
        while True:
            snapshot = await self._repo.runtime_snapshot()
            now = now_ms()
            candidates = [
                (record, oauth_model_sets(record))
                for record in snapshot.items
                if record.token not in attempted
                and (not model or model in oauth_model_ids(record))
                and is_oauth_manageable(record, now=now)
            ]
            if not candidates:
                if last_error is not None:
                    raise last_error
                raise RateLimitError("没有可用的 OAuth 账户")

            # ponytail: process-local inflight is sufficient for the current
            # single-worker default; persist it if multi-worker skew is measured.
            async with self._lock:
                record, model_sets = min(
                    candidates,
                    key=lambda item: (
                        self._inflight.get(item[0].token, 0),
                        item[0].last_use_at or 0,
                        item[0].usage_fail_count,
                    ),
                )
                self._inflight[record.token] = self._inflight.get(record.token, 0) + 1
            attempted.add(record.token)
            try:
                access_token = await self.access_token(record.token)
            except AppError as exc:
                last_error = exc
                await self.release_id(record.token)
                continue
            return OAuthAccountLease(
                account_id=record.token,
                access_token=access_token,
                build_models=model_sets[0],
                api_models=model_sets[1],
                language_models=model_sets[2],
            )

    async def access_token(self, account_id: str, *, force_refresh: bool = False) -> str:
        lock = self._refresh_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            records = await self._repo.get_accounts([account_id])
            if not records or not is_oauth_record(records[0]):
                raise ValidationError("OAuth 账户不存在", param="account_id")
            record = records[0]
            ext = record.ext
            access_token = str(ext.get("oauth_access_token") or "")
            expires_at = int(ext.get("oauth_expires_at") or 0)
            if (
                not force_refresh
                and access_token
                and expires_at > now_ms() + _ACCESS_REFRESH_SKEW_MS
            ):
                return access_token

            refresh_token = str(ext.get("oauth_refresh_token") or "")
            if not refresh_token:
                await self.expire(account_id, "oauth_refresh_token_missing")
                raise UpstreamError("OAuth 账户缺少 refresh_token", status=401)
            status, payload = await post_oauth_form(
                OAUTH_TOKEN_URL,
                {
                    "grant_type": "refresh_token",
                    "client_id": OAUTH_CLIENT_ID,
                    "refresh_token": refresh_token,
                },
            )
            next_access = str(payload.get("access_token") or "")
            if status != 200 or not next_access:
                error = str(payload.get("error") or "oauth_refresh_failed")
                terminal = error in _TERMINAL_REFRESH_ERRORS
                if terminal:
                    await self.expire(account_id, error)
                raise UpstreamError(
                    f"OAuth 刷新失败：{_token_error(payload, str(status))}",
                    status=401 if terminal else (429 if status == 429 else 502),
                )
            next_refresh = str(payload.get("refresh_token") or "") or refresh_token
            expires_in = max(int(payload.get("expires_in") or 3600), 60)
            await self._repo.patch_accounts(
                [
                    AccountPatch(
                        token=account_id,
                        status=AccountStatus.ACTIVE,
                        state_reason="oauth_refreshed",
                        ext_merge={
                            "oauth_access_token": next_access,
                            "oauth_refresh_token": next_refresh,
                            "oauth_id_token": str(payload.get("id_token") or ext.get("oauth_id_token") or ""),
                            "oauth_expires_at": now_ms() + expires_in * 1000,
                        },
                        clear_failures=True,
                    )
                ]
            )
            return next_access

    async def success(self, lease: OAuthAccountLease) -> None:
        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=lease.account_id,
                    status=AccountStatus.ACTIVE,
                    usage_use_delta=1,
                    last_use_at=now_ms(),
                    state_reason="oauth_ok",
                    ext_merge={"oauth_cooldown_until": 0},
                    clear_failures=True,
                )
            ]
        )

    async def failure(
        self,
        lease: OAuthAccountLease,
        *,
        status: int | None,
        retry_after_s: int = 0,
    ) -> None:
        patch = AccountPatch(
            token=lease.account_id,
            usage_fail_delta=1,
            last_fail_at=now_ms(),
            last_fail_reason=f"oauth_upstream_{status or 'error'}",
        )
        if status == 429:
            patch.status = AccountStatus.COOLING
            patch.state_reason = "oauth_rate_limited"
            patch.ext_merge = {
                "cooldown_until": now_ms() + max(retry_after_s, 60) * 1000,
                "cooldown_reason": "oauth_rate_limited",
            }
        await self._repo.patch_accounts([patch])

    async def expire(self, account_id: str, reason: str) -> None:
        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=account_id,
                    status=AccountStatus.EXPIRED,
                    state_reason=reason[:200],
                    last_fail_at=now_ms(),
                    last_fail_reason=reason[:200],
                    usage_fail_delta=1,
                )
            ]
        )

    async def release(self, lease: OAuthAccountLease) -> None:
        await self.release_id(lease.account_id)

    async def release_id(self, account_id: str) -> None:
        async with self._lock:
            remaining = self._inflight.get(account_id, 0) - 1
            if remaining > 0:
                self._inflight[account_id] = remaining
            else:
                self._inflight.pop(account_id, None)


__all__ = [
    "GrokOAuthService",
    "OAuthAccountLease",
    "is_oauth_manageable",
    "is_oauth_record",
    "oauth_model_ids",
    "oauth_model_sets",
]
