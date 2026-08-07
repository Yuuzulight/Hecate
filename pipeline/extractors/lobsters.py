"""Lobsters, via its public JSON feed.

Free, unauthenticated, and the links are in the listing, so it costs nothing per
post - unlike dev.to, which hides the body behind a request per article.

Coverage is narrow: a small site, heavily weighted towards systems and language
work. That is the trade, and it is a good one at this price, because the
signal-to-noise on what it does cover is higher than anywhere else available.

Like Hacker News, this produces mentions rather than repositories, so it goes in
MENTION_EXTRACTORS and never reaches the transformer.
"""

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor
from pipeline.extractors.hackernews import URL_IN_TEXT, canonical_repo_url

HOTTEST_URL = "https://lobste.rs/hottest.json"


class LobstersExtractor(Extractor):
    source = "lobsters"

    def fetch(self) -> list[dict]:
        response = self.session.get(HOTTEST_URL, timeout=TIMEOUT)
        if not response.ok:
            raise ExtractError(f"lobsters: feed returned {response.status_code}")

        payload = response.json()
        if not isinstance(payload, list):
            raise ExtractError(
                f"lobsters: expected a list of stories, got {type(payload).__name__}"
            )

        found = []
        for story in payload[: self.config.batch_size]:
            mention = self._transform_to_schema(story)
            if mention is not None:
                found.append(mention)
        return found

    def _candidate_url(self, raw: dict) -> str | None:
        direct = canonical_repo_url(raw.get("url") or "")
        if direct:
            return direct
        # - Text posts carry their links in the description rather than the url.
        for candidate in URL_IN_TEXT.findall(raw.get("description") or ""):
            resolved = canonical_repo_url(candidate)
            if resolved:
                return resolved
        return None

    def _transform_to_schema(self, raw: dict) -> dict | None:
        short_id = raw.get("short_id")
        target = self._candidate_url(raw)
        if not short_id or not target:
            return None

        return {
            "id": f"lobsters_{short_id}",
            "platform": "lobsters",
            "repository_id": None,
            "target_url": target,
            "title": raw.get("title"),
            "url": raw.get("short_id_url") or raw.get("comments_url"),
            "score": raw.get("score"),
            "comments": raw.get("comment_count"),
            "author": (raw.get("submitter_user") or {}).get("username")
            if isinstance(raw.get("submitter_user"), dict)
            else raw.get("submitter_user"),
            # - Tags are the closest thing Lobsters has to a subreddit.
            "channel": ",".join(raw.get("tags") or []) or None,
            "posted_at": raw.get("created_at"),
            "extracted_at": self.now(),
        }
