"""Russia edition sources — kremlin.ru, TASS, government.ru (region='russia').

These are official/state RSS feeds that are ~99% non-AI, so they are title-gated
to a Russian+English AI/tech keyword list (config.yaml russia.keywords): only
matching items reach the digest's 🇷🇺 Russia block; the rest go to archive.

NB on the gate: bare 2-char tokens like Russian "ИИ" (AI) are NOT used — lowercased
they collide inside "России" (Russia). We use stems like "искусственн"/"нейросет".
"""

from __future__ import annotations

from collectors.rss import RssCollector, _slug


def collectors_from_config(cfg: dict) -> list[RssCollector]:
    r = cfg.get("russia") or {}
    keywords = r.get("keywords") or []
    collectors: list[RssCollector] = []
    for src in r.get("sources") or []:
        collectors.append(
            RssCollector(
                src["name"],
                src["url"],
                src.get("category", "regulation"),
                keywords=keywords,
                region="russia",
                source=f"ru:{_slug(src['url'])}",
            )
        )
    return collectors
