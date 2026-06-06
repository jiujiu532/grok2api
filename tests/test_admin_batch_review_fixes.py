import asyncio
import re
import unittest
from pathlib import Path

import orjson

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.account.refresh import RefreshResult
from app.control.account.backends.redis import RedisAccountRepository
from app.platform.errors import ValidationError
from app.products.web.admin.batch import BatchRequest, batch_refresh
from app.products.web.admin import tokens as admin_tokens


class _Repo:
    def __init__(self) -> None:
        self.records = {
            "active-token": AccountRecord(token="active-token", status=AccountStatus.ACTIVE),
            "disabled-token": AccountRecord(token="disabled-token", status=AccountStatus.DISABLED),
        }
        self.requested_tokens: list[str] = []

    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        self.requested_tokens = tokens
        return [self.records[token] for token in tokens if token in self.records]


class _RefreshService:
    def __init__(self) -> None:
        self.refreshed_tokens: list[str] = []

    async def refresh_tokens(self, tokens: list[str]) -> RefreshResult:
        self.refreshed_tokens.extend(tokens)
        return RefreshResult(refreshed=len(tokens))


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.keys: list[str] = []

    async def __aenter__(self) -> "_Pipeline":
        self.redis.pipeline_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def hgetall(self, key: str) -> None:
        self.keys.append(key)

    async def execute(self) -> list[dict[str, str]]:
        return [self.redis.hashes.get(key, {}) for key in self.keys]


class _Redis:
    def __init__(self) -> None:
        active = AccountRecord(token="active-token", status=AccountStatus.ACTIVE)
        self.hashes = {
            "accounts:record:active-token": RedisAccountRepository._to_hash(active, revision=7),
        }
        self.pipeline_count = 0
        self.hgetall_count = 0

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        self.hgetall_count += 1
        return self.hashes.get(key, {})


class AdminBatchReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_refresh_filters_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        response = await batch_refresh(
            BatchRequest(tokens=["active-token", "disabled-token"]),
            async_mode=False,
            all_manageable=False,
            concurrency=None,
            repo=repo,
            refresh_svc=refresh_svc,
        )

        body = orjson.loads(response.body)
        self.assertEqual(repo.requested_tokens, ["active-token", "disabled-token"])
        self.assertEqual(refresh_svc.refreshed_tokens, ["active-token"])
        self.assertEqual(body["summary"], {"total": 1, "ok": 1, "fail": 0})

    async def test_batch_refresh_rejects_only_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        with self.assertRaises(ValidationError) as cm:
            await batch_refresh(
                BatchRequest(tokens=["disabled-token"]),
                async_mode=False,
                all_manageable=False,
                concurrency=None,
                repo=repo,
                refresh_svc=refresh_svc,
            )

        self.assertIn("No manageable tokens available", str(cm.exception))
        self.assertEqual(refresh_svc.refreshed_tokens, [])


class RedisRepositoryReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_accounts_reads_many_tokens_with_one_pipeline(self):
        redis = _Redis()
        repo = RedisAccountRepository(redis)

        records = await repo.get_accounts(["active-token", "missing-token"])

        self.assertEqual([record.token for record in records], ["active-token"])
        self.assertEqual(redis.pipeline_count, 1)
        self.assertEqual(redis.hgetall_count, 0)


class AccountHtmlReviewFixTests(unittest.TestCase):
    def test_disabled_nsfw_buttons_use_row_specific_unavailable_reason(self):
        with open("app/statics/admin/account.html", encoding="utf-8") as fh:
            html = fh.read()
        disabled_branches = re.findall(
            r"data-tip=\"\$\{xe\(canManageNsfw \? tr\('account\.batchNsfw(?:Disable)?'.*?"
            r": tr\('account\.rowActionNotSupported'.*?aria-label=\"\$\{xe\((.*?)\)\}\"",
            html,
        )

        self.assertEqual(len(disabled_branches), 2)
        self.assertTrue(
            all(
                "canManageNsfw ?" in branch and "account.rowActionNotSupported" in branch
                for branch in disabled_branches
            )
        )

    def test_row_action_not_supported_is_translated_for_all_account_locales(self):
        for path in Path("app/statics/i18n").glob("*.json"):
            data = orjson.loads(path.read_bytes())
            with self.subTest(locale=path.name):
                self.assertIn("account", data, f"Locale {path.name} missing account section")
                self.assertIn("rowActionNotSupported", data["account"])


class ConfigHtmlReviewFixTests(unittest.TestCase):
    def test_get_current_value_preserves_schema_defaults(self):
        html = Path("app/statics/admin/config.html").read_text(encoding="utf-8")

        self.assertIn("function _getCurrentValue(section, key, field)", html)
        self.assertIn("_getValue(section, key, field)", html)
        self.assertIn("_getCurrentValue(section, field.key, field)", html)


class AdminTokenTaskReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        getattr(admin_tokens, "_background_tasks", set()).clear()

    async def asyncTearDown(self) -> None:
        pending = list(getattr(admin_tokens, "_background_tasks", set()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        getattr(admin_tokens, "_background_tasks", set()).clear()

    async def test_fire_and_forget_keeps_task_until_completion(self):
        release = asyncio.Event()

        async def _wait() -> None:
            await release.wait()

        task = admin_tokens._fire_and_forget(_wait())

        self.assertIn(task, admin_tokens._background_tasks)
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, admin_tokens._background_tasks)


if __name__ == "__main__":
    unittest.main()
