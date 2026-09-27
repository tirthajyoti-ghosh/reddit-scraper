"""
Hardened Reddit scraper (educational / research use).

Handles, in order:
  Wall 2 (TLS/HTTP fingerprint) : curl_cffi impersonating Chrome
  Session warmup                : solve the homepage challenge to earn the verified cookies
                                  (the JSON endpoints hard-403 an un-warmed client)
  Wall 1 (JS challenge)         : regex fast-path, with a Node JS-eval fallback (solve.js)
  Politeness                    : jittered delay, 429 Retry-After, exponential backoff
  Pagination                    : Reddit listing 'after'/'count' cursor

For anything real/at scale, use the official API instead: https://www.reddit.com/dev/api
"""
import re, time, random, subprocess, os, sys
from curl_cffi import requests

HERE        = os.path.dirname(os.path.abspath(__file__))
NODE_SOLVER = os.path.join(HERE, "solve.js")
BASE        = "https://www.reddit.com"


class HumanVerificationRequired(RuntimeError):
    """Reddit answered with a human-verification page (reCAPTCHA) instead of content."""


class ChallengeFormatChanged(RuntimeError):
    """Reddit served a JS challenge this solver no longer understands."""


# Markers of the reCAPTCHA "Prove your humanity" page (seen from 27 Sep 2026).
HUMAN_CHECK_MARKERS = ("google.com/recaptcha", 'action="/?captcha=1"')

