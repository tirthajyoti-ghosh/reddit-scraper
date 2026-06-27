"""Fetch the hottest r/python post's comment tree (breadth + depth) and print it threaded."""
from reddit_scraper import RedditScraper


def walk(nodes, indent=0):
    for n in nodes:
        body = " ".join(n["body"].split())[:70]
        print("  " * indent + f'[{n["score"]:>4}] u/{n["author"]}: {body}')
        walk(n["replies"], indent + 1)


if __name__ == "__main__":
    sc = RedditScraper(min_delay=2, max_delay=5)
    post = next(iter(sc.listing("python", sort="hot", limit=5, pages=1)))
    print(f'\n# {post["title"]}\n')

    _, roots, leftover = sc.comments(post["permalink"], expand_more=True, max_more_calls=20)
    walk(roots)

    still = sum(m.get("count", 1) for m in leftover)
    print(f'\n[{still} comments still collapsed — raise max_more_calls to pull them]')
