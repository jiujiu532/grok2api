import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.control.account import cleanup as cleanup_mod
from app.control.account.backends.local import LocalAccountRepository
from app.control.account.cleanup import (
    cleanup_threshold_ms,
    seconds_until_next_daily_run,
)
from app.control.account.commands import AccountUpsert
from app.platform.runtime.clock import now_ms

_ROOT = Path(__file__).resolve().parents[1]


class AccountCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_repository_purges_deleted_accounts_in_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()
            await repo.upsert_accounts([
                AccountUpsert(token="old-deleted"),
                AccountUpsert(token="old-deleted-2"),
                AccountUpsert(token="old-deleted-3"),
                AccountUpsert(token="new-deleted"),
                AccountUpsert(token="live-token"),
            ])
            await repo.delete_accounts([
                "old-deleted",
                "old-deleted-2",
                "old-deleted-3",
                "new-deleted",
            ])

            cutoff = now_ms() - 7 * 86_400_000
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE accounts SET deleted_at = ? WHERE token LIKE ?",
                    (cutoff - 1, "old-deleted%"),
                )
                conn.execute(
                    "UPDATE accounts SET deleted_at = ? WHERE token = ?",
                    (cutoff + 1, "new-deleted"),
                )
                conn.commit()

            purged = await repo.purge_deleted_accounts(
                deleted_before_ms=cutoff,
                batch_size=2,
                vacuum=False,
            )

            self.assertEqual(purged, 3)
            with closing(sqlite3.connect(db_path)) as conn:
                rows = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT token, deleted_at FROM accounts ORDER BY token"
                    )
                }

        self.assertNotIn("old-deleted", rows)
        self.assertNotIn("old-deleted-2", rows)
        self.assertNotIn("old-deleted-3", rows)
        self.assertIsNone(rows["live-token"])
        self.assertIsNotNone(rows["new-deleted"])

    async def test_local_repository_vacuums_only_after_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()

            purged = await repo.purge_deleted_accounts(
                deleted_before_ms=now_ms(),
                batch_size=100,
                vacuum=True,
            )

        self.assertEqual(purged, 0)

    async def test_deleted_account_cleanup_query_uses_deleted_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()

            with closing(sqlite3.connect(db_path)) as conn:
                plan = conn.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT token
                    FROM accounts
                    WHERE deleted_at IS NOT NULL
                      AND deleted_at < ?
                    ORDER BY deleted_at
                    LIMIT ?
                    """,
                    (now_ms(), 5000),
                ).fetchall()

        details = " ".join(str(row) for row in plan)
        self.assertIn("idx_acc_deleted", details)

    async def test_daily_cleanup_runs_after_stop_event_wait_times_out(self):
        stop_event = asyncio.Event()

        class Repo:
            def __init__(self):
                self.calls = []

            async def purge_deleted_accounts(self, **kwargs):
                self.calls.append(kwargs)
                stop_event.set()
                return 0

        repo = Repo()
        settings = {
            "retention_days": 7,
            "run_at": "03:30",
            "batch_size": 10,
            "vacuum": False,
        }

        with (
            patch.object(cleanup_mod, "seconds_until_next_daily_run", return_value=0.001),
            patch.object(cleanup_mod, "now_ms", return_value=1_000_000_000),
        ):
            await asyncio.wait_for(
                cleanup_mod.run_daily_deleted_account_cleanup(
                    repo,
                    lambda: settings,
                    stop_event=stop_event,
                ),
                timeout=1,
            )

        self.assertEqual(len(repo.calls), 1)


class AccountCleanupScheduleTests(unittest.TestCase):
    def test_cleanup_threshold_uses_retention_days(self):
        now = 1_000_000_000

        self.assertEqual(cleanup_threshold_ms(now, 7), now - 7 * 86_400_000)

    def test_seconds_until_next_daily_run_rolls_to_tomorrow_after_time_passed(self):
        now = 1_000_000_000

        delay = seconds_until_next_daily_run(now_ms_value=now, run_at="00:00")

        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 86_400)

    def test_seconds_until_next_daily_run_accepts_invalid_config_as_default(self):
        now = 1_000_000_000

        invalid = seconds_until_next_daily_run(now_ms_value=now, run_at="bad")
        default = seconds_until_next_daily_run(now_ms_value=now, run_at="03:30")

        self.assertEqual(invalid, default)


class AccountCleanupConfigTests(unittest.TestCase):
    def test_defaults_define_deleted_account_cleanup(self):
        text = (_ROOT / "config.defaults.toml").read_text(encoding="utf-8")

        self.assertIn("[account.cleanup]", text)
        self.assertIn("deleted_retention_days = 7", text)
        self.assertIn('run_at = "03:30"', text)
        self.assertIn("batch_size = 5000", text)
        self.assertIn("vacuum = true", text)

    def test_admin_config_schema_exposes_deleted_account_cleanup(self):
        html = (_ROOT / "app/statics/admin/config.html").read_text(encoding="utf-8")

        self.assertIn("section: 'account.cleanup'", html)
        self.assertIn("deleted_retention_days", html)
        self.assertIn("run_at", html)
        self.assertIn("batch_size", html)
        self.assertIn("vacuum", html)


if __name__ == "__main__":
    unittest.main()
