"""TC260 drafting-committee (起草单位) extractor — the hardest response signal.

For each open TC260 draft, this fetches the detail page, downloads the 编制说明
(drafting-explanation) .docx, and extracts the 起草单位 / 参与单位 list. When a
watchlisted org (a tracked lab or key institute — config.yaml
regulation.drafting_watchlist) is on the committee, the item is promoted to
ALERT and pinned; a lab's name on an AI standard committee is high signal.

Drafts with no watchlist match are archived (searchable, not emailed) so the full
committee record is retained. One collector, source 'tc260-drafting'. Runs on the
daily full pass only (it downloads ~10-15 .docx), not the 4-hourly poll.
"""

from __future__ import annotations

import io
import logging
import re

import docx
from bs4 import BeautifulSoup

from collectors.base import Collector, http_get, sha256
from pipeline.store import Item

log = logging.getLogger(__name__)

BASE = "https://www.tc260.org.cn"
LIST_URL = f"{BASE}/portal/suggestion"
DETAIL_URL = f"{BASE}/portal/suggestion-detail/{{id}}"

# 起草单位 block: from the first of these markers up to the first stop marker.
_UNIT_STARTS = ("起草单位", "本标准由", "参与单位", "参编单位", "牵头")
_UNIT_STOPS = ("主要起草人", "起草过程", "任务分工", "1.4", "1.3", "编制原则")


class Tc260DraftingCollector(Collector):
    source = "tc260-drafting"

    def __init__(self, watchlist: list[str], *, max_drafts: int = 15) -> None:
        self.watchlist = watchlist
        self.max_drafts = max_drafts

    def fetch(self) -> list[Item]:
        soup = BeautifulSoup(http_get(LIST_URL).content, "lxml")
        drafts: list[tuple[str, str]] = []
        for a in soup.select("div.right a[onclick*=jumpDetail]"):
            m = re.search(r"jumpDetail\('([^']+)'", a.get("onclick", ""))
            if m:
                drafts.append((m.group(1), a.get_text(strip=True)))

        items: list[Item] = []
        for did, title in drafts[: self.max_drafts]:
            try:
                units = self._committee_units(did)
            except Exception as exc:  # per-draft isolation
                log.warning("tc260-drafting %s failed: %s", did, exc)
                continue
            if not units:
                continue
            matched = [w for w in self.watchlist if w in units]
            items.append(
                Item(
                    source=self.source,
                    url=DETAIL_URL.format(id=did),
                    title=(("⚑ " + "、".join(matched) + " ｜ ") if matched else "") + title,
                    content_hash=sha256(units),  # re-alert if the committee changes
                    raw_excerpt=("命中: " + "、".join(matched) + "\n\n" if matched else "") + units[:1800],
                    tier="alert" if matched else "archive",
                    category="regulation",
                    pinned=bool(matched),
                )
            )
        n_hit = sum(1 for i in items if i.tier == "alert")
        log.info("tc260-drafting: %d drafts parsed, %d committee match(es)", len(items), n_hit)
        return items

    def _committee_units(self, detail_id: str) -> str:
        """Return the normalized 起草单位 text for a draft, or '' if unavailable."""
        soup = BeautifulSoup(http_get(DETAIL_URL.format(id=detail_id)).content, "lxml")
        href = None
        for a in soup.find_all("a", href=True):
            if "编制说明" in a.get_text() and "downloadFile" in a["href"]:
                href = a["href"]
                break
        if not href:
            return ""
        url = href if href.startswith("http") else BASE + href
        data = http_get(url).content
        doc = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        return _extract_section(text, _UNIT_STARTS, _UNIT_STOPS)


def _extract_section(text: str, starts: tuple[str, ...], stops: tuple[str, ...]) -> str:
    for marker in starts:
        i = text.find(marker)
        if i < 0:
            continue
        end = len(text)
        for stop in stops:
            k = text.find(stop, i + len(marker))
            if k >= 0:
                end = min(end, k)
        return " ".join(text[i:end].split())
    return ""


def collectors_from_config(cfg: dict) -> list[Tc260DraftingCollector]:
    watchlist = (cfg.get("regulation") or {}).get("drafting_watchlist") or []
    return [Tc260DraftingCollector(watchlist)] if watchlist else []
