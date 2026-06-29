"""The Source interface and its registry.

A Source turns one site (per its config) into a list of ScrapeRecord. Three
implementations are interchangeable behind this interface -- html, browser, api
-- so the rest of the pipeline never knows or cares which one ran.

`get_source` dispatches on `config.type`. The browser source is imported lazily
so that Playwright stays an optional dependency: you only need it installed if a
config actually asks for `type: browser`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config import SourceConfig
from ..records import ScrapeRecord


# Protocol = a "shape" contract: any class with a matching fetch() method counts as
# a Source, WITHOUT inheriting from this. runtime_checkable lets isinstance() work too.
# The `...` (literally Ellipsis) is just an empty body -- protocols declare, never implement.
@runtime_checkable
class Source(Protocol):
    """Anything that can turn a site config into records."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        ...


def get_source(config: SourceConfig) -> Source:
    """Pick the Source implementation named by `config.type`."""
    kind = config.type.lower()  # .lower() so "API"/"Api"/"api" all match
    if kind == "html":
        # imports live INSIDE each branch (not at top of file) so a heavy/optional
        # dependency only loads when a config actually asks for that source type.
        from .html_source import HtmlSource

        return HtmlSource()
    if kind == "api":
        from .api_source import ApiSource

        return ApiSource()
    if kind == "browser":
        # Lazy import keeps Playwright optional.
        from .browser_source import BrowserSource

        return BrowserSource()
    if kind == "ose_chain":
        # multi-endpoint options-chain join for the OSE OPC API family.
        from .ose_chain import OseChainSource

        return OseChainSource()
    if kind == "hkex_hsi":
        # fixed-width-text parser for the HKEX HSI options daily report.
        from .hkex_hsi import HkexHsiSource

        return HkexHsiSource()
    if kind == "taifex":
        # TAIFEX TXO options + TX futures join (forward for IV inversion).
        from .taifex import TaifexSource

        return TaifexSource()
    if kind == "taifex_live":
        # TAIFEX MIS live (15-min delayed) quotes; bid-ask midpoint as the premium.
        from .taifex_live import TaifexLiveSource

        return TaifexLiveSource()
    raise ValueError(
        f"Unknown source type '{config.type}' "
        "(expected: html, browser, api, ose_chain, hkex_hsi, taifex, taifex_live)."
    )
