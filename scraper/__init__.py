"""Component-based web scraper.

Three interchangeable stages share one data contract (`ScrapeRecord`):

    Extract  ->  Process  ->  Export
   (sources)   (processing)   (Excel)

Each stage knows only the contract, never how the others work. A new site is a
new YAML file in `configs/`, not new code -- see `config.py`.
"""

from __future__ import annotations

from .config import SourceConfig, list_configs, load_config
from .pipeline import run
from .records import ScrapeRecord, records_to_frame

__all__ = [
    "ScrapeRecord",
    "SourceConfig",
    "list_configs",
    "load_config",
    "records_to_frame",
    "run",
]
