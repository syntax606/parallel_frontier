"""安全内参 (secrss.com) collector — governance/security trade press.

No RSS, but the homepage server-renders its article list, so it's a
ListingCollector: rows -> items, keyword-gated to the AI/governance cut
(config.yaml secrss.keywords) into digest:discourse; the rest -> archive.
Never alerts (comment_markers disabled — this is media, not a docket).
"""

from __future__ import annotations

from collectors.listing import ListingCollector

SOURCE = "secrss"
ROW_SELECTOR = "a[href*='/articles/']"


def collectors_from_config(cfg: dict) -> list[ListingCollector]:
    c = cfg.get("secrss") or {}
    if not c.get("url"):
        return []
    return [
        ListingCollector(
            SOURCE,
            c["url"],
            ROW_SELECTOR,
            digest_keywords=c.get("keywords") or [],
            comment_markers=(),      # media source — never promote to alert
            category="discourse",
        )
    ]
