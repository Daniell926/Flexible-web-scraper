"""The data contract.

Every stage of the pipeline agrees on this shape. A Source produces a list of
ScrapeRecord; Processing consumes and returns the same; Export writes them out.
No stage knows how any other stage works -- they only share this type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# soft import: try to load pandas, but if it's missing don't crash on import --
# set pd = None and only complain later (in records_to_frame) if it's actually used.
try:  # pandas is part of the core stack, but keep the import soft for type help
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore


@dataclass
class ScrapeRecord:
    """One scraped row.

    `fields` holds the actual scraped values keyed by column name. Keeping the
    payload in a free-form dict (rather than fixed attributes) is what lets one
    pipeline serve many different sites -- the per-site config decides the keys.
    """

    source_url: str
    scraped_at: datetime
    # default_factory=dict gives each record its OWN empty {}. writing `= {}` here
    # would share one dict across every record (classic python mutable-default bug).
    fields: dict[str, Any] = field(default_factory=dict)

    def flat(self) -> dict[str, Any]:
        """Flatten to a single dict (provenance columns + scraped fields)."""
        return {
            "source_url": self.source_url,
            "scraped_at": self.scraped_at,
            **self.fields,  # ** spreads the per-row fields dict INTO this one dict
        }


def records_to_frame(records: list[ScrapeRecord]) -> "pd.DataFrame":
    """Convert the contract to a DataFrame for the Processing/Export stages."""
    if pd is None:  # pragma: no cover
        raise RuntimeError("pandas is required: pip install -r requirements.txt")
    # each record -> one flat dict -> pandas builds one ROW per dict, and the dict
    # keys become the column headers. so a list of records becomes a table.
    return pd.DataFrame([r.flat() for r in records])
