import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import orjson

from app.control.account.models import AccountPage, AccountRecord
from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.products.web.admin import tokens as admin_tokens


class _FastListRepo:
    def __init__(self) -> None:
        self.fast_called = False
        self.list_called = False

    async def list_token_payloads(self) -> list[dict]:
        self.fast_called = True
        return [{
            "token": "tok-1",
            "pool": "basic",
            "status": "active",
            "quota": {},
            "use_count": 0,
            "fail_count": 0,
            "last_used_at": None,
            "tags": [],
        }]

    async def list_accounts(self, query):
        self.list_called = True
        raise AssertionError("list_tokens should use the compact token payload path")


class _FastInvalidRepo:
    def __init__(self) -> None:
        self.fast_called = False
        self.payload_called = False
        self.deleted: list[str] = []

    async def list_invalid_tokens(self) -> list[str]:
        self.fast_called = True
        return ["expired-token"]

    async def list_token_payloads(self) -> list[dict]:
        self.payload_called = True
        raise AssertionError("delete_invalid_tokens should use the invalid-token fast path")

    async def delete_accounts(self, tokens: list[str]):
        self.deleted = tokens


class _PagedRepo:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def list_accounts(self, query):
        return AccountPage(
            items=[
                AccountRecord(token="active-token", status=AccountStatus.ACTIVE),
                AccountRecord(token="expired-token", status=AccountStatus.EXPIRED),
            ],
            total=2,
            page=1,
            page_size=2000,
            total_pages=1,
        )

    async def delete_accounts(self, tokens: list[str]):
        self.deleted = tokens


class _WalFailConnection:
    def __init__(self):
        self.row_factory = None
        self.closed = False

    def execute(self, sql: str, *args, **kwargs):
        if sql == "PRAGMA journal_mode=WAL":
            raise sqlite3.OperationalError("disk I/O error")
        raise AssertionError(f"broken WAL connection reused: {sql}")

    def close(self):
        self.closed = True


class _WalRetryForbiddenConnection:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def execute(self, sql: str, *args, **kwargs):
        if sql == "PRAGMA journal_mode=WAL":
            raise AssertionError("WAL retried after fallback")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self.closed = True
        self._conn.close()


class _WalWriteFailConnection:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def execute(self, sql: str, *args, **kwargs):
        return self._conn.execute(sql, *args, **kwargs)

    def executescript(self, sql: str):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        self.closed = True
        self._conn.close()


class _LiveIndexFailConnection:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def execute(self, sql: str, *args, **kwargs):
        if "idx_acc_live_updated" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args, **kwargs)

    def executescript(self, sql: str):
        if "idx_acc_live_updated" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.executescript(sql)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self.closed = True
        self._conn.close()


class AdminTokenListPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tokens_uses_compact_payload_fast_path(self):
        repo = _FastListRepo()

        response = await admin_tokens.list_tokens(repo=repo)

        body = orjson.loads(response.body)
        self.assertTrue(repo.fast_called)
        self.assertFalse(repo.list_called)
        self.assertEqual(body["tokens"][0]["token"], "tok-1")

    async def test_local_repository_returns_compact_token_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([
                AccountUpsert(token="tok-1", pool="basic", tags=["nsfw"]),
            ])
            await repo.patch_accounts([
                AccountPatch(
                    token="tok-1",
                    usage_use_delta=3,
                    usage_fail_delta=2,
                    quota_console={"remaining": 4, "total": 5},
                )
            ])

            items = await repo.list_token_payloads()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["token"], "tok-1")
        self.assertEqual(items[0]["pool"], "basic")
        self.assertEqual(items[0]["status"], "active")
        self.assertEqual(items[0]["use_count"], 3)
        self.assertEqual(items[0]["fail_count"], 2)
        self.assertEqual(items[0]["quota"]["console"], {"remaining": 4, "total": 5})
        self.assertEqual(items[0]["tags"], ["nsfw"])
        self.assertNotIn("ext", items[0])

    async def test_local_repository_tolerates_legacy_blank_quota_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="tok-1", pool="basic")])

            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE accounts SET quota_auto = '', quota_console = 'not-json'"
                )
                conn.commit()

            items = await repo.list_token_payloads()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quota"]["auto"], {"remaining": 0, "total": 0})
        self.assertEqual(items[0]["quota"]["console"], {"remaining": 0, "total": 0})

    async def test_local_repository_initializes_live_updated_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()

            with closing(sqlite3.connect(db_path)) as conn:
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }

        self.assertIn("idx_acc_live_updated", indexes)

    async def test_local_repository_initializes_after_wal_disk_io_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            broken = _WalFailConnection()
            real_conn = sqlite3.connect(db_path, check_same_thread=False)

            try:
                with patch(
                    "app.control.account.backends.local.sqlite3.connect",
                    side_effect=[broken, real_conn],
                ):
                    await repo.initialize()
            finally:
                real_conn.close()

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertTrue(broken.closed)
        self.assertIn("accounts", tables)

    async def test_local_repository_does_not_retry_wal_after_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            broken = _WalFailConnection()
            real_conn = sqlite3.connect(db_path, check_same_thread=False)
            retry_forbidden = _WalRetryForbiddenConnection(
                sqlite3.connect(db_path, check_same_thread=False)
            )

            try:
                with patch(
                    "app.control.account.backends.local.sqlite3.connect",
                    side_effect=[broken, real_conn, retry_forbidden],
                ):
                    await repo.initialize()
                    revision = await repo.get_revision()
            finally:
                real_conn.close()
                retry_forbidden.close()

        self.assertEqual(revision, 0)
        self.assertTrue(broken.closed)

    async def test_local_repository_retries_delete_journal_when_wal_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            wal_conn = _WalWriteFailConnection(
                sqlite3.connect(db_path, check_same_thread=False)
            )
            delete_conn = _WalRetryForbiddenConnection(
                sqlite3.connect(db_path, check_same_thread=False)
            )

            try:
                with patch(
                    "app.control.account.backends.local.sqlite3.connect",
                    side_effect=[wal_conn, delete_conn],
                ):
                    await repo.initialize()
            finally:
                wal_conn.close()
                delete_conn.close()

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertTrue(wal_conn.closed)
        self.assertTrue(delete_conn.closed)
        self.assertFalse(repo._prefer_wal)
        self.assertIn("accounts", tables)

    async def test_local_repository_retries_memory_journal_when_delete_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            wal_broken = _WalFailConnection()
            delete_conn = _WalWriteFailConnection(
                sqlite3.connect(db_path, check_same_thread=False)
            )
            memory_conn = _WalRetryForbiddenConnection(
                sqlite3.connect(db_path, check_same_thread=False)
            )

            try:
                with patch(
                    "app.control.account.backends.local.sqlite3.connect",
                    side_effect=[wal_broken, delete_conn, memory_conn],
                ):
                    await repo.initialize()
            finally:
                delete_conn.close()
                memory_conn.close()

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertTrue(wal_broken.closed)
        self.assertTrue(delete_conn.closed)
        self.assertTrue(memory_conn.closed)
        self.assertFalse(repo._prefer_wal)
        self.assertEqual(repo._fallback_journal_mode, "MEMORY")
        self.assertIn("accounts", tables)

    async def test_local_repository_tolerates_live_updated_index_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            real_connect = sqlite3.connect
            wrapped: list[_LiveIndexFailConnection] = []

            def connect(*args, **kwargs):
                conn = _LiveIndexFailConnection(real_connect(*args, **kwargs))
                wrapped.append(conn)
                return conn

            with patch(
                "app.control.account.backends.local.sqlite3.connect",
                side_effect=connect,
            ):
                await repo.initialize()

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }

        self.assertTrue(all(conn.closed for conn in wrapped))
        self.assertIn("accounts", tables)
        self.assertNotIn("idx_acc_live_updated", indexes)

    async def test_local_repository_token_payload_query_uses_live_updated_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()

            with closing(sqlite3.connect(db_path)) as conn:
                plan = [
                    row[-1]
                    for row in conn.execute(
                        f"EXPLAIN QUERY PLAN {repo._token_payload_select_sql()}"
                    )
                ]

        self.assertTrue(
            any("idx_acc_live_updated" in detail for detail in plan),
            plan,
        )

    async def test_delete_invalid_tokens_uses_invalid_token_fast_path(self):
        repo = _FastInvalidRepo()

        response = await admin_tokens.delete_invalid_tokens(repo=repo)

        body = orjson.loads(response.body)
        self.assertTrue(repo.fast_called)
        self.assertFalse(repo.payload_called)
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(repo.deleted, ["expired-token"])

    async def test_delete_invalid_tokens_fallback_keeps_active_accounts(self):
        repo = _PagedRepo()

        response = await admin_tokens.delete_invalid_tokens(repo=repo)

        body = orjson.loads(response.body)
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(repo.deleted, ["expired-token"])


if __name__ == "__main__":
    unittest.main()
