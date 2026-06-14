"""Listing collector: parse a server-rendered index page into individual items.

Used for sources (e.g. CAC) that post comment solicitations *inline* among
general policy news, with no dedicated low-churn docket page. Watching the whole
list as one alert would over-fire and break the < 2/week goal, so instead we:

  * parse each row into its own item (URL = the article link);
  * tier it by title keywords —
      alert   : title has an AI keyword AND a comment-period marker (rare);
      digest  : title has an AI keyword (regulation digest);
      archive : everything else (searchable, never emailed — feeds the dataset).

This keeps ALERT to genuine AI comment-periods while still capturing the full
policy stream for the archive.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from collectors.base import Collector, http_get, sha256
from pipeline.store import Category, Item, Tier

# Comment-solicitation markers that, combined with an AI keyword, promote to alert.
DEFAULT_COMMENT_MARKERS = ("公开征求意见", "征求意见", "征集意见", "向社会公开征求")


class ListingCollector(Collector):
    def __init__(
        self,
        source: str,
        url: str,
        row_selector: str,
        *,
        digest_keywords: list[str],
        comment_markers: tuple[str, ...] = DEFAULT_COMMENT_MARKERS,
        category: Category = "regulation",
        min_title_len: int = 8,
    ) -> None:
        self.source = source
        self.url = url
        self.row_selector = row_selector
        self.digest_keywords = digest_keywords
        self.comment_markers = comment_markers
        self.category = category
        self.min_title_len = min_title_len

    def fetch(self) -> list[Item]:
        resp = http_get(self.url)
        soup = BeautifulSoup(resp.content, "lxml")

        items: list[Item] = []
        seen: set[str] = set()
        for a in soup.select(self.row_selector):
            title = a.get_text(strip=True)
            href = a.get("href")
            if not href or len(title) < self.min_title_len:
                continue
            link = urljoin(self.url, href)
            if link in seen:
                continue
            seen.add(link)
            items.append(
                Item(
                    source=self.source,
                    url=link,
                    title=title,
                    # Listing gives only the title; body fetch is a v2 enrichment.
                    content_hash=sha256(title),
                    raw_excerpt=title,
                    tier=self._tier(title),
                    category=self.category,
                )
            )
        return items

    def _tier(self, title: str) -> Tier:
        is_ai = any(k in title for k in self.digest_keywords)
        is_comment = any(m in title for m in self.comment_markers)
        if is_ai and is_comment:
            return "alert"
        if is_ai:
            return "digest"
        return "archive"
