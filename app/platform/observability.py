"""Request IDs and lightweight process-local API metrics."""

import re
import os
import resource
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.logging.logger import logger
from app.platform.metrics_store import HTTPMetricsStore
from app.platform.paths import data_dir

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")


def request_id(value: str | None) -> str:
    """Return a log-safe caller ID or generate a new one."""
    candidate = (value or "").strip()
    if _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return f"req_{secrets.token_hex(12)}"


def _is_public_api(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


class HTTPMetrics:
    """Small in-memory counter set for the current worker process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._requests = 0
        self._successes = 0
        self._client_errors = 0
        self._server_errors = 0
        self._active = 0
        self._latency_ms = 0.0
        self._last_request_at: float | None = None
        self._history: deque[dict[str, float | int]] = deque(maxlen=120)
        self._store: HTTPMetricsStore | None = None
        self._store_warning_logged = False

    def enable_persistence(self, path: Path) -> None:
        if self._store is not None:
            self._store.close()
        self._store = HTTPMetricsStore(path)
        self._store_warning_logged = False

    def close_persistence(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def storage_health(self) -> dict[str, Any]:
        if self._store is None:
            return {"status": "disabled"}
        try:
            return self._store.health()
        except (OSError, sqlite3.Error) as exc:
            return {"status": "error", "error": type(exc).__name__}

    def begin(self) -> None:
        with self._lock:
            self._active += 1

    def finish(
        self,
        status: int,
        latency_ms: float,
        *,
        request_id: str = "",
        method: str = "",
        path: str = "",
    ) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._requests += 1
            self._latency_ms += latency_ms
            now = time.time()
            self._last_request_at = now
            if status < 400:
                self._successes += 1
            elif status < 500:
                self._client_errors += 1
            else:
                self._server_errors += 1
            minute = int(now // 60) * 60
            if not self._history or self._history[-1]["timestamp"] != minute:
                self._history.append(
                    {
                        "timestamp": minute,
                        "requests": 0,
                        "errors": 0,
                        "latency_ms": 0.0,
                    }
                )
            bucket = self._history[-1]
            bucket["requests"] += 1
            bucket["latency_ms"] += latency_ms
            if status >= 400:
                bucket["errors"] += 1
        if self._store is not None:
            try:
                # ponytail: synchronous two-row SQLite write; batch behind a
                # queue only if profiling shows request-tail contention.
                self._store.record(
                    now,
                    status,
                    latency_ms,
                    request_id=request_id,
                    method=method,
                    path=path,
                )
            except (OSError, sqlite3.Error) as exc:
                if not self._store_warning_logged:
                    logger.warning(
                        "persistent HTTP metrics write failed: error_type={}",
                        type(exc).__name__,
                    )
                    self._store_warning_logged = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            started_at = self._started_at
            errors = self._client_errors + self._server_errors
            history_by_minute = {
                int(bucket["timestamp"]): bucket.copy()
                for bucket in self._history
            }
            current_minute = int(time.time() // 60) * 60
            history = []
            for timestamp in range(
                current_minute - 29 * 60,
                current_minute + 1,
                60,
            ):
                bucket = history_by_minute.get(timestamp)
                requests = int(bucket["requests"]) if bucket else 0
                latency_total = float(bucket["latency_ms"]) if bucket else 0.0
                history.append(
                    {
                        "timestamp": timestamp,
                        "requests": requests,
                        "errors": int(bucket["errors"]) if bucket else 0,
                        "average_latency_ms": round(
                            latency_total / requests,
                            1,
                        ) if requests else 0.0,
                    }
                )
            fallback = {
                "scope": "process",
                "started_at": started_at,
                "uptime_seconds": max(0, int(time.time() - started_at)),
                "requests": self._requests,
                "successes": self._successes,
                "client_errors": self._client_errors,
                "server_errors": self._server_errors,
                "active_requests": active,
                "average_latency_ms": round(
                    self._latency_ms / self._requests, 1
                ) if self._requests else 0.0,
                "error_rate": round(errors * 100 / self._requests, 2)
                if self._requests else 0.0,
                "last_request_at": self._last_request_at,
                "history": history,
            }
        if self._store is None:
            return fallback
        try:
            persistent = self._store.snapshot()
        except (OSError, sqlite3.Error):
            return fallback
        persistent.update(
            {
                "started_at": started_at,
                "uptime_seconds": max(0, int(time.time() - started_at)),
                "active_requests": active,
            }
        )
        errors = persistent["client_errors"] + persistent["server_errors"]
        persistent["error_rate"] = (
            round(errors * 100 / persistent["requests"], 2)
            if persistent["requests"]
            else 0.0
        )
        return persistent


http_metrics = HTTPMetrics()


class ProcessHealth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_wall = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._last_cpu = usage.ru_utime + usage.ru_stime

    @staticmethod
    def _rss_bytes() -> int:
        statm = Path("/proc/self/statm")
        if statm.exists():
            pages = int(statm.read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(os.getpid())],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=True,
                )
                return int(result.stdout.strip() or 0) * 1024
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu = usage.ru_utime + usage.ru_stime
        with self._lock:
            elapsed = max(now - self._last_wall, 0.001)
            cpu_percent = max(0.0, (cpu - self._last_cpu) * 100 / elapsed)
            self._last_wall = now
            self._last_cpu = cpu
        cpu_count = os.cpu_count() or 1
        load_1m, load_5m, load_15m = os.getloadavg()
        rss = self._rss_bytes()
        try:
            memory_total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            memory_total = 0
        disk = shutil.disk_usage(data_dir())
        return {
            "pid": os.getpid(),
            "cpu_count": cpu_count,
            "process_cpu_percent": round(cpu_percent, 1),
            "load_1m": round(load_1m, 2),
            "load_5m": round(load_5m, 2),
            "load_15m": round(load_15m, 2),
            "load_percent": round(load_1m * 100 / cpu_count, 1),
            "memory_rss_bytes": rss,
            "memory_total_bytes": int(memory_total),
            "memory_percent": (
                round(rss * 100 / memory_total, 2) if memory_total else None
            ),
            "disk_total_bytes": disk.total,
            "disk_used_bytes": disk.used,
            "disk_free_bytes": disk.free,
            "disk_percent": round(disk.used * 100 / disk.total, 1),
        }


process_health = ProcessHealth()


class RequestObservabilityMiddleware:
    """Track full request lifetimes without buffering streaming responses."""

    def __init__(self, app: ASGIApp, metrics: HTTPMetrics | None = None) -> None:
        self.app = app
        self.metrics = metrics or http_metrics

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        req_id = request_id(headers.get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = req_id
        path = str(scope.get("path") or "")
        tracked = _is_public_api(path)
        status = 500
        started = time.perf_counter()

        if tracked:
            self.metrics.begin()

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = req_id
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("X-Frame-Options", "DENY")
                response_headers.setdefault("Referrer-Policy", "same-origin")
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            if tracked:
                self.metrics.finish(
                    status,
                    latency_ms,
                    request_id=req_id,
                    method=str(scope.get("method") or ""),
                    path=path,
                )
                logger.info(
                    "api request completed: request_id={} method={} path={} "
                    "status={} latency_ms={:.1f}",
                    req_id,
                    scope.get("method", ""),
                    path,
                    status,
                    latency_ms,
                )


__all__ = [
    "HTTPMetrics",
    "RequestObservabilityMiddleware",
    "http_metrics",
    "process_health",
    "request_id",
]
