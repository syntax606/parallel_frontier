"""Shared watcher for regulatory docket pages (CAC, TC260).

These are the alert-worthy sources: a page is watched as a single item keyed by
its URL, and a change in its *normalized* content (see pipeline.normalize) flips
the store's upsert to "changed" — which is the ALERT trigger. The digest reads
alert-tier changes via `store.alerts_since`.

Each watched page is configured as:

    { name: "CAC 公开征求意见", url: "https://...", selector: "div.content" }

`selector` is optional but strongly recommended — it pins the diff to the real
content region so template churn doesn't fire false alerts. `cac.py` and
`tc260.py` are thin wrappers that pass their agency's page list and source name.
"""

from __future__ import annotations

from typing import Optional

from collectors.base import Collector, http_get
from pipeline.normalize import content_hash, main_text
from pipeline.store import Item


class DocketCollector(Collector):
    def __init__(self, source: str, pages: list[dict]) -> None:
        self.source = source
        self.pages = pages

    def fetch(self) -> list[Item]:
        items: list[Item] = []
        for page in self.pages:
            url = page["url"]
            selector: Optional[str] = page.get("selector")
            resp = http_get(url)
            # Pass bytes so lxml can detect GBK/GB2312/UTF-8 from the markup.
            html = resp.content
            items.append(
                Item(
                    source=self.source,
                    url=url,
                    title=page.get("name", url),
                    content_hash=content_hash(html, selector),
                    raw_excerpt=main_text(html, selector)[:2000],
                    tier="alert",
                    category="regulation",
                )
            )
        return items
