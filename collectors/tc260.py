"""TC260 (National Cybersecurity Standardization TC) docket watcher.

Watches TC260 draft-standard / notice pages listed in
config.yaml -> regulation.tc260_pages. A normalized-content change is an ALERT
(see collectors.docket).
"""

from __future__ import annotations

from collectors.docket import DocketCollector

SOURCE = "tc260"


def collectors_from_config(cfg: dict) -> list[DocketCollector]:
    pages = (cfg.get("regulation") or {}).get("tc260_pages") or []
    return [DocketCollector(SOURCE, pages)] if pages else []