class RedditScraper:
    def __init__(self, impersonate="chrome", min_delay=2.0, max_delay=5.0, verbose=True):
        self.s = requests.Session(impersonate=impersonate)   # browser TLS + HTTP2 fingerprint
        self.min_delay, self.max_delay, self.verbose = min_delay, max_delay, verbose
        self._warmed = False

    def _log(self, *a):
        if self.verbose: print("[scraper]", *a, file=sys.stderr)

    def _sleep(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    # ---- challenge solving (Wall 1) ---------------------------------------
    def _solution(self, html):
        m = re.search(r'e\s*\+\s*e\)\s*\(\s*"([0-9a-f]+)"', html)        # Tier 1: cheap regex
        if m:
            return m.group(1) + m.group(1)
        self._log("regex miss -> Node JS-eval fallback")                # Tier 2: run their real JS
        out = subprocess.run(["node", NODE_SOLVER, "-"], input=html,
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            raise RuntimeError(f"node solver failed: {out.stderr.strip()}")
        return out.stdout.strip()

    def _solve(self, html):
        token = re.search(r'name="token"\s+value="([0-9a-f]+)"', html)
        if not token:
            raise ChallengeFormatChanged(
                "Reddit served a JS challenge without the hidden token field this solver expects. "
                "The challenge format has changed; _solve() and solve.js need updating.")
        sol = self._solution(html)
        self._log(f"solving challenge (solution={sol[:16]}...)")
        self.s.get(BASE + "/", params={"solution": sol, "js_challenge": "1",
                                       "token": token.group(1), "jsc_orig_r": ""}, timeout=30)

    def _stop_if_human_check(self, r, url):
        """A reCAPTCHA page is a wall for humans, not a puzzle for this client: stop, don't retry."""
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" in ctype and any(m in r.text for m in HUMAN_CHECK_MARKERS):
            raise HumanVerificationRequired(
                f"Reddit answered {url} with a human-verification page (reCAPTCHA). "
                "This scraper does not solve CAPTCHAs, and retrying only makes the block last longer. "
                "Wait before trying again, or use Reddit's official API (see README).")

    def warmup(self):
        self._log("warming up session on homepage")
        r = self.s.get(BASE + "/", timeout=30)
        self._stop_if_human_check(r, BASE + "/")
        if "js_challenge" in r.text:
            self._solve(r.text)
            r = self.s.get(BASE + "/", timeout=30)
            self._stop_if_human_check(r, BASE + "/")
        self._warmed = (r.status_code == 200 and "js_challenge" not in r.text)
        self._log(f"warmup {'OK' if self._warmed else 'FAILED'}  cookies={list(self.s.cookies.keys())}")

    # ---- core fetch (with retries + self-healing) -------------------------
    def get(self, url, params=None, max_retries=5):
        if not self._warmed:
            self.warmup()
        r = None
        for attempt in range(max_retries):
            self._sleep()
            r = self.s.get(url, params=params, timeout=30)
            self._stop_if_human_check(r, url)
            if r.status_code == 200 and "js_challenge" not in r.text:
                return r
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", 5)) * (attempt + 1)
                self._log(f"429 -> sleep {wait}s"); time.sleep(wait); continue
            if "js_challenge" in r.text:
                self._solve(r.text); continue
            if r.status_code == 403:                                     # cookie likely expired
                self._log("403 -> re-warming session"); self._warmed = False; self.warmup(); continue
            self._log(f"status {r.status_code} -> backoff {2**attempt}s"); time.sleep(2 ** attempt)
        return r

    # ---- pagination -------------------------------------------------------
    def listing(self, subreddit, sort="hot", limit=25, pages=2):
        after, count = None, 0
        for page in range(pages):
            params = {"limit": limit, "count": count, "raw_json": 1}
            if after: params["after"] = after
            r = self.get(f"{BASE}/r/{subreddit}/{sort}.json", params=params)
            try:
                data = r.json()
            except Exception:
                self._log(f"non-JSON (status {getattr(r,'status_code','?')}); stop"); break
            kids = data.get("data", {}).get("children", [])
            self._log(f"page {page+1}: {len(kids)} posts (after={after})")
            for c in kids:
                yield c["data"]
            after = data.get("data", {}).get("after")
            count += len(kids)
            if not after: break

    # ---- comment trees ----------------------------------------------------
    def _comments_url(self, post):
        if post.startswith("http"):  base = post
        elif post.startswith("/"):   base = BASE + post
        else:                        base = f"{BASE}/comments/{post.split('_')[-1]}"
        return base.rstrip("/") + ".json"

    def _mk_node(self, d):
        return {"name": d["name"], "id": d["id"], "parent_id": d.get("parent_id"),
                "author": d.get("author"), "score": d.get("score"), "body": d.get("body", ""),
                "created_utc": d.get("created_utc"), "permalink": d.get("permalink")}

    def _flatten(self, children, by_name, mores):
        """Walk the loaded forest -> by_name{fullname: node} + collect 'more' stubs."""
        for c in children:
            kind, d = c.get("kind"), c.get("data", {})
            if kind == "t1":
                by_name.setdefault(d["name"], self._mk_node(d))
                rep = d.get("replies")
                if isinstance(rep, dict):                       # nested replies listing
                    self._flatten(rep["data"]["children"], by_name, mores)
            elif kind == "more":                                # 'load more' (has ids) | 'continue thread' (empty)
                mores.append({"parent_id": d.get("parent_id"), "ids": d.get("children", []),
                              "count": d.get("count", 0)})

    def _morechildren(self, link_id, child_ids):
        """Expand collapsed comments through the public morechildren endpoint (~100 ids/call)."""
        things = []
        for i in range(0, len(child_ids), 100):
            r = self.get(f"{BASE}/api/morechildren.json",
                         params={"api_type": "json", "link_id": link_id,
                                 "children": ",".join(child_ids[i:i + 100])})
            try:
                things += r.json()["json"]["data"]["things"]
            except Exception:
                self._log("morechildren parse failed")
        return things

    def _focused(self, permalink, limit):
        """Refetch the thread focused on one comment -> its previously-hidden subtree.
        This is how depth-capped 'continue this thread' stubs get expanded."""
        r = self.get(BASE + permalink.rstrip("/") + ".json", params={"limit": limit, "raw_json": 1})
        try:
            return r.json()[1]["data"]["children"]
        except Exception:
            self._log("focused refetch parse failed"); return []

    def _threaded(self, by_name, link_id):
        """Re-nest a flat {fullname: node} map into a forest with depth set."""
        for n in by_name.values(): n["replies"] = []
        roots = []
        for n in by_name.values():
            parent = by_name.get(n["parent_id"])
            (roots if parent is None else parent["replies"]).append(n)
        def setdepth(n, d):
            n["depth"] = d
            for ch in n["replies"]: setdepth(ch, d + 1)
        for r in roots: setdepth(r, 0)
        return roots

    def comments(self, post, sort="confidence", limit=500, expand_more=False, max_more_calls=40):
        """Fetch a post's comment tree. Returns (link_data, threaded_roots, leftover).
        expand_more=True chases BOTH stub kinds (interleaved) until exhausted or budget hit:
          - 'load more'            -> batched morechildren API
          - 'continue this thread' -> focused refetch of that comment's subtree"""
        r = self.get(self._comments_url(post), params={"limit": limit, "raw_json": 1, "sort": sort})
        data = r.json()
        link = data[0]["data"]["children"][0]["data"]
        link_id = link["name"]                                  # t3_xxxx
        by_name, mores = {}, []
        self._flatten(data[1]["data"]["children"], by_name, mores)

        leftover = mores
        if expand_more:
            def route(ms, ids_q, cont_q):                       # split stubs by how each expands
                for m in ms:
                    if m["ids"]:                    ids_q += m["ids"]            # -> morechildren
                    elif m["parent_id"] in by_name: cont_q.append(m["parent_id"])# -> focused refetch
            ids_q, cont_q, seen, calls = [], [], set(), 0
            route(mores, ids_q, cont_q)
            while (ids_q or cont_q) and calls < max_more_calls:
                new = []
                if cont_q and (not ids_q or calls % 2 == 1):    # interleave depth with breadth
                    focus = cont_q.pop(0)
                    if focus in seen: continue
                    seen.add(focus); node = by_name.get(focus)
                    if not (node and node.get("permalink")): continue
                    calls += 1
                    self._flatten(self._focused(node["permalink"], limit), by_name, new)
                    route([m for m in new if m["parent_id"] != focus], ids_q, cont_q)
                else:                                           # 'load more' batch
                    batch, ids_q = ids_q[:100], ids_q[100:]; calls += 1
                    for t in self._morechildren(link_id, batch):
                        k, d = t.get("kind"), t.get("data", {})
                        if   k == "t1":   by_name.setdefault(d["name"], self._mk_node(d))
                        elif k == "more": new.append({"parent_id": d.get("parent_id"),
                                                      "ids": d.get("children", []), "count": d.get("count", 0)})
                    route(new, ids_q, cont_q)
                self._log(f"expand {calls}: {len(by_name)} comments "
                          f"(load-more q={len(ids_q)}, continue q={len(cont_q)})")
            leftover = ([{"ids": ids_q, "count": len(ids_q)}] if ids_q else []) + \
                       [{"parent_id": p, "kind": "continue"} for p in cont_q]
        return link, self._threaded(by_name, link_id), leftover

if __name__ == "__main__":
    sc = RedditScraper(min_delay=1.5, max_delay=3.0)
    ids = []
    for i, p in enumerate(sc.listing("python", sort="hot", limit=10, pages=2), 1):
        ids.append(p["id"])
        if i <= 13:
            print(f'{i:2}. [{p["score"]:>6}] r/{p["subreddit"]:14} {p["title"][:62]}')
    print(f'\ntotal={len(ids)}  unique={len(set(ids))}  pagination={"OK (no dupes across pages)" if len(set(ids))==len(ids) else "DUPLICATE!"}')
