"""GitHub releases collector for the lab orgs in config (github.orgs).

For each org we look at its most recently-pushed repos and collect their latest
releases — the high-signal "a lab shipped something" event. Items are tiered
digest:research and pinned (every watched org is a roster lab by definition), so
releases surface at the top of Research alongside roster papers.

One collector, source 'github', one heartbeat row. Per-org failures are logged
and skipped so one bad org never sinks the run. Uses GITHUB_TOKEN (5000 req/hr)
when present; falls back to unauthenticated (60 req/hr) with reduced scope.

Model-card commit tracking (SPEC mentions it) is a v2 refinement; v1 covers
releases, which is the primary release signal.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from collectors.base import Collector, http_get, sha256
from pipeline.store import Item

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GithubReleasesCollector(Collector):
    source = "github"

    def __init__(
        self,
        orgs: list[str],
        *,
        repos_per_org: int = 15,
        releases_per_repo: int = 2,
        token: Optional[str] = None,
    ) -> None:
        self.orgs = orgs
        self.token = token or os.environ.get("GITHUB_TOKEN")
        # Unauthenticated is rate-limited to 60/hr; shrink scope to stay under it.
        if self.token:
            self.repos_per_org = repos_per_org
            self.releases_per_repo = releases_per_repo
        else:
            self.repos_per_org = 3
            self.releases_per_repo = 1
            log.warning("github: no GITHUB_TOKEN; reduced scope (60 req/hr limit)")

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get_json(self, path: str):
        # GitHub is fast and rate-limited by quota, not politeness; skip the delay.
        resp = http_get(f"{API}{path}", headers=self._headers(), polite_delay=0)
        return resp.json()

    def fetch(self) -> list[Item]:
        items: list[Item] = []
        for org in self.orgs:
            try:
                repos = self._get_json(
                    f"/orgs/{org}/repos?sort=pushed&per_page={self.repos_per_org}"
                )
                for repo in repos:
                    name = repo["name"]
                    rels = self._get_json(
                        f"/repos/{org}/{name}/releases?per_page={self.releases_per_repo}"
                    )
                    for rel in rels:
                        if rel.get("draft"):
                            continue
                        items.append(self._release_item(org, name, rel))
            except Exception as exc:  # per-org isolation
                log.warning("github org %s failed: %s", org, exc)
        log.info("github: %d releases across %d orgs", len(items), len(self.orgs))
        return items

    def _release_item(self, org: str, repo: str, rel: dict) -> Item:
        tag = rel.get("tag_name") or ""
        name = rel.get("name") or tag
        url = rel.get("html_url")
        return Item(
            source=self.source,
            url=url,
            title=f"{org}/{repo}: {name}",
            content_hash=sha256(url),  # a release URL is immutable once published
            raw_excerpt=(rel.get("body") or "")[:2000],
            published_at=rel.get("published_at"),
            tier="digest",
            category="research",
            pinned=True,  # every watched org is a roster lab
        )


def collectors_from_config(cfg: dict) -> list[GithubReleasesCollector]:
    orgs = (cfg.get("github") or {}).get("orgs") or []
    return [GithubReleasesCollector(orgs)] if orgs else []
