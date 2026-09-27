#!/usr/bin/env python3
"""Command-line front end for reddit_scraper, for people and for AI agents.

  python3 cli.py search "moondrop chu" --sub iems,headphones --t year --limit 10
  python3 cli.py thread https://www.reddit.com/r/iems/comments/abc123/some_title/ --top 40
  python3 cli.py listing iems --sort top --limit 25

Add --json to any command for machine-readable output.

Politeness is built in, so many parallel callers stay gentle on Reddit:
  - one Reddit conversation at a time across every process on the machine (a file lock)
  - successful JSON responses cached on disk (24 h by default; --max-age, --no-cache)
  - session cookies reused for 30 minutes, so each run skips the homepage warmup

Exit codes: 0 ok, 1 other failure, 2 bad arguments,
            3 Reddit wants a human (reCAPTCHA), 4 Reddit's JS challenge changed.
On 3 or 4, stop: don't retry in a loop, and never try to solve a CAPTCHA.
"""
import argparse, datetime, hashlib, json, os, sys, time
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:            # Windows: no cross-process lock, everything else works
    fcntl = None

from reddit_scraper import BASE, ChallengeFormatChanged, HumanVerificationRequired, RedditScraper

DEFAULT_CACHE = os.environ.get("REDDIT_SCRAPER_CACHE",
                               os.path.join(os.path.expanduser("~"), ".cache", "reddit-scraper"))
COOKIE_TTL = 30 * 60


class _CachedResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def json(self):
        return json.loads(self.text)


class PoliteSession:
    """Context manager around a RedditScraper: cross-process lock, JSON disk cache, cookie reuse."""

    def __init__(self, cache_dir, make_scraper=None, max_age=86400, use_cache=True):
        self.cache_dir = cache_dir
        self.responses = os.path.join(cache_dir, "responses")
        self.cookie_file = os.path.join(cache_dir, "cookies.json")
        self.make_scraper = make_scraper or (lambda: RedditScraper(min_delay=2, max_delay=5, verbose=False))
        self.max_age, self.use_cache = max_age, use_cache

    def __enter__(self):
        os.makedirs(self.responses, exist_ok=True)
        self._lock = open(os.path.join(self.cache_dir, ".lock"), "w")
        if fcntl:
            fcntl.flock(self._lock, fcntl.LOCK_EX)      # waits while another process talks to Reddit
        self.sc = self.make_scraper()
        self._load_cookies()
        network_get = self.sc.get

        def get(url, params=None, max_retries=5):
            key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
            path = os.path.join(self.responses, key + ".json")
            if self.use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < self.max_age:
                with open(path) as f:
                    return _CachedResponse(f.read())
            r = network_get(url, params=params, max_retries=max_retries)
            if r is not None and r.status_code == 200:
                try:
                    json.loads(r.text)
                except ValueError:
                    return r                              # only JSON is worth caching
                with open(path, "w") as f:
                    f.write(r.text)
            return r

        self.sc.get = get      # listing() and comments() call self.get, so they are cached too
        return self.sc

    def __exit__(self, *exc):
        try:
            if getattr(self.sc, "_warmed", False):
                self._save_cookies()
        finally:
            self._lock.close()                            # closing the file releases the lock

    def _load_cookies(self):
        try:
            if time.time() - os.path.getmtime(self.cookie_file) > COOKIE_TTL:
                return
            with open(self.cookie_file) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for c in saved:
            self.sc.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        self.sc._warmed = True       # the scraper re-warms by itself if Reddit answers 403

    def _save_cookies(self):
        jar = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
               for c in self.sc.s.cookies.jar]
        fd = os.open(self.cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(jar, f)


# ---- commands ---------------------------------------------------------------
def day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d") if ts else ""


def post_row(p):
    return {"subreddit": p.get("subreddit"), "title": p.get("title"),
            "url": BASE + (p.get("permalink") or ""), "author": p.get("author"),
            "score": p.get("score"), "num_comments": p.get("num_comments"),
            "created": day(p.get("created_utc")), "selftext": p.get("selftext") or ""}


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n["replies"])


def cmd_search(sc, a):
    params = {"q": a.query, "sort": a.sort, "t": a.t, "limit": str(a.limit), "type": "link", "raw_json": "1"}
    if a.sub:
        subs = [s.strip().removeprefix("r/") for s in a.sub.split(",") if s.strip()]
        url = f"{BASE}/r/{'+'.join(subs)}/search.json"
        params["restrict_sr"] = "1"
    else:
        url = f"{BASE}/search.json"
    data = sc.get(url, params=params).json()
    return [post_row(c["data"]) for c in data.get("data", {}).get("children", [])]


