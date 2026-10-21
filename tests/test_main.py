import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from loguru import logger

from main import build_result, describe_login_failure, ensure_account, parse_tweet_id, scrape_and_save


def make_tweet(tweet_id: int, likes: int = 0, retweets: int = 0):
    return SimpleNamespace(
        id=tweet_id,
        date="2025-01-01",
        user=SimpleNamespace(username="author", displayname="Author"),
        rawContent=f"post {tweet_id}",
        likeCount=likes,
        retweetCount=retweets,
        replyCount=0,
        quoteCount=0,
        viewCount=0,
        url=f"https://x.com/author/status/{tweet_id}",
    )


class MainTests(unittest.TestCase):
    def test_parse_tweet_id(self):
        self.assertEqual(parse_tweet_id(" 123456 "), "123456")
        self.assertEqual(parse_tweet_id("https://x.com/name/status/123456?s=20"), "123456")
        self.assertEqual(parse_tweet_id("https://twitter.com/name/status/987"), "987")

    def test_parse_tweet_id_rejects_other_input(self):
        with self.assertRaises(ValueError):
            parse_tweet_id("not a tweet")
        with self.assertRaises(ValueError):
            parse_tweet_id("https://not-x.com/name/status/123")

    def test_build_result_sorts_by_engagement_and_reports_counts(self):
        result = build_result(
            make_tweet(1),
            [make_tweet(2, likes=3), make_tweet(3, likes=1, retweets=5)],
            [make_tweet(4)],
        )
        self.assertEqual([tweet["id"] for tweet in result["quotes"]], ["3", "2"])
        self.assertEqual(result["metrics"], {"total_quotes_found": 2, "total_replies_found": 1})

    def test_scrape_and_save_writes_expected_json(self):
        class FakeAPI:
            async def tweet_details(self, tweet_id):
                self.asserted_tweet_id = tweet_id
                return make_tweet(tweet_id)

            async def search(self, query, limit):
                posts = [make_tweet(2, likes=7)] if query.startswith("quoted_tweet_id:") else [make_tweet(3)]
                for post in posts:
                    yield post

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_file = Path(temporary_directory) / "tweet_1_data.json"
            api = FakeAPI()
            saved = asyncio.run(scrape_and_save(api, "1", output_file, lambda _: None))
            payload = json.loads(saved.read_text(encoding="utf-8"))

        self.assertEqual(api.asserted_tweet_id, 1)
        self.assertEqual(payload["source_tweet"]["id"], "1")
        self.assertEqual(payload["metrics"]["total_quotes_found"], 1)
        self.assertEqual(payload["metrics"]["total_replies_found"], 1)

    def test_ensure_account_reuses_active_session(self):
        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[{"username": "friend", "active": True}]),
                add_account=AsyncMock(),
                login_all=AsyncMock(),
            )
        )
        asyncio.run(ensure_account(api, "friend", "", "", "", lambda _: None))
        api.pool.add_account.assert_not_awaited()
        api.pool.login_all.assert_not_awaited()

    def test_ensure_account_registers_and_logs_in_first_account(self):
        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[]),
                add_account=AsyncMock(),
                login_all=AsyncMock(return_value={"success": 1}),
            )
        )
        asyncio.run(ensure_account(api, "friend", "x-pass", "friend@example.com", "email-pass", lambda _: None))
        api.pool.add_account.assert_awaited_once_with("friend", "x-pass", "friend@example.com", "email-pass")
        api.pool.login_all.assert_awaited_once_with(["friend"])

    def test_ensure_account_replaces_inactive_saved_credentials(self):
        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[{"username": "friend", "active": False}]),
                delete_accounts=AsyncMock(),
                add_account=AsyncMock(),
                login_all=AsyncMock(return_value={"success": 1}),
            )
        )
        asyncio.run(ensure_account(api, "friend", "new-pass", "friend@example.com", "email-pass", lambda _: None))
        api.pool.delete_accounts.assert_awaited_once_with("friend")
        api.pool.add_account.assert_awaited_once_with("friend", "new-pass", "friend@example.com", "email-pass")

    def test_ensure_account_requires_verification_credentials(self):
        api = SimpleNamespace(pool=SimpleNamespace(accounts_info=AsyncMock(return_value=[])))
        with self.assertRaisesRegex(ValueError, "email app password"):
            asyncio.run(ensure_account(api, "friend", "", "", "", lambda _: None))

    def test_login_failure_diagnostics_are_categorized_and_redacted(self):
        diagnostic = describe_login_failure(
            "login_step=LoginAcid err=ProxyError https://proxy.internal.example:8080 secret=hidden"
        )
        self.assertIn("corporate proxy", diagnostic)
        self.assertNotIn("proxy.internal.example", diagnostic)
        self.assertNotIn("hidden", diagnostic)
        self.assertIn("IMAP", describe_login_failure("login_step=LoginAcid err=IMAP authentication failed"))
        self.assertIn("rate-limited", describe_login_failure("HTTP 429"))

    def test_cloudflare_block_is_not_misreported_as_bad_credentials(self):
        response = (
            "403 - <title>Attention Required! | Cloudflare</title>"
            "<h2>Sorry, you have been blocked</h2>"
            "Cloudflare Ray ID: <strong class=\"font-semibold\">a4e0cdbe8994fab7</strong>"
            "Your IP: 203.0.113.10"
        )
        diagnostic = describe_login_failure(response)
        self.assertIn("does not indicate an incorrect password", diagnostic)
        self.assertIn("a4e0cdbe8994fab7", diagnostic)
        self.assertNotIn("203.0.113.10", diagnostic)

    def test_ensure_account_surfaces_sanitized_login_failure(self):
        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[]),
                add_account=AsyncMock(),
                login_all=AsyncMock(return_value={"success": 0}),
                get=AsyncMock(
                    return_value=SimpleNamespace(error_msg="login_step=LoginJsInstrumentationSubtask SSL certificate error")
                ),
            )
        )
        with self.assertRaisesRegex(RuntimeError, "network or TLS") as failure:
            asyncio.run(ensure_account(api, "friend", "x-pass", "friend@example.com", "email-pass", lambda _: None))
        self.assertNotIn("x-pass", str(failure.exception))
        api.pool.get.assert_awaited_once_with("friend")

    def test_ensure_account_captures_and_redacts_twscrape_http_failure(self):
        async def failed_login(_usernames):
            logger.error("Failed to login 'friend': 407 - proxy.internal.example:8080 credential=x-pass")
            return {"success": 0}

        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[]),
                add_account=AsyncMock(),
                login_all=AsyncMock(side_effect=failed_login),
                get=AsyncMock(return_value=SimpleNamespace(error_msg=None)),
            )
        )
        with self.assertRaisesRegex(RuntimeError, "proxy requires authentication") as failure:
            asyncio.run(ensure_account(api, "friend", "x-pass", "friend@example.com", "email-pass", lambda _: None))
        self.assertNotIn("proxy.internal.example", str(failure.exception))
        self.assertNotIn("x-pass", str(failure.exception))


if __name__ == "__main__":
    unittest.main()