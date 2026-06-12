"""HTML source: a plain HTTP GET, then CSS-selector extraction.

The cheapest, fastest way to scrape -- works whenever the data is present in the
server-rendered HTML (i.e. you can see it in "View Source", not just in DevTools).
If the page builds its content with JavaScript, use `type: browser` instead.
"""

from __future__ import annotations

import requests

from ..config import SourceConfig
from ..records import ScrapeRecord
from ._selectors import records_from_html

# A real User-Agent avoids the most basic bot blocks; override via options if needed.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


class HtmlSource:
    """Fetch a URL over HTTP and extract records with CSS selectors."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        # merge two dicts: start with our defaults, then let the config's headers
        # override/add. (later ** wins on key clashes -- that's how the override works.)
        headers = {**_DEFAULT_HEADERS, **config.options.get("headers", {})}
        timeout = config.options.get("timeout", 30)
        resp = requests.get(config.url, headers=headers, timeout=timeout)
        resp.raise_for_status()  # turn an HTTP error (404/500) into a clean exception
        # resp.text = the raw HTML string (vs resp.json() for the api source).
        return records_from_html(resp.text, config)
