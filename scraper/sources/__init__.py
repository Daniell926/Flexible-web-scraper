"""Extract stage: turn a site config into a list of ScrapeRecord.

`get_source` is the only thing the pipeline needs from here -- it dispatches on
`config.type` to one of the interchangeable Source implementations.
"""

from __future__ import annotations

from .base import Source, get_source

__all__ = ["Source", "get_source"]