def cmd_thread(sc, a):
    target = urlparse(a.url).path if a.url.startswith("http") else a.url   # www. and old. links alike
    link, roots, leftover = sc.comments(target, expand_more=a.more > 0, max_more_calls=a.more)
    comments = []
    for c in walk(roots):
        if len(comments) >= a.top:
            break
        comments.append({"author": c.get("author"), "score": c.get("score"), "depth": c["depth"],
                         "created": day(c.get("created_utc")), "body": c.get("body") or ""})
    return {**post_row(link), "comments": comments,
            "collapsed": sum(m.get("count", 1) for m in leftover)}


def cmd_listing(sc, a):
    return [post_row(p) for p in sc.listing(a.subreddit.removeprefix("r/"), sort=a.sort,
                                            limit=a.limit, pages=a.pages)]


def print_posts(rows, a):
    if not rows:
        print("(no results)")
    for r in rows:
        print(f"[r/{r['subreddit']}] {r['created']} | score {r['score']} | {r['num_comments']} comments")
        print(f"  {r['title']}")
        print(f"  {r['url']}")
        body = " ".join(r["selftext"].split())
        if body:
            print(f"  {body[:300]}")
        print()


def print_thread(t, a):
    print(f"[r/{t['subreddit']}] {t['created']} | score {t['score']} | {t['num_comments']} comments")
    print(t["title"])
    print(t["url"])
    body = " ".join(t["selftext"].split())
    if body:
        print(body[:2000])
    print("=" * 60)
    for c in t["comments"]:
        text = " ".join(c["body"].split())
        print(f'{"  " * c["depth"]}u/{c["author"]} [{c["score"]}] ({c["created"]}): {text[:a.maxlen]}')
    if t["collapsed"]:
        print(f"[{t['collapsed']} more comments collapsed; raise --more to fetch some of them]")


COMMANDS = {"search": (cmd_search, print_posts), "thread": (cmd_thread, print_thread),
            "listing": (cmd_listing, print_posts)}


def main(argv=None, make_scraper=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON instead of text")
    common.add_argument("--cache-dir", default=DEFAULT_CACHE,
                        help="where responses, cookies and the lock live (default: $REDDIT_SCRAPER_CACHE "
                             "or ~/.cache/reddit-scraper)")
    common.add_argument("--max-age", type=int, default=86400,
                        help="seconds a cached response stays fresh (default 86400)")
    common.add_argument("--no-cache", action="store_true", help="ignore cached responses")

    ap = argparse.ArgumentParser(prog="cli.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", parents=[common], help="search posts, across Reddit or inside subreddits")
    s.add_argument("query")
    s.add_argument("--sub", default="", help="comma-separated subreddits to search inside, e.g. iems,headphones")
    s.add_argument("--t", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    s.add_argument("--sort", default="relevance", choices=["relevance", "top", "new", "comments"])
    s.add_argument("--limit", type=int, default=15)

    t = sub.add_parser("thread", parents=[common], help="read one post and its comment tree")
    t.add_argument("url", help="post URL (www. or old.reddit.com) or permalink")
    t.add_argument("--top", type=int, default=40, help="most comments to return, best first (default 40)")
    t.add_argument("--maxlen", type=int, default=700, help="cut each comment here in text output (default 700)")
    t.add_argument("--more", type=int, default=0,
                   help="extra requests allowed for expanding collapsed comments (default 0)")

    li = sub.add_parser("listing", parents=[common], help="a subreddit's hot/new/top posts")
    li.add_argument("subreddit")
    li.add_argument("--sort", default="hot", choices=["hot", "new", "top", "rising", "controversial"])
    li.add_argument("--limit", type=int, default=25)
    li.add_argument("--pages", type=int, default=1)

    a = ap.parse_args(argv)
    run, show = COMMANDS[a.command]
    try:
        with PoliteSession(a.cache_dir, make_scraper, a.max_age, not a.no_cache) as sc:
            result = run(sc, a)
    except HumanVerificationRequired as e:
        print(f"stopped, Reddit wants a human: {e}", file=sys.stderr)
        return 3
    except ChallengeFormatChanged as e:
        print(f"stopped, the solver is out of date: {e}", file=sys.stderr)
        return 4
    except Exception as e:  # network errors, non-JSON answers after retries, ...
        print(f"failed: {e!r}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        show(result, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
