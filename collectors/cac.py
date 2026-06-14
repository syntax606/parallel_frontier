"""CAC (Cyberspace Administration of China) collector.

CAC has no dedicated comment-period column — solicitations are posted inline in
its policy feeds (e.g. 政策法规). So CAC uses a ListingCollector: each row becomes
an item, AI items go to digest:regulation, and AI comment-periods promote to
alert. Configure pages in config.yaml -> regulation.cac_pages as
{name, url, row_selector}.
"""

from __future__ import annotations

from collectors.listing import ListingCollector

SOURCE = "cac"


def collectors_from_config(cfg: dict) -> list[ListingCollector]:
    reg = cfg.get("regulation") or {}
    keywords = reg.get("keywords") or []
    collectors: list[ListingCollector] = []
    for page in reg.get("cac_pages") or []:
        collectors.append(
            ListingCollector(
                SOURCE,
                page["url"],
                page["row_selector"],
                digest_keywords=keywords,
            )
        )
    return collectors
