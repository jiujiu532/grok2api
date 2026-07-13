"""Admin endpoints for xAI OAuth device login."""

from fastapi import APIRouter, Request

from app.control.account.oauth import GrokOAuthService
from app.platform.errors import AppError, ErrorKind

router = APIRouter(prefix="/oauth", tags=["Admin - OAuth"])


def _service(request: Request) -> GrokOAuthService:
    service = getattr(request.app.state, "oauth_service", None)
    if not isinstance(service, GrokOAuthService):
        raise AppError(
            "OAuth service is not initialised",
            kind=ErrorKind.SERVER,
            code="oauth_not_initialised",
            status=503,
        )
    return service


@router.post("/device/start")
async def start_device_login(request: Request):
    return await _service(request).start_device_login()


@router.get("/device/{session_id}")
async def poll_device_login(session_id: str, request: Request):
    result = await _service(request).poll_device_login(session_id)
    if result.get("status") == "success":
        directory = getattr(request.app.state, "directory", None)
        if directory is not None:
            await directory.sync_if_changed()
    return result


@router.get("/accounts/metadata")
async def account_metadata(request: Request):
    return {"accounts": await _service(request).account_metadata()}


__all__ = ["router"]
