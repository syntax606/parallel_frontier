"""Render (and send) the one daily digest — the only email.

Sections, in order (SPEC "The daily digest"):
  1. ⚡ Docket        — alert-tier changes in the window (usually empty)
  2. Regulation      — new digest:regulation items
  3. Research        — new digest:research items (arXiv net + roster)
  4. Discourse       — new digest:discourse items
  5. Rivals published— titles only, so you never duplicate them unknowingly
  6. Heartbeat       — per-source last run (✓/✗ + count); NOT a dashboard, it
                       rides inside the one email so a broken collector is visible

`build_digest` is pure (store -> Digest); `send_via_resend` is the optional
delivery side, gated on RESEND_API_KEY.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from pipeline.store import Store

# Visual constants kept inline (email clients ignore <style> unreliably).
_WRAP = "max-width:680px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;line-height:1.5"
_H2 = "font-size:15px;text-transform:uppercase;letter-spacing:.04em;color:#666;border-bottom:1px solid #e5e5e5;padding-bottom:4px;margin:28px 0 12px"
_MUTED = "color:#888;font-size:13px"


@dataclass(slots=True)
class Digest:
    subject: str
    html: str
    has_docket: bool


def _esc(s: Optional[str]) -> str:
    return html.escape(s or "")


def _item_row(url: str, title: str, gloss: str = "", source: str = "", pinned: bool = False) -> str:
    src = f' <span style="{_MUTED}">· {_esc(source)}</span>' if source else ""
    gloss_html = (
        f'<div style="{_MUTED};margin-top:2px">{_esc(gloss)}</div>' if gloss else ""
    )
    badge = '<span style="color:#b8860b" title="roster lab">★ </span>' if pinned else ""
    return (
        f'<li style="margin:0 0 12px">{badge}'
        f'<a href="{_esc(url)}" style="color:#0b5cad;text-decoration:none;font-weight:500">{_esc(title)}</a>'
        f"{src}{gloss_html}</li>"
    )


def _section(title: str, rows: list[str], *, empty_note: Optional[str] = None) -> str:
    if not rows and empty_note is None:
        return ""
    body = (
        f'<ul style="list-style:none;padding:0;margin:0">{"".join(rows)}</ul>'
        if rows
        else f'<p style="{_MUTED}">{_esc(empty_note)}</p>'
    )
    return f'<h2 style="{_H2}">{_esc(title)}</h2>{body}'


def _heartbeat(runs: list[tuple]) -> str:
    chips = []
    for source, _run_at, ok, count, error in runs:
        if ok:
            chips.append(f'<span style="color:#2e7d32">{_esc(source)} ✓ ({count if count is not None else "?"})</span>')
        else:
            chips.append(
                f'<span style="color:#c62828">{_esc(source)} ✗ {_esc((error or "error")[:40])}</span>'
            )
    inner = " · ".join(chips) if chips else "no runs recorded"
    return (
        f'<h2 style="{_H2}">Heartbeat</h2>'
        f'<p style="{_MUTED};line-height:1.8">{inner}</p>'
    )


def build_digest(
    store: Store,
    *,
    now: Optional[datetime] = None,
    capture_url: Optional[str] = None,
    window_hours: int = 24,
) -> Digest:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=window_hours)).replace(microsecond=0).isoformat()

    docket = store.alerts_since(since)
    regulation = store.digest_items("regulation", since)
    research = store.digest_items("research", since)
    discourse = store.digest_items("discourse", since)
    rivals = store.digest_items("rivals", since)
    runs = store.latest_runs()

    # alerts_since rows: (id, source, url, title, changed_at)
    docket_rows = [_item_row(r[2], r[3], source=r[1]) for r in docket]
    # digest_items rows: (id, source, url, title, title_en, published_at, first_seen, pinned)
    reg_rows = [_item_row(r[2], r[3], gloss=r[4], source=r[1]) for r in regulation]
    res_rows = [_item_row(r[2], r[3], gloss=r[4], source=r[1], pinned=bool(r[7])) for r in research]
    dis_rows = [_item_row(r[2], r[3], source=r[1]) for r in discourse]
    riv_rows = [_item_row(r[2], r[3], source=r[1]) for r in rivals]  # titles only

    sections = [
        _section(
            "⚡ Docket",
            docket_rows,
            empty_note="No regulatory docket changes in the last 24h.",
        ),
        _section("Regulation", reg_rows),
        _section("Research", res_rows),
        _section("Discourse", dis_rows),
        _section("Rivals published", riv_rows),
        _heartbeat(runs),
    ]

    footer = ""
    if capture_url:
        footer = (
            f'<p style="margin-top:28px"><a href="{_esc(capture_url)}" '
            f'style="{_MUTED}">→ open capture file</a></p>'
        )

    header = (
        f'<h1 style="font-size:20px;margin:0 0 4px">Parallel Frontier</h1>'
        f'<p style="{_MUTED};margin:0">{now:%A %d %B %Y} · daily digest</p>'
    )
    body = "".join(s for s in sections if s)
    doc = f'<div style="{_WRAP}">{header}{body}{footer}</div>'

    subject = f"Parallel Frontier — {now:%Y-%m-%d}"
    if docket_rows:
        subject = "⚡ " + subject + f" ({len(docket_rows)} docket)"

    return Digest(subject=subject, html=doc, has_docket=bool(docket_rows))


def build_alert(alert_rows: list[tuple], now: Optional[datetime] = None) -> Digest:
    """Minimal immediate-alert email for a docket change (4-hourly poll path).

    `alert_rows` are store.alerts_since rows: (id, source, url, title, changed_at).
    """
    now = now or datetime.now(timezone.utc)
    rows = [_item_row(r[2], r[3], source=r[1]) for r in alert_rows]
    header = (
        f'<h1 style="font-size:20px;margin:0 0 4px">Parallel Frontier — docket change</h1>'
        f'<p style="{_MUTED};margin:0">{now:%A %d %B %Y %H:%M} UTC</p>'
    )
    body = _section("⚡ Docket", rows)
    doc = f'<div style="{_WRAP}">{header}{body}</div>'
    subject = f"⚡ Parallel Frontier docket — {now:%Y-%m-%d %H:%M} ({len(rows)})"
    return Digest(subject=subject, html=doc, has_docket=True)


def send_via_resend(digest: Digest, recipients: list[str], sender: str) -> dict:
    """Send the digest via the Resend HTTP API. Requires RESEND_API_KEY.

    Returns the parsed API response. Raises on transport/HTTP error so the
    caller can record a failed `digest` heartbeat (SPEC: send failure must be
    detectable).
    """
    import requests

    api_key = os.environ["RESEND_API_KEY"]
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": sender,
            "to": recipients,
            "subject": digest.subject,
            "html": digest.html,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":  # `python -m pipeline.digest > preview.html`
    import sys

    from pipeline.store import connect

    with connect() as s:
        d = build_digest(s, capture_url="https://example.invalid/capture")
        sys.stderr.write(f"subject: {d.subject}\n")
        print(d.html)
