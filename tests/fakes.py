"""Test doubles for the network layer only.

The scrapers under test are real RedditScraper objects with a real curl_cffi
session (so the real cookie jar is used); only session.get, the call that would
touch reddit.com, is replaced by FakeReddit.
"""
import json

HOMEPAGE = "https://www.reddit.com/"

# Trimmed from the page reddit.com served on 27 Sep 2026 (title, reCAPTCHA
# script, g-recaptcha widget and the /?captcha=1 form are verbatim).
RECAPTCHA_WALL = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>Reddit - Prove your humanity</title>
    <script src="https://www.google.com/recaptcha/api.js"></script>
</head>
<body>
    <form method="POST" action="/?captcha=1">
        <div class="g-recaptcha" data-sitekey="6LeTlV4oAAAAAGioktuFt-KvUtwKRJRfc8A7UJws" data-action="edge" data-callback="submit"></div>
    </form>
    <p>Prove your humanity</p>
    <p>We're committed to safety and security. But not for bots. Complete the challenge below and let us know you're a real person.</p>
</body>
</html>"""

# A JS-challenge interstitial whose markup no longer carries the hidden token input.
CHALLENGE_WITHOUT_TOKEN = """<!DOCTYPE html><html><head><title>Reddit</title></head>
<body><form method="GET" action="/?js_challenge=1"><input type="hidden" name="solution" value=""></form>
<script>/* new-style js_challenge */</script></body></html>"""

PLAIN_HOMEPAGE = "<!DOCTYPE html><html><head><title>Reddit - Dive into anything</title></head><body></body></html>"


class FakeResponse:
    def __init__(self, status_code, text, content_type):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)


def html(text, status=200):
    return FakeResponse(status, text, "text/html; charset=UTF-8")


def as_json(obj, status=200):
    return FakeResponse(status, json.dumps(obj), "application/json; charset=UTF-8")


class FakeReddit:
    """Routes session.get calls by URL and records every call made."""

    def __init__(self, routes):
        self.routes = routes          # url -> FakeResponse, or a list of them served in order
        self.calls = []               # (url, params) for every request

    def attach(self, scraper, session_cookie=("edgebucket", "fake")):
        def get(url, params=None, timeout=None, **_):
            self.calls.append((url, dict(params or {})))
            if url == HOMEPAGE and session_cookie:
                name, value = session_cookie
                scraper.s.cookies.set(name, value, domain=".reddit.com", path="/")
            route = self.routes[url]
            return route.pop(0) if isinstance(route, list) else route
        scraper.s.get = get
        return scraper

    def count(self, url):
        return sum(1 for u, _ in self.calls if u == url)
