# reddit-scraper

A research project studying how Reddit gates automated access to its public pages —
and a Python client that works through that gate, for educational use.

## The mechanism

A plain `requests`/`urllib` script gets `403` from Reddit; a real browser does not.
There are two layers behind that:

1. **TLS / HTTP-2 fingerprint.** Before any HTTP is exchanged, Python's TLS stack
   (`urllib3` + OpenSSL) is fingerprinted (JA3/JA4) as "not a browser." No amount of
   header-faking fixes this, because it is below HTTP.
2. **A JavaScript browser-verification challenge.** Reddit serves an 8 KB interstitial
   ("Please wait for verification") whose inline JS computes a `solution` from a seed,
   submits a hidden form, and earns a verification cookie. A client that does not run
   the JS never gets the cookie.

This client clears both:

| Concern | Approach |
| --- | --- |
| TLS/HTTP-2 fingerprint | `curl_cffi` impersonating Chrome |
| Verified session | warm up on the homepage (the JSON endpoints hard-`403` an un-warmed client) |
| JS challenge | regex fast-path (`solution = seed + seed`), with a Node fallback (`solve.js`) that runs Reddit's *actual* challenge JS in a sandbox with a minimal DOM shim |
| Politeness | jittered delays, `429` Retry-After, exponential backoff, re-warm on `403` |
| Human verification (reCAPTCHA) | detected and stopped at, never solved ([see below](#when-reddit-asks-for-a-human)) |
| Listings | pagination via the `after` / `count` cursor |
| Comment trees | recursive flatten with depth, plus expansion of both `more` stub kinds — `morechildren` (breadth) and focused refetch (depth) |

## Requirements

- Python 3.9+ — `pip install -r requirements.txt`
- Node.js — only needed for the `solve.js` challenge fallback

## Usage

```python
from reddit_scraper import RedditScraper

sc = RedditScraper(min_delay=2, max_delay=5)

# paginated listings
for post in sc.listing("python", sort="hot", limit=25, pages=3):
    print(post["score"], post["title"])

# a post's full comment tree (breadth + depth)
link, roots, leftover = sc.comments(post["permalink"], expand_more=True, max_more_calls=60)

def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n["replies"])

for c in walk(roots):
    print("  " * c["depth"], f'u/{c["author"]} [{c["score"]}] {c["body"][:80]}')
```

See [`examples/`](examples/).

## Command line (for people and AI agents)

`cli.py` wraps the scraper with the politeness a shared machine needs, so several
scripts or agents can use it at once without hammering Reddit.

```bash
python3 cli.py search "moondrop chu" --sub iems,headphones --t year --limit 10
python3 cli.py thread https://www.reddit.com/r/iems/comments/abc123/some_title/ --top 40
python3 cli.py listing iems --sort top --limit 25
```

Add `--json` to any command for machine-readable output. `thread` takes `www.` and
`old.reddit.com` links or a bare permalink.

| Built in | Why |
| --- | --- |
| One Reddit conversation at a time across all processes (a file lock) | parallel agents queue instead of bursting |
| JSON responses cached on disk for 24 h (`--max-age`, `--no-cache`) | asking again costs no request |
| Session cookies reused for 30 min | each run skips the homepage warmup |

Cache, cookies and the lock live in `~/.cache/reddit-scraper` (override with
`$REDDIT_SCRAPER_CACHE` or `--cache-dir`). The lock uses `fcntl`, so on Windows runs
are not serialised.

| Exit code | Meaning | What to do |
| --- | --- | --- |
| 0 | ok | |
| 1 | other failure (network, non-JSON answer) | read stderr |
| 2 | bad arguments | see `--help` |
| 3 | Reddit wants a human (reCAPTCHA page) | stop; wait, or use the official API |
| 4 | Reddit's JS challenge changed | stop; the solver needs updating |

**If you are an AI agent:** use `--json`, keep volume low (a handful of searches and
threads per task), and let the cache work for you. On exit code 3 or 4, stop and tell
your human. Don't retry in a loop, and never try to solve or get around a CAPTCHA: it
is a wall for humans, and retrying only makes the block last longer.

## When Reddit asks for a human

On 27 Sep 2026 Reddit started answering this client with a Google reCAPTCHA page
("Prove your humanity") instead of the JS challenge. That is a different kind of gate:
it is meant for a person, and this client does not solve CAPTCHAs.

- `RedditScraper` raises `HumanVerificationRequired` the moment it sees one, instead of
  mistaking the page for content or re-warming in a loop. The CLI exits with `3`.
- A JS challenge in a shape the solver doesn't recognise raises `ChallengeFormatChanged`
  (CLI exit `4`) instead of a bare "no token" error.
- Both subclass `RuntimeError`, so existing `except RuntimeError` code keeps working.

If you hit either, wait before trying again, or move to the official OAuth API
([`praw`](https://praw.readthedocs.io/)), which is the right tool for anything real anyway.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

They run offline: only the session's network call is swapped for a fake, so nothing
touches reddit.com.

## Notes & caveats

- **This is research / educational code.** Scraping `reddit.com` (rather than the
  official API) is against Reddit's Terms of Service. For anything real, use the
  official OAuth Data API (e.g. [`praw`](https://praw.readthedocs.io/)).
- The solver is intentionally minimal and **brittle by design** — it tracks Reddit's
  current challenge. Structural changes (real proof-of-work, browser-API
  fingerprinting) would call for a headless browser (Playwright / Puppeteer).
- Be a good citizen: keep volume low, leave the rate limiting on, and cache what you
  pull instead of re-fetching. `expand_more=True` costs one request per collapsed /
  depth-capped branch, so a large thread can be many requests.
