"""Collector base class and shared HTTP plumbing.

Every collector subclasses `Collector` and implements `fetch() -> list[Item]`.
The `run(store)` wrapper is where the SPEC's robustness rules live:

  * one collector == one `source` == one heartbeat row;
  * failures are NEVER swallowed silently — an exception in fetch() records
    `source_runs.ok = 0` with the error and lets the pipeline continue;
  * HTTP goes through `http_get`, which enforces a timeout, retries with
    exponential backoff, a polite inter-request delay, and a real User-Agent
    (datacenter IPs on bare clients get tarpitted by gov sites / WAFs).
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import requests

from pipeline.store import Item, Store, UpsertStatus

log = logging.getLogger(__name__)

USER_AGENT = "ParallelFrontier/0.1 (+research aggregation; css216@nyu.edu)"
DEFAULT_TIMEOUT = 20          # seconds; a hung site must never hang the runner
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0         # base of exponential backoff, in seconds
DEFAULT_POLITE_DELAY = 1.0    # seconds to wait before each request

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def sha256(text: str) -> str:
    """Stable content hash. Caller is responsible for normalizing `text` first."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    polite_delay: float = DEFAULT_POLITE_DELAY,
    headers: Optional[dict] = None,
) -> requests.Response:
    """GET with timeout, retry/backoff, and a polite pre-request delay.

    Raises the last `requests.RequestException` if all attempts fail.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        if polite_delay:
            time.sleep(polite_delay)
        try:
            resp = _session.get(url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff ** attempt)
    assert last_exc is not None
    raise last_exc


@dataclass(slots=True)
class RunResult:
    """Outcome of a single collector run (also persisted to source_runs)."""

    source: str
    ok: bool
    item_count: int = 0
    error: Optional[str] = None
    statuses: Optional[list[UpsertStatus]] = None

    @property
    def changed(self) -> int:
        """Items that were new or changed — i.e. candidates for the digest/alert."""
        if not self.statuses:
            return 0
        return sum(1 for s in self.statuses if s in ("new", "changed"))


class Collector(ABC):
    """Base class for all sources.

    Subclasses set `self.source` (e.g. ``'rss:chinai'``, ``'cac'``) and implement
    `fetch()`.
    """

    source: str

    @abstractmethod
    def fetch(self) -> list[Item]:
        """Return the current items from this source. May raise on failure."""
        raise NotImplementedError

    def run(self, store: Store) -> RunResult:
        """Fetch, upsert, and record the heartbeat. Never raises for source errors."""
        try:
            items = self.fetch()
        except Exception as exc:  # loud failure -> heartbeat, pipeline continues
            log.error("collector %s failed: %s", self.source, exc)
            store.record_run(self.source, ok=False, error=f"{type(exc).__name__}: {exc}")
            return RunResult(self.source, ok=False, error=str(exc))

        statuses = [store.upsert(item) for item in items]
        store.record_run(self.source, ok=True, item_count=len(items))
        log.info("collector %s: %d items (%d new/changed)",
                 self.source, len(items), sum(1 for s in statuses if s != "unchanged"))
        return RunResult(self.source, ok=True, item_count=len(items), statuses=statuses)
