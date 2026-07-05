"""Hugging Face model-release collector for the lab orgs in config (huggingface.orgs).

For each org, the HF API returns its most-recently-updated models; a model id we
haven't seen before is a new release. Items are digest:research and pinned (every
watched org is a roster lab), so they surface at the top of Research alongside
arXiv roster papers and GitHub releases.

One collector, source 'huggingface', one heartbeat row. Per-org failures are
logged and skipped. HF's public API needs no auth for these read calls.

NOTE: HF org handles differ from GitHub — e.g. Zhipu is `zai-org` on HF (not
THUDM), MiniMax is `MiniMaxAI` (not MiniMax-AI). See config.yaml.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

from collectors.base import Collector, http_get, sha256
from pipeline.store import Item

log = logging.getLogger(__name__)

API = "https://huggingface.co/api/models"


class HuggingFaceCollector(Collector):
    source = "huggingface"

    def __init__(self, orgs: list[str], *, per_org: int = 8) -> None:
        self.orgs = orgs
        self.per_org = per_org
        self.token = os.environ.get("HF_TOKEN")

    def _headers(self) -> dict | None:
        return {"Authorization": f"Bearer {self.token}"} if self.token else None

    def fetch(self) -> list[Item]:
        items: list[Item] = []
        for org in self.orgs:
            params = urlencode({
                "author": org,
                "sort": "lastModified",
                "direction": -1,
                "limit": self.per_org,
            })
            try:
                models = http_get(
                    f"{API}?{params}", headers=self._headers(), polite_delay=0.5
                ).json()
            except Exception as exc:  # per-org isolation
                log.warning("huggingface org %s failed: %s", org, exc)
                continue
            for m in models:
                mid = m.get("id")
                if not mid:
                    continue
                items.append(
                    Item(
                        source=self.source,
                        url=f"https://huggingface.co/{mid}",
                        title=mid,  # e.g. "deepseek-ai/DeepSeek-V4-Pro"
                        content_hash=sha256(mid),  # one item per model id
                        published_at=m.get("lastModified"),
                        tier="digest",
                        category="research",
                        pinned=True,
                    )
                )
        log.info("huggingface: %d models across %d orgs", len(items), len(self.orgs))
        return items


def collectors_from_config(cfg: dict) -> list[HuggingFaceCollector]:
    hf = cfg.get("huggingface") or {}
    orgs = hf.get("orgs") or []
    return [HuggingFaceCollector(orgs, per_org=hf.get("per_org", 8))] if orgs else []
