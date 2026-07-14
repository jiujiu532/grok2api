"""Small SQLite store for cross-restart HTTP metric buckets."""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class HTTPMetricsStore:
    def __init__(self, path: Path, *, retention_days: int = 7) -> None:
        self.path = Path(path)
        self.retention_days = max(1, retention_days)
        self._lock = threading.Lock()
        self._last_prune_day = -1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS http_minute_metrics (
                timestamp       INTEGER PRIMARY KEY,
                requests        INTEGER NOT NULL DEFAULT 0,
                successes       INTEGER NOT NULL DEFAULT 0,
                client_errors   INTEGER NOT NULL DEFAULT 0,
                server_errors   INTEGER NOT NULL DEFAULT 0,
                latency_ms      REAL NOT NULL DEFAULT 0,
                last_request_at REAL NOT NULL
            )
            """
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS http_request_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     REAL NOT NULL,
                request_id    TEXT NOT NULL,
                method        TEXT NOT NULL,
                path          TEXT NOT NULL,
                status        INTEGER NOT NULL,
                latency_ms    REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_http_request_log_timestamp
                ON http_request_log (timestamp DESC);
            """
        )
        self._conn.commit()

    def record(
        self,
        timestamp: float,
        status: int,
        latency_ms: float,
        *,
        request_id: str = "",
        method: str = "",
        path: str = "",
    ) -> None:
        minute = int(timestamp // 60) * 60
        day = int(timestamp // 86_400)
        success = int(status < 400)
        client_error = int(400 <= status < 500)
        server_error = int(status >= 500)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO http_request_log (
                    timestamp, request_id, method, path, status, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timestamp, request_id, method, path, status, latency_ms),
            )
            self._conn.execute(
                """
                INSERT INTO http_minute_metrics (
                    timestamp, requests, successes, client_errors,
                    server_errors, latency_ms, last_request_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(timestamp) DO UPDATE SET
                    requests = requests + 1,
                    successes = successes + excluded.successes,
                    client_errors = client_errors + excluded.client_errors,
                    server_errors = server_errors + excluded.server_errors,
                    latency_ms = latency_ms + excluded.latency_ms,
                    last_request_at = MAX(last_request_at, excluded.last_request_at)
                """,
                (
                    minute,
                    success,
                    client_error,
                    server_error,
                    latency_ms,
                    timestamp,
                ),
            )
            if day != self._last_prune_day:
                self._conn.execute(
                    "DELETE FROM http_minute_metrics WHERE timestamp < ?",
                    (minute - self.retention_days * 86_400,),
                )
                self._conn.execute(
                    "DELETE FROM http_request_log WHERE timestamp < ?",
                    (timestamp - self.retention_days * 86_400,),
                )
                self._last_prune_day = day

    def snapshot(self, *, minutes: int = 30) -> dict[str, Any]:
        current_minute = int(time.time() // 60) * 60
        first_minute = current_minute - (max(1, minutes) - 1) * 60
        with self._lock:
            totals = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(requests), 0),
                    COALESCE(SUM(successes), 0),
                    COALESCE(SUM(client_errors), 0),
                    COALESCE(SUM(server_errors), 0),
                    COALESCE(SUM(latency_ms), 0),
                    MAX(last_request_at)
                FROM http_minute_metrics
                """
            ).fetchone()
            rows = self._conn.execute(
                """
                SELECT timestamp, requests, client_errors, server_errors, latency_ms
                FROM http_minute_metrics
                WHERE timestamp >= ?
                ORDER BY timestamp
                """,
                (first_minute,),
            ).fetchall()
            recent_rows = self._conn.execute(
                """
                SELECT timestamp, request_id, method, path, status, latency_ms
                FROM http_request_log
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()

        requests = int(totals[0])
        history_by_minute = {int(row[0]): row for row in rows}
        history = []
        for timestamp in range(first_minute, current_minute + 1, 60):
            row = history_by_minute.get(timestamp)
            bucket_requests = int(row[1]) if row else 0
            history.append(
                {
                    "timestamp": timestamp,
                    "requests": bucket_requests,
                    "errors": int(row[2] + row[3]) if row else 0,
                    "average_latency_ms": (
                        round(float(row[4]) / bucket_requests, 1)
                        if row and bucket_requests
                        else 0.0
                    ),
                }
            )
        return {
            "scope": "persistent",
            "requests": requests,
            "successes": int(totals[1]),
            "client_errors": int(totals[2]),
            "server_errors": int(totals[3]),
            "average_latency_ms": (
                round(float(totals[4]) / requests, 1) if requests else 0.0
            ),
            "last_request_at": float(totals[5]) if totals[5] is not None else None,
            "history": history,
            "recent_requests": [
                {
                    "timestamp": float(row[0]),
                    "request_id": str(row[1]),
                    "method": str(row[2]),
                    "path": str(row[3]),
                    "status": int(row[4]),
                    "latency_ms": round(float(row[5]), 1),
                }
                for row in recent_rows
            ],
            "retention_days": self.retention_days,
        }

    def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        with self._lock:
            result = str(self._conn.execute("PRAGMA quick_check(1)").fetchone()[0])
        size = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if candidate.exists()
        )
        return {
            "status": "ok" if result == "ok" else "error",
            "check": result,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "size_bytes": size,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["HTTPMetricsStore"]
