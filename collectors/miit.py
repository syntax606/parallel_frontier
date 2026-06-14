"""MIIT (Ministry of Industry and Information Technology) collector.

MIIT posts AI-relevant policy/notices inline in its announcement listings, like
CAC — so it uses a ListingCollector: rows -> items, AI items to digest:regulation,
AI comment-periods promoted to alert. Configure pages in
config.yaml -> regulation.miit_pages as {name, url, row_selector}.
"""

from __future__ import annotations

from collectors.listing import ListingCollector

SOURCE = "miit"


def collectors_from_config(cfg: dict) -> list[ListingCollector]:
    reg = cfg.get("regulation") or {}
    keywords = reg.get("keywords") or []
    collectors: list[ListingCollector] = []
    for page in reg.get("miit_pages") or []:
        collectors.append(
            ListingCollector(
                SOURCE,
                page["url"],
                page["row_selector"],
                digest_keywords=keywords,
            )
        )
    return collectors
