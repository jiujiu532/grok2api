import asyncio
import importlib
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.middleware.cors import CORSMiddleware
from app.dataplane.account import AccountDirectory
from app.dataplane.account.table import AccountRuntimeTable
from app.dataplane.shared.enums import PoolId, StatusId
from app.main import app as main_app, create_app
from app.platform.observability import (
    HTTPMetrics,
    RequestObservabilityMiddleware,
    request_id,
)

overview_module = importlib.import_module("app.products.web.admin.overview")
_ROOT = pathlib.Path(__file__).resolve().parents[1]


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_tracks_complete_api_request_and_sets_headers(self):
        metrics = HTTPMetrics()

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await asyncio.sleep(0)
            await send(
                {
                    "type": "http.response.body",
                    "body": b"{}",
                    "more_body": False,
                }
            )

        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/models",
            "raw_path": b"/v1/models",
            "query_string": b"",
            "headers": [(b"x-request-id", b"client-request-1")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }

        await RequestObservabilityMiddleware(app, metrics)(scope, receive, send)

        headers = dict(messages[0]["headers"])
        self.assertEqual(headers[b"x-request-id"], b"client-request-1")
        self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
        self.assertEqual(metrics.snapshot()["requests"], 1)
        self.assertEqual(metrics.snapshot()["active_requests"], 0)
        self.assertEqual(len(metrics.snapshot()["history"]), 30)
        self.assertEqual(
            sum(point["requests"] for point in metrics.snapshot()["history"]),
            1,
        )

    async def test_stream_stays_active_until_final_response_body(self):
        metrics = HTTPMetrics()
        response_started = asyncio.Event()
        release_stream = asyncio.Event()

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            response_started.set()
            await release_stream.wait()
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: [DONE]\n\n",
                    "more_body": False,
                }
            )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/responses",
            "raw_path": b"/v1/responses",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }

        task = asyncio.create_task(
            RequestObservabilityMiddleware(app, metrics)(
                scope,
                receive,
                AsyncMock(),
            )
        )
        await asyncio.wait_for(response_started.wait(), timeout=1)
        self.assertEqual(metrics.snapshot()["active_requests"], 1)
        self.assertEqual(metrics.snapshot()["requests"], 0)

        release_stream.set()
        await task
        self.assertEqual(metrics.snapshot()["active_requests"], 0)
        self.assertEqual(metrics.snapshot()["requests"], 1)

    def test_request_id_rejects_log_injection(self):
        self.assertEqual(request_id("safe-id:1"), "safe-id:1")
        self.assertRegex(request_id("bad\nid"), r"^req_[0-9a-f]{24}$")

    def test_metrics_survive_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "observability.db"
            first = HTTPMetrics()
            first.enable_persistence(path)
            first.begin()
            first.finish(
                200,
                12.5,
                request_id="persisted-request",
                method="GET",
                path="/v1/models",
            )
            first.close_persistence()

            second = HTTPMetrics()
            second.enable_persistence(path)
            snapshot = second.snapshot()
            health = second.storage_health()
            second.close_persistence()

        self.assertEqual(snapshot["scope"], "persistent")
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["successes"], 1)
        self.assertEqual(snapshot["recent_requests"][0]["path"], "/v1/models")
        self.assertEqual(health["status"], "ok")

    def test_cors_exposes_request_id_to_browser_clients(self):
        cors = next(
            middleware
            for middleware in main_app.user_middleware
            if middleware.cls is CORSMiddleware
        )
        self.assertIn("X-Request-ID", cors.kwargs["expose_headers"])

    async def test_unhandled_500_keeps_trace_and_cors_headers(self):
        app = create_app()

        @app.get("/__test_unhandled")
        async def unhandled():
            raise RuntimeError("must not leak")

        messages = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/__test_unhandled",
            "raw_path": b"/__test_unhandled",
            "query_string": b"",
            "headers": [
                (b"origin", b"https://client.example"),
                (b"x-request-id", b"unhandled-smoke"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
            "state": {},
        }

        with patch("app.main.logger.exception") as log_exception, (
            self.assertRaises(RuntimeError)
        ):
            await app(scope, receive, send)

        start = next(
            message
            for message in messages
            if message["type"] == "http.response.start"
        )
        headers = dict(start["headers"])
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        self.assertEqual(start["status"], 500)
        self.assertEqual(headers[b"x-request-id"], b"unhandled-smoke")
        self.assertEqual(
            headers[b"access-control-allow-origin"],
            b"https://client.example",
        )
        self.assertEqual(
            headers[b"access-control-expose-headers"],
            b"X-Request-ID",
        )
        self.assertNotIn(b"must not leak", body)
        self.assertIn("unhandled-smoke", log_exception.call_args.args)


class AccountDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_aggregates_without_exposing_tokens(self):
        table = AccountRuntimeTable()
        directory = AccountDirectory(repository=object())
        directory._table = table

        def add(token, pool, status, auto, console, health, inflight, tags=None):
            idx = table._append_slot(
                token=token,
                pool_id=int(pool),
                status_id=int(status),
                quota_auto=auto,
                quota_fast=0,
                quota_expert=0,
                quota_heavy=0,
                quota_grok_4_3=0,
                quota_console=console,
                total_auto=10,
                total_fast=0,
                total_expert=0,
                total_heavy=0,
                total_grok_4_3=0,
                total_console=10,
                window_auto=3600,
                window_fast=0,
                window_expert=0,
                window_heavy=0,
                window_grok_4_3=0,
                window_console=3600,
                reset_auto=0,
                reset_fast=0,
                reset_expert=0,
                reset_heavy=0,
                reset_grok_4_3=0,
                reset_console=0,
                health=health,
                last_use_s=0,
                last_fail_s=0,
                fail_count=0,
                tags=tags or [],
            )
            table.inflight_by_idx[idx] = inflight

        add("secret-a", PoolId.BASIC, StatusId.ACTIVE, 7, 2, 1.0, 1)
        add("secret-b", PoolId.SUPER, StatusId.COOLING, 3, 0, 0.5, 0, ["oauth"])

        result = await directory.diagnostics()

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["status"]["active"], 1)
        self.assertEqual(result["status"]["cooling"], 1)
        self.assertEqual(result["pools"], {"basic": 1, "super": 1, "heavy": 0})
        self.assertEqual(result["auth_types"], {"sso": 1, "oauth": 1})
        self.assertEqual(result["quota_remaining"]["auto"], 7)
        self.assertEqual(
            result["health_bands"],
            {"healthy": 1, "warning": 1, "critical": 0, "disabled": 0},
        )
        self.assertEqual(result["inflight"], 1)
        self.assertNotIn("secret-a", repr(result))


class AdminOverviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_reports_account_pool_health(self):
        request = types.SimpleNamespace(
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(
                    directory=types.SimpleNamespace(diagnostics=AsyncMock()),
                    account_refresh_is_leader=True,
                    repository=None,
                )
            )
        )
        base = {
            "total": 0,
            "status": {
                "active": 0,
                "cooling": 0,
                "expired": 0,
                "disabled": 0,
            },
            "pools": {"basic": 0, "super": 0, "heavy": 0},
            "quota_remaining": {},
            "inflight": 0,
            "average_health": 0.0,
            "revision": 0,
        }

        with (
            patch.object(overview_module, "get_project_version", return_value="test"),
            patch.object(overview_module, "get_repository_backend", return_value="local"),
            patch.object(overview_module, "current_strategy", return_value="quota"),
            patch.object(overview_module.http_metrics, "snapshot", return_value={}),
            patch.object(
                overview_module.http_metrics,
                "storage_health",
                return_value={"status": "ok"},
            ),
            patch.object(
                overview_module.process_health,
                "snapshot",
                return_value={
                    "load_percent": 0,
                    "disk_percent": 0,
                    "memory_percent": 0,
                },
            ),
        ):
            for total, active, cooling, expected in [
                (0, 0, 0, "degraded"),
                (2, 0, 1, "degraded"),
                (2, 0, 0, "error"),
                (2, 1, 0, "ok"),
            ]:
                diagnostics = {
                    **base,
                    "total": total,
                    "status": {
                        **base["status"],
                        "active": active,
                        "cooling": cooling,
                    },
                }
                request.app.state.directory.diagnostics.return_value = diagnostics
                result = await overview_module.overview(request)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["accounts"]["available"], active)
                self.assertEqual(result["database"]["status"], "unknown")
                self.assertEqual(result["metrics_database"]["status"], "ok")


class AdminDashboardStaticTests(unittest.TestCase):
    def test_dashboard_contains_native_trends_and_translations(self):
        html = (_ROOT / "app/statics/admin/dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="status-hero"', html)
        self.assertIn('id="request-trend"', html)
        self.assertIn('id="latency-trend"', html)
        self.assertIn('id="system-cpu"', html)
        self.assertIn('id="database-status"', html)
        self.assertIn('id="health-warning"', html)
        self.assertIn('id="request-log-body"', html)
        self.assertIn("renderSparkline", html)
        self.assertIn("renderRequestLog", html)

        for language in ("en", "zh"):
            translations = json.loads(
                (_ROOT / f"app/statics/i18n/{language}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("requestTrend", translations["dashboard"])
            self.assertIn("heroReady", translations["dashboard"])
            self.assertIn("systemHealth", translations["dashboard"])
            self.assertIn("accountDiagnosis", translations["dashboard"])

    def test_account_page_has_health_filter_and_diagnosis_column(self):
        html = (_ROOT / "app/statics/admin/account.html").read_text(encoding="utf-8")

        self.assertIn('data-health="healthy"', html)
        self.assertIn('data-health="warning"', html)
        self.assertIn('data-i18n="account.colHealth"', html)
        self.assertIn("function accountHealth(token)", html)
        self.assertIn("function healthBadge(token)", html)

    def test_admin_pages_keep_session_on_transient_verify_failure(self):
        auth = (_ROOT / "app/statics/js/auth.js").read_text(encoding="utf-8")
        self.assertIn("async function keepAdminSession", auth)
        self.assertIn("response.status !== 401 && response.status !== 403", auth)
        for name in ("account.html", "config.html", "cache.html", "dashboard.html"):
            html = (_ROOT / f"app/statics/admin/{name}").read_text(
                encoding="utf-8"
            )
            self.assertIn("keepAdminSession", html)


if __name__ == "__main__":
    unittest.main()
