import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

import cli
from reddit_scraper import RedditScraper
from tests.fakes import (CHALLENGE_WITHOUT_TOKEN, HOMEPAGE, PLAIN_HOMEPAGE, RECAPTCHA_WALL,
                         FakeReddit, as_json, html)

SEARCH = "https://www.reddit.com/search.json"
SUB_SEARCH = "https://www.reddit.com/r/iems+headphones/search.json"
THREAD_URL = "https://www.reddit.com/r/iems/comments/abc123/chu_ii_vs_chu_iii_for_walks/"
THREAD_JSON = "https://www.reddit.com/r/iems/comments/abc123/chu_ii_vs_chu_iii_for_walks.json"

POST = {"subreddit": "iems", "title": "Chu II vs Chu III for walks?",
        "permalink": "/r/iems/comments/abc123/chu_ii_vs_chu_iii_for_walks/", "author": "someone",
        "score": 42, "num_comments": 3, "created_utc": 1758931200,
        "selftext": "Which one fits better?\nThanks", "name": "t3_abc123", "id": "abc123"}


def listing(*posts):
    return {"kind": "Listing", "data": {"after": None, "dist": len(posts),
                                        "children": [{"kind": "t3", "data": p} for p in posts]}}


def comment(cid, parent, author, score, body, ts, replies=()):
    return {"kind": "t1", "data": {
        "name": f"t1_{cid}", "id": cid, "parent_id": parent, "author": author, "score": score,
        "body": body, "created_utc": ts, "permalink": f"/r/iems/comments/abc123/x/{cid}/",
        "replies": {"kind": "Listing", "data": {"children": list(replies)}} if replies else ""}}


THREAD = [
    listing(POST),
    {"kind": "Listing", "data": {"after": None, "children": [
        comment("c1", "t3_abc123", "alice", 30, "Chu II fits smaller ears.", 1758934800, replies=[
            comment("c2", "t1_c1", "bob", 12, "Agreed, the III is chunkier.", 1758938400)]),
        comment("c3", "t3_abc123", "carol", 5, "Get the DSP one if you need a mic.", 1758942000),
    ]}},
]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name
        self.fake = FakeReddit({HOMEPAGE: html(PLAIN_HOMEPAGE), SEARCH: as_json(listing(POST)),
                                SUB_SEARCH: as_json(listing(POST)), THREAD_JSON: as_json(THREAD)})

    def tearDown(self):
        self.tmp.cleanup()

    def make_scraper(self):
        return self.fake.attach(RedditScraper(min_delay=0, max_delay=0, verbose=False))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*argv, "--cache-dir", self.cache], make_scraper=self.make_scraper)
        return code, out.getvalue(), err.getvalue()


class SearchTests(CliTestCase):
    def test_json_output_maps_reddit_fields(self):
        code, out, _ = self.run_cli("search", "chu", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [{
            "subreddit": "iems", "title": "Chu II vs Chu III for walks?",
            "url": "https://www.reddit.com/r/iems/comments/abc123/chu_ii_vs_chu_iii_for_walks/",
            "author": "someone", "score": 42, "num_comments": 3, "created": "2025-09-27",
            "selftext": "Which one fits better?\nThanks"}])

    def test_sub_option_searches_inside_those_subreddits(self):
        code, _, _ = self.run_cli("search", "chu", "--sub", "iems,r/headphones")
        self.assertEqual(code, 0)
        url, params = self.fake.calls[-1]
        self.assertEqual(url, SUB_SEARCH)
        self.assertEqual((params["q"], params["restrict_sr"]), ("chu", "1"))


