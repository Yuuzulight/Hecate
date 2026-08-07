"""Measure how often name matching gets it wrong.

The URL-resolved mentions are ground truth: for each one we already know which
repository the post is about, because it linked it. Running name matching over
those same posts, blind to the link, gives a directly comparable answer.

    python -m tools.measure_name_matching

Reports precision on the posts it chose to answer, and how many it declined.
Declining is not a failure - the whole design is that ambiguity produces no
answer rather than a guess.
"""

from pipeline.config import Config
from pipeline.loader import PostgreSQLLoader
from pipeline.matching import resolve_by_name


def main() -> int:
    loader = PostgreSQLLoader(Config())
    loader.connect()
    try:
        names = loader.repository_names()
        with loader.transaction() as cur:
            cur.execute("""
                SELECT title, repository_id
                FROM social_mentions
                WHERE repository_id IS NOT NULL AND title IS NOT NULL
            """)
            truth = cur.fetchall()
    finally:
        loader.close()

    if not truth:
        print("no URL-resolved mentions to measure against yet")
        return 1

    correct = wrong = declined = 0
    mistakes = []
    for title, actual in truth:
        guess = resolve_by_name(title, names)
        if guess is None:
            declined += 1
        elif guess == actual:
            correct += 1
        else:
            wrong += 1
            mistakes.append((title, actual, guess))

    answered = correct + wrong
    print(f"ground truth posts : {len(truth)}")
    print(f"  answered         : {answered}")
    print(f"  declined         : {declined}")
    if answered:
        print(f"  correct          : {correct}")
        print(f"  wrong            : {wrong}")
        print(f"  false positive % : {100 * wrong / answered:.1f}")
    else:
        print("  no post was distinctive enough to name-match at all")

    for title, actual, guess in mistakes[:10]:
        print(f"    MISS {title[:60]!r}: said {guess}, was {actual}")

    print(f"names considered   : {len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
