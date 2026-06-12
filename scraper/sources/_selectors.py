"""Shared HTML -> records logic for the html and browser sources.

Both sources end up with an HTML string; only how they *get* it differs (a plain
HTTP GET vs. a real browser that runs JavaScript). The turning-HTML-into-records
part is identical, so it lives here and both call it.

Selector grammar (kept deliberately small, driven entirely by the YAML config):

    selectors:
      name:  "td.name"          # text content of the first match within a row
      price: "td.price"         # ditto
      link:  "a.more@href"      # "@attr" suffix -> read an attribute instead of text

    options:
      row_selector: "table tbody tr"   # optional. If set, one ScrapeRecord per
                                       # matched element, with the column selectors
                                       # evaluated *inside* each element. If absent,
                                       # the whole document is a single row.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ..config import SourceConfig
from ..records import ScrapeRecord


def _read(node, selector: str):
    """Apply one `selector` (with optional `@attr`) against a BeautifulSoup node."""
    # partition("@") splits "a.more@href" into ("a.more", "@", "href"). if there's no
    # "@" you get ("a.more", "", "") -- so `attr` is "" (falsy) for plain text selectors.
    css, _, attr = selector.partition("@")
    found = node.select_one(css.strip())  # first element matching the CSS, or None
    if found is None:
        return None  # selector matched nothing -> missing data is a value, not a crash
    if attr:
        return found.get(attr.strip())  # read an HTML attribute, e.g. the href/title
    return found.get_text(strip=True)  # else the visible text, trimmed of whitespace


def records_from_html(html: str, config: SourceConfig) -> list[ScrapeRecord]:
    """Parse `html` into records according to `config.selectors`/`row_selector`."""
    # BeautifulSoup parses the raw HTML string into a searchable tree.
    soup = BeautifulSoup(html, "html.parser")
    scraped_at = datetime.now(timezone.utc)  # one timestamp shared by every row here

    # if a row_selector is set, select() returns ALL matching elements (one per row).
    # if not, [soup] = a one-item list of the whole document -> a single record.
    row_selector = config.options.get("row_selector")
    rows = soup.select(row_selector) if row_selector else [soup]

    records: list[ScrapeRecord] = []
    for row in rows:
        # dict comprehension: run every column's selector INSIDE this row element.
        fields = {col: _read(row, sel) for col, sel in config.selectors.items()}
        records.append(
            ScrapeRecord(source_url=config.url, scraped_at=scraped_at, fields=fields)
        )
    return records
