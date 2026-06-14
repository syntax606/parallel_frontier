"""arXiv collector: broad category net -> keyword gate -> pinned lab/org roster.

See SPEC "arXiv: broad net + pinned lab/org roster". Three layers:

  1. Net    — query `cat:cs.AI OR cat:cs.CL OR ...` over a rolling window.
  2. Gate   — a netted paper reaches digest:research only if its title/abstract
              hits a topic keyword; everything else goes to archive.
  3. Roster — papers matching a roster lab (author name OR alias in
              title/abstract/comment) are pinned (shown first, gate-exempt).

Config under `arxiv:` — categories, window_hours, max_results, keywords, roster.
arXiv asks for polite use (no faster than ~1 req / 3s), honored via http_get.
"""

from __future__ import annotations

import logging
import re
from calendar import timegm
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import feedparser

from collectors.base import Collector, http_get, sha256
from pipeline.store import Item

log = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"


class ArxivCollector(Collector):
    source = "arxiv"

    def __init__(
        self,
        categories: list[str],
        keywords: list[str],
        roster: list[dict],
        *,
        window_hours: int = 24,
        max_results: int = 300,
    ) -> None:
        self.categories = categories
        self.keywords = [k.lower() for k in keywords]
        self.roster = roster
        self.window_hours = window_hours
        self.max_results = max_results
        # Precompile boundary-guarded matchers. Roster pin signals AUTHORSHIP, so:
        #   * author names are matched against the author list;
        #   * aliases (model/lab names) are matched against the TITLE only —
        #     an alias in the abstract means the paper USES the model, not that
        #     it's FROM the lab (everyone benchmarks Qwen/DeepSeek now).
        self._author_pats = [
            self._boundary(name)
            for lab in roster for name in (lab.get("authors") or [])
        ]
        self._alias_pats = [
            self._boundary(alias)
            for lab in roster for alias in (lab.get("aliases") or [])
        ]

    def _query_url(self) -> str:
        search = " OR ".join(f"cat:{c}" for c in self.categories)
        params = {
            "search_query": search,
            "start": 0,
            "max_results": self.max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        return f"{ARXIV_API}?{urlencode(params)}"

    def fetch(self) -> list[Item]:
        # arXiv politeness: ~1 request / 3s.
        resp = http_get(self._query_url(), polite_delay=3.0, timeout=30)
        feed = feedparser.parse(resp.content)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.window_hours)
        items: list[Item] = []
        for entry in feed.entries:
            published = self._published(entry)
            # Results are sorted newest-first; stop once we pass the window.
            if published is not None and published < cutoff:
                break

            title = " ".join((entry.get("title") or "").split())
            summary = entry.get("summary") or ""
            url = entry.get("id") or entry.get("link")
            if not url or not title:
                continue

            pinned = self._roster_hit(entry, title)
            keyword = self._keyword_hit(title)
            if pinned or keyword:
                tier, cat = "digest", "research"
            else:
                tier, cat = "archive", "research"

            items.append(
                Item(
                    source=self.source,
                    url=url,
                    title=title,
                    content_hash=sha256(f"{title}\n{summary}"),
                    raw_excerpt=summary[:2000],
                    published_at=published.isoformat() if published else None,
                    tier=tier,
                    category=cat,
                    pinned=pinned,
                )
            )
        log.info("arxiv: %d in window (%d pinned)",
                 len(items), sum(1 for i in items if i.pinned))
        return items

    @staticmethod
    def _published(entry) -> Optional[datetime]:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed:
            return None
        return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)

    @staticmethod
    def _boundary(token: str) -> re.Pattern:
        # Guard against ASCII-alphanumeric neighbors so 'Ling' != 'scaLING' and
        # 'Seed-' != random seeds; CJK aliases (通义千问) match freely.
        return re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )

    def _keyword_hit(self, title: str) -> bool:
        # Title-only: a paper *about* a topic says so in its title; an abstract
        # mention is too permissive (cs.AI/CL are LLM-saturated). Keeps Research
        # to a readable size. Non-matches still land in archive (searchable).
        hay = title.lower()
        return any(k in hay for k in self.keywords)

    def _roster_hit(self, entry, title: str) -> bool:
        authors = " ".join(a.get("name", "") for a in entry.get("authors", []))
        if any(p.search(authors) for p in self._author_pats):
            return True
        return any(p.search(title) for p in self._alias_pats)


def collectors_from_config(cfg: dict) -> list[ArxivCollector]:
    a = cfg.get("arxiv")
    if not a or not a.get("categories"):
        return []
    return [
        ArxivCollector(
            categories=a["categories"],
            keywords=a.get("keywords") or [],
            roster=a.get("roster") or [],
            window_hours=a.get("window_hours", 24),
            max_results=a.get("max_results", 300),
        )
    ]
