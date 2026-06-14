"""Generic RSS/Atom collector.

One `RssCollector` per feed, so each feed is its own `source` (``rss:<slug>``)
and gets its own heartbeat row. Discourse feeds (机器之心-style media, 量子位) and
rival newsletters (ChinAI, ChinaTalk, …) differ only in `category`.

Feeds are listed in config.yaml under `rss.discourse` and `rss.rivals`; build
the collector list with `collectors_from_config(cfg)`.
"""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone
from time import struct_time
from typing import Optional
from urllib.parse import urlparse

import feedparser

from collectors.base import Collector, http_get, sha256
from pipeline.store import Category, Item


def _slug(url: str) -> str:
    """Derive a stable source slug from a feed URL's hostname.

    chinai.substack.com -> 'chinai'   www.chinatalk.media -> 'chinatalk'
    """
    host = (urlparse(url).hostname or url).lower()
    parts = host.split(".")
    if parts and parts[0] == "www":
        parts = parts[1:]
    return parts[0] if parts else host


def _entry_date(entry) -> Optional[str]:
    """Best-effort published/updated timestamp as UTC ISO-8601."""
    parsed: Optional[struct_time] = (
        entry.get("published_parsed") or entry.get("updated_parsed")
    )
    if not parsed:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc).isoformat()


class RssCollector(Collector):
    def __init__(self, name: str, url: str, category: Category) -> None:
        self.name = name
        self.url = url
        self.category = category
        self.source = f"rss:{_slug(url)}"

    def fetch(self) -> list[Item]:
        resp = http_get(self.url)
        feed = feedparser.parse(resp.content)

        items: list[Item] = []
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            title = (entry.get("title") or "").strip()
            summary = entry.get("summary") or ""
            # Hash title+summary: an edited post re-surfaces as a changed version.
            content_hash = sha256(f"{title}\n{summary}")
            items.append(
                Item(
                    source=self.source,
                    url=link,
                    title=title,
                    content_hash=content_hash,
                    raw_excerpt=summary[:2000],
                    published_at=_entry_date(entry),
                    tier="digest",
                    category=self.category,
                )
            )
        return items


def collectors_from_config(cfg: dict) -> list[RssCollector]:
    """Build one RssCollector per configured feed."""
    rss = cfg.get("rss") or {}
    collectors: list[RssCollector] = []
    for feed in rss.get("discourse") or []:
        collectors.append(RssCollector(feed["name"], feed["url"], "discourse"))
    for feed in rss.get("rivals") or []:
        collectors.append(RssCollector(feed["name"], feed["url"], "rivals"))
    return collectors
