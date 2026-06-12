"""The orchestrator: wire the three stages together.

This is the one place that knows the full shape of a run:

    load config -> Source.fetch -> process -> export

Every other module stays ignorant of the others. `run()` is the single function
the CLI (and any caller) needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import SourceConfig, load_config
from .export import export
from .processing import process
from .records import ScrapeRecord
from .sources import get_source


@dataclass
class RunResult:
    """What a finished run produced -- handy for logging or testing."""

    config: SourceConfig
    records: list[ScrapeRecord]
    output: Path


def run(name: str, output: str | None = None) -> RunResult:
    """Run the full extract -> process -> export pipeline for one config."""
    config = load_config(name)  # name -> typed SourceConfig object (reads the yaml)

    source = get_source(config)             # pick html/api/browser by config.type
    # note: `records` is REASSIGNED each line -- each stage takes the list and returns
    # a new list, so the output of one feeds straight into the next.
    records = source.fetch(config)          # Extract: site -> list[ScrapeRecord]
    records = process(records, config)      # Process: clean/sort/etc -> list[ScrapeRecord]
    path = export(records, config, output)  # Export: list -> .xlsx, returns the path

    return RunResult(config=config, records=records, output=path)
