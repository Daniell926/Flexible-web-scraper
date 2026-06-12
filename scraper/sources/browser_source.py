"""Browser source: render the page with a real browser, then extract.

For sites that build their content with JavaScript (prices that tick in via a
websocket, tables loaded after the initial HTML, etc.) a plain GET returns an
empty shell. This source drives a headless Chromium via Playwright so the page
runs exactly as it would for a human, then hands the rendered HTML to the same
selector logic the html source uses.

Playwright is an *optional* dependency -- imported lazily here (and lazily in
`base.get_source`) so you only need it installed when a config asks for it:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

from ..config import SourceConfig
from ..records import ScrapeRecord
from ._selectors import records_from_html


class BrowserSource:
    """Render with headless Chromium, then extract records with CSS selectors."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "type: browser needs Playwright. Install it with:\n"
                "    pip install playwright && playwright install chromium"
            ) from exc

        opts = config.options
        timeout = opts.get("timeout", 30) * 1000  # Playwright wants milliseconds
        # wait_until="networkidle" = wait until the page stops making network requests
        # (a decent signal that JS-loaded content has finished arriving).
        wait_until = opts.get("wait_until", "networkidle")
        wait_for = opts.get("wait_for")  # optional CSS selector to wait on

        # `with` = context manager: guarantees playwright shuts down even on error.
        with sync_playwright() as p:
            browser = p.chromium.launch()  # launch headless Chromium (no visible window)
            try:
                page = browser.new_page()
                page.goto(config.url, wait_until=wait_until, timeout=timeout)
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout)
                html = page.content()  # the RENDERED html, after JS has run
            finally:
                browser.close()  # finally => always runs, so the browser never leaks

        # hand off to the exact same parser the html source uses -- only the way we
        # GOT the html differs (real browser vs plain GET).
        return records_from_html(html, config)
