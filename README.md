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
