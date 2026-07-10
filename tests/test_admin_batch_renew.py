import unittest
from pathlib import Path

import orjson

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.account.refresh import RefreshResult
from app.platform.errors import ValidationError
from app.products.web.admin.batch import BatchRequest, batch_renew


class _Repo:
    def __init__(self, records: dict[str, AccountRecord]) -> None:
        self.records = records
        self.requested_tokens: list[str] = []
        self.patches: list = []

    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        self.requested_tokens = list(tokens)
        return [self.records[token] for token in tokens if token in self.records]

    async def patch_accounts(self, patches: list) -> object:
        self.patches.extend(patches)

        class _Result:
            patched = len(patches)

        return _Result()


class _RefreshService:
    def __init__(self, results: dict[str, RefreshResult] | None = None) -> None:
        self.results = results or {}
        self.renewed_tokens: list[str] = []

    async def renew_tokens(self, tokens: list[str]) -> RefreshResult:
        self.renewed_tokens.extend(tokens)
        if len(tokens) == 1 and tokens[0] in self.results:
            return self.results[tokens[0]]
        return RefreshResult(refreshed=len(tokens))


class AdminBatchRenewTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_renew_includes_expired_and_skips_disabled(self):
        repo = _Repo(
            {
                "active-token": AccountRecord(token="active-token", status=AccountStatus.ACTIVE),
                "expired-token": AccountRecord(token="expired-token", status=AccountStatus.EXPIRED),
                "disabled-token": AccountRecord(token="disabled-token", status=AccountStatus.DISABLED),
            }
        )
        refresh_svc = _RefreshService(
            {
                "active-token": RefreshResult(refreshed=1),
                "expired-token": RefreshResult(refreshed=1, recovered=1),
            }
        )

        response = await batch_renew(
            BatchRequest(tokens=["active-token", "expired-token", "disabled-token"]),
            async_mode=False,
            concurrency=None,
            repo=repo,
            refresh_svc=refresh_svc,
        )

        body = orjson.loads(response.body)
        self.assertEqual(
            repo.requested_tokens,
            ["active-token", "expired-token", "disabled-token"],
        )
        self.assertEqual(refresh_svc.renewed_tokens, ["active-token", "expired-token"])
        self.assertEqual(body["summary"]["total"], 2)
        self.assertEqual(body["summary"]["ok"], 2)
        self.assertEqual(body["summary"]["fail"], 0)

    async def test_batch_renew_rejects_only_disabled_tokens(self):
        repo = _Repo(
            {
                "disabled-token": AccountRecord(token="disabled-token", status=AccountStatus.DISABLED),
            }
        )
        refresh_svc = _RefreshService()

        with self.assertRaises(ValidationError) as cm:
            await batch_renew(
                BatchRequest(tokens=["disabled-token"]),
                async_mode=False,
                concurrency=None,
                repo=repo,
                refresh_svc=refresh_svc,
            )

        self.assertIn("No renewable tokens available", str(cm.exception))
        self.assertEqual(refresh_svc.renewed_tokens, [])

    async def test_batch_renew_marks_probe_failure(self):
        repo = _Repo(
            {
                "dead-token": AccountRecord(token="dead-token", status=AccountStatus.EXPIRED),
            }
        )
        refresh_svc = _RefreshService({"dead-token": RefreshResult(checked=1, failed=1)})

        response = await batch_renew(
            BatchRequest(tokens=["dead-token"]),
            async_mode=False,
            concurrency=None,
            repo=repo,
            refresh_svc=refresh_svc,
        )
        body = orjson.loads(response.body)
        self.assertEqual(body["summary"]["ok"], 0)
        self.assertEqual(body["summary"]["fail"], 1)


class AccountRenewUiTests(unittest.TestCase):
    def test_account_html_has_row_and_batch_renew_controls(self):
        html = Path("app/statics/admin/account.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-renew"', html)
        self.assertIn("batchRenewSel()", html)
        self.assertIn("renewOne(", html)
        self.assertIn("/batch/renew", html)

    def test_renew_strings_exist_for_all_locales(self):
        required = {
            "batchRenew",
            "actionRenew",
            "renewing",
            "renewDone",
            "renewFailed",
            "noRenewableAccounts",
            "batchRenewConfirmTitle",
            "batchRenewConfirmBody",
        }
        for path in Path("app/statics/i18n").glob("*.json"):
            data = orjson.loads(path.read_bytes())
            with self.subTest(locale=path.name):
                account = data["account"]
                missing = sorted(required - set(account))
                self.assertEqual(missing, [], f"{path.name} missing keys: {missing}")


if __name__ == "__main__":
    unittest.main()