class CacheTests(CliTestCase):
    def test_repeat_search_is_served_from_cache(self):
        first = self.run_cli("search", "chu", "--json")
        second = self.run_cli("search", "chu", "--json")
        self.assertEqual(self.fake.count(SEARCH), 1)
        self.assertEqual(first, second)

    def test_no_cache_refetches(self):
        self.run_cli("search", "chu")
        self.run_cli("search", "chu", "--no-cache")
        self.assertEqual(self.fake.count(SEARCH), 2)

    def test_entry_older_than_max_age_is_refetched(self):
        self.run_cli("search", "chu", "--max-age", "60")
        old = time.time() - 120
        for root, _, files in os.walk(self.cache):
            for f in files:
                os.utime(os.path.join(root, f), (old, old))
        self.run_cli("search", "chu", "--max-age", "60")
        self.assertEqual(self.fake.count(SEARCH), 2)

    def test_session_cookies_are_reused_between_runs(self):
        self.run_cli("search", "chu")
        self.run_cli("search", "moondrop")      # different query, so it is a real request
        self.assertEqual(self.fake.count(SEARCH), 2)
        self.assertEqual(self.fake.count(HOMEPAGE), 1)


class FailureTests(CliTestCase):
    def test_human_check_exits_3_and_explains(self):
        self.fake.routes[HOMEPAGE] = html(RECAPTCHA_WALL)
        code, out, err = self.run_cli("search", "chu")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("human", err.lower())

    def test_a_wall_does_not_poison_later_runs(self):
        self.fake.routes[SEARCH] = [html(RECAPTCHA_WALL), as_json(listing(POST))]
        self.assertEqual(self.run_cli("search", "chu")[0], 3)
        code, out, _ = self.run_cli("search", "chu", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["title"], "Chu II vs Chu III for walks?")

    def test_changed_challenge_exits_4(self):
        self.fake.routes[SEARCH] = html(CHALLENGE_WITHOUT_TOKEN)
        code, _, err = self.run_cli("search", "chu")
        self.assertEqual(code, 4)
        self.assertIn("challenge", err.lower())


class ThreadTests(CliTestCase):
    def test_text_output_indents_replies_under_their_parent(self):
        code, out, _ = self.run_cli("thread", THREAD_URL)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        start = lines.index("u/alice [30] (2025-09-27): Chu II fits smaller ears.")
        self.assertEqual(lines[start:start + 3], [
            "u/alice [30] (2025-09-27): Chu II fits smaller ears.",
            "  u/bob [12] (2025-09-27): Agreed, the III is chunkier.",
            "u/carol [5] (2025-09-27): Get the DSP one if you need a mic.",
        ])

    def test_old_reddit_links_are_accepted(self):
        code, out, _ = self.run_cli(
            "thread", "https://old.reddit.com/r/iems/comments/abc123/chu_ii_vs_chu_iii_for_walks/", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.count(THREAD_JSON), 1)
        self.assertEqual([(c["author"], c["depth"]) for c in json.loads(out)["comments"]],
                         [("alice", 0), ("bob", 1), ("carol", 0)])

    def test_top_limits_the_number_of_comments(self):
        code, out, _ = self.run_cli("thread", THREAD_URL, "--json", "--top", "2")
        self.assertEqual([c["author"] for c in json.loads(out)["comments"]], ["alice", "bob"])


class ListingTests(CliTestCase):
    def test_listing_returns_the_subreddits_posts(self):
        self.fake.routes["https://www.reddit.com/r/iems/top.json"] = as_json(listing(POST))
        code, out, _ = self.run_cli("listing", "r/iems", "--sort", "top", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([p["url"] for p in json.loads(out)],
                         ["https://www.reddit.com/r/iems/comments/abc123/chu_ii_vs_chu_iii_for_walks/"])


class ParallelRunTests(CliTestCase):
    def test_parallel_runs_never_talk_to_reddit_at_the_same_time(self):
        active, peak, guard = [0], [0], threading.Lock()

        def slow(route):
            def get(url, params=None, timeout=None, **_):
                with guard:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.15)
                with guard:
                    active[0] -= 1
                return route
            return get

        def run(query):
            sc = RedditScraper(min_delay=0, max_delay=0, verbose=False)
            sc.s.get = slow(as_json(listing(POST)))
            sc._warmed = True
            with cli.PoliteSession(self.cache, lambda: sc) as polite:
                polite.get(SEARCH, params={"q": query})

        threads = [threading.Thread(target=run, args=(q,)) for q in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1)


if __name__ == "__main__":
    unittest.main()
