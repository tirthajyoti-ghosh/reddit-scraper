import unittest

from reddit_scraper import ChallengeFormatChanged, HumanVerificationRequired, RedditScraper
from tests.fakes import (CHALLENGE_WITHOUT_TOKEN, HOMEPAGE, PLAIN_HOMEPAGE, RECAPTCHA_WALL,
                         FakeReddit, as_json, html)

SEARCH = "https://www.reddit.com/search.json"


def scraper_on(routes):
    fake = FakeReddit(routes)
    sc = fake.attach(RedditScraper(min_delay=0, max_delay=0, verbose=False))
    return sc, fake


class HumanCheckTests(unittest.TestCase):
    def test_wall_on_homepage_stops_before_any_data_request(self):
        sc, fake = scraper_on({HOMEPAGE: html(RECAPTCHA_WALL)})
        with self.assertRaises(HumanVerificationRequired):
            sc.get(SEARCH, params={"q": "x"})
        self.assertEqual(fake.count(SEARCH), 0)

    def test_wall_on_data_request_raises_without_retrying(self):
        sc, fake = scraper_on({HOMEPAGE: html(PLAIN_HOMEPAGE), SEARCH: html(RECAPTCHA_WALL)})
        with self.assertRaises(HumanVerificationRequired):
            sc.get(SEARCH, params={"q": "x"})
        self.assertEqual(fake.count(SEARCH), 1)

    def test_wall_served_with_403_is_still_a_human_check(self):
        sc, fake = scraper_on({HOMEPAGE: html(PLAIN_HOMEPAGE), SEARCH: html(RECAPTCHA_WALL, status=403)})
        with self.assertRaises(HumanVerificationRequired):
            sc.get(SEARCH, params={"q": "x"})
        self.assertEqual(fake.count(SEARCH), 1)
        self.assertEqual(fake.count(HOMEPAGE), 1)

    def test_json_that_talks_about_captchas_is_returned_normally(self):
        listing = {"kind": "Listing", "data": {"after": None, "children": [
            {"kind": "t3", "data": {"title": "Prove your humanity: why does Reddit show a recaptcha?",
                                     "selftext": 'the form posts to action="/?captcha=1"'}}]}}
        sc, _ = scraper_on({HOMEPAGE: html(PLAIN_HOMEPAGE), SEARCH: as_json(listing)})
        r = sc.get(SEARCH, params={"q": "captcha"})
        self.assertEqual(r.json()["data"]["children"][0]["data"]["title"],
                         "Prove your humanity: why does Reddit show a recaptcha?")


class ChallengeFormatTests(unittest.TestCase):
    def test_challenge_without_token_raises_a_specific_error_once(self):
        sc, fake = scraper_on({HOMEPAGE: html(PLAIN_HOMEPAGE), SEARCH: html(CHALLENGE_WITHOUT_TOKEN)})
        with self.assertRaises(ChallengeFormatChanged):
            sc.get(SEARCH, params={"q": "x"})
        self.assertEqual(fake.count(SEARCH), 1)

    def test_challenge_format_error_is_still_a_runtime_error(self):
        # existing callers that catch RuntimeError keep working
        self.assertTrue(issubclass(ChallengeFormatChanged, RuntimeError))
        self.assertTrue(issubclass(HumanVerificationRequired, RuntimeError))


if __name__ == "__main__":
    unittest.main()
