import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from main import build_result, ensure_account, parse_tweet_id, scrape_and_save


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
                add_account_cookies=AsyncMock(),
            )
        )
        asyncio.run(ensure_account(api, "friend", "auth_token=abc; ct0=123", lambda _: None))
        api.pool.add_account_cookies.assert_not_awaited()

    def test_ensure_account_adds_cookies_for_new_account(self):
        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[]),
                add_account_cookies=AsyncMock(),
            )
        )
        asyncio.run(ensure_account(api, "friend", "auth_token=abc; ct0=123", lambda _: None))
        api.pool.add_account_cookies.assert_awaited_once_with("friend", "auth_token=abc; ct0=123")

    def test_ensure_account_replaces_inactive_saved_credentials(self):
        api = SimpleNamespace(
            pool=SimpleNamespace(
                accounts_info=AsyncMock(return_value=[{"username": "friend", "active": False}]),
                delete_accounts=AsyncMock(),
                add_account_cookies=AsyncMock(),
            )
        )
        asyncio.run(ensure_account(api, "friend", "auth_token=abc; ct0=123", lambda _: None))
        api.pool.delete_accounts.assert_awaited_once_with("friend")
        api.pool.add_account_cookies.assert_awaited_once_with("friend", "auth_token=abc; ct0=123")

    def test_ensure_account_requires_cookies(self):
        api = SimpleNamespace(pool=SimpleNamespace(accounts_info=AsyncMock(return_value=[])))
        with self.assertRaisesRegex(ValueError, "Browser cookies are required"):
            asyncio.run(ensure_account(api, "friend", "", lambda _: None))


if __name__ == "__main__":
    unittest.main()