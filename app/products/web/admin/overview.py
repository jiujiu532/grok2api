"""Admin dashboard overview."""

import asyncio
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Request

from app.control.account.backends.factory import get_repository_backend
from app.dataplane.account.selector import current_strategy
from app.platform.meta import get_project_version
from app.platform.observability import http_metrics, process_health

router = APIRouter(tags=["Admin - System"])


def _local_database_health(path: Path) -> dict:
    started = time.perf_counter()
    with sqlite3.connect(path, timeout=2) as conn:
        check = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0])
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    size = sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    return {
        "status": "ok" if check == "ok" else "error",
        "check": check,
        "journal_mode": journal_mode,
        "size_bytes": size,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def _database_health(request: Request) -> dict:
    repo = getattr(request.app.state, "repository", None)
    backend = get_repository_backend()
    if repo is None:
        return {"status": "unknown", "backend": backend}
    started = time.perf_counter()
    try:
        revision = await repo.get_revision()
        result = {
            "status": "ok",
            "backend": backend,
            "revision": revision,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        path = getattr(repo, "_path", None)
        if backend == "local" and path is not None:
            result.update(await asyncio.to_thread(_local_database_health, Path(path)))
        return result
    except Exception as exc:
        return {
            "status": "error",
            "backend": backend,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": type(exc).__name__,
        }


@router.get("/overview")
async def overview(request: Request):
    accounts = await request.app.state.directory.diagnostics()
    system = process_health.snapshot()
    database = await _database_health(request)
    status = accounts["status"]
    available = int(status.get("active", 0))
    manageable = available + int(status.get("cooling", 0))
    if available:
        health = "ok"
    elif manageable or not accounts["total"]:
        health = "degraded"
    else:
        health = "error"
    if database["status"] == "error":
        health = "error"
    return {
        "status": health,
        "version": get_project_version(),
        "accounts": {
            **accounts,
            "available": available,
            "manageable": manageable,
        },
        "runtime": {
            **http_metrics.snapshot(),
            "storage": get_repository_backend(),
            "selection_strategy": current_strategy(),
            "scheduler_leader": bool(
                getattr(request.app.state, "account_refresh_is_leader", False)
            ),
        },
        "system": system,
        "database": database,
        "metrics_database": http_metrics.storage_health(),
    }


__all__ = ["router"]
