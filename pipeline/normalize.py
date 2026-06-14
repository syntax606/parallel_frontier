"""HTML normalization for stable content hashing.

The whole alert-rate goal (SPEC: ALERT < 2/week) rests on *what we hash*. A
naive whole-page diff trips on nav tweaks, footer dates, reordered link lists,
and visitor counters — producing daily false alerts you learn to ignore. So
before hashing we:

  1. drop boilerplate tags (script/style/nav/header/footer/aside/…);
  2. optionally restrict to a per-page content selector (the main lever — give
     each watched page a CSS selector for its real content region in config);
  3. strip known volatile widgets (view/visit counters);
  4. collapse whitespace.

Then hash the result. Two fetches of an unchanged page → identical hash, even if
the surrounding template churns.

`html` may be `bytes` (preferred for gov sites — lxml detects GBK/GB2312/UTF-8)
or `str`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Union

from bs4 import BeautifulSoup

HtmlInput = Union[str, bytes]

# Structural chrome that never carries the item's substance.
_DROP_TAGS = [
    "script", "style", "noscript", "template",
    "nav", "header", "footer", "aside", "form",
    "iframe", "svg", "button",
]

# Volatile fragments that change without the content changing.
_VOLATILE_PATTERNS = [
    re.compile(r"(浏览|点击|阅读|访问)\s*[:：]?\s*\d+"),     # 浏览次数 / 点击：123
    re.compile(r"(views?|hits?)\s*[:：]?\s*\d+", re.I),
    re.compile(r"打印\s*[|｜]?\s*关闭"),                      # print | close widgets
]

_WS = re.compile(r"[ \t 　]+")


def _soup(html: HtmlInput) -> BeautifulSoup:
    # lxml handles encoding detection when given bytes.
    return BeautifulSoup(html, "lxml")


def main_text(html: HtmlInput, selector: Optional[str] = None) -> str:
    """Extract the normalized main-content text of a page.

    If `selector` is given and matches, only that region is used; otherwise
    falls back to <body> (still with chrome tags removed).
    """
    soup = _soup(html)

    for tag in soup(_DROP_TAGS):
        tag.decompose()

    root = soup.select_one(selector) if selector else None
    if root is None:
        root = soup.body or soup

    text = root.get_text(separator="\n")
    return _normalize_text(text)


def _normalize_text(text: str) -> str:
    for pat in _VOLATILE_PATTERNS:
        text = pat.sub("", text)
    lines = (_WS.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def content_hash(html: HtmlInput, selector: Optional[str] = None) -> str:
    """Stable SHA-256 of a page's normalized main content."""
    return hashlib.sha256(main_text(html, selector).encode("utf-8")).hexdigest()
