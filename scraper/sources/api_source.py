"""API source: fetch JSON from an endpoint and map it to records.

When a site has a JSON API behind it, this is the most robust way to scrape --
no HTML parsing, no breakage when the page layout changes. The `api:` block in
the config says how to reach the data and how to walk the JSON:

    type: api
    url: https://api.example.com/quotes
    api:
      method: GET                 # optional, default GET
      params: {symbol: BRENT}     # optional query string
      headers: {Authorization: "Bearer ..."}   # optional
      records_path: "data.quotes" # dotted path to the collection (optional)
      fields:                     # output column -> dotted path within each record
        symbol: "ticker"
        price:  "last.value"

Two collection shapes are handled:

  * `records_path` resolves to a **list** -> one record per item; `fields` paths
    are read from each item.
  * `records_path` resolves to a **dict** (e.g. {"EUR": 0.92, "GBP": 0.79}) ->
    one record per key/value pair. Use the specials `@key` and `@value` in
    `fields` to grab them.

If `records_path` is omitted the whole JSON document is treated as one record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from ..config import SourceConfig
from ..records import ScrapeRecord

# Sentinels usable in a `fields` mapping when iterating a dict collection.
_KEY = "@key"
_VALUE = "@value"


def _dig(data: Any, path: str) -> Any:
    """Walk a dotted `path` (e.g. "data.quotes.0.price") through nested JSON.
        by trying list or disc access on each part 1 by 1.
    """
    if not path:
        return data
    node = data
    for part in path.split("."):
        if node is None:
            return None
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _record_from_item(item: Any, key: Any, config: SourceConfig) -> dict[str, Any]:
    """Build one record's `fields` dict from a collection item.

    `key` is the dict key when iterating a dict (else None). `@key`/`@value` in
    the field map read the key and the item itself; any other value is a dotted
    path into `item`.
    """
    fields = config.api.get("fields")
    if not fields:
        # No explicit map: emit the item as-is if it's a dict, else wrap it.
        if isinstance(item, dict):
            return dict(item)
        return {"value": item}

    out: dict[str, Any] = {}
    for col, path in fields.items():
        if path == _KEY:
            out[col] = key
        elif path == _VALUE:
            out[col] = item
        else:
            out[col] = _dig(item, path)
    return out


class ApiSource:
    """Call a JSON endpoint and map the response into records."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        api = config.api
        method = api.get("method", "GET").upper() #api method if configured, otherwise GET
        resp = requests.request(
            method,
            config.url,
            params=api.get("params"),
            headers=api.get("headers"),
            json=api.get("body"),
            timeout=config.options.get("timeout", 30), # config limit or 30s
        )
        resp.raise_for_status()
        data = resp.json()

        collection = _dig(data, api.get("records_path", ""))
        scraped_at = datetime.now(timezone.utc)

        #convert the collection into a list of kv pairs, if its a list keys are defaulted to None
        #if the collection is a single element its just 1 tuple of None,collection
        if isinstance(collection, list):
            items = [(None, item) for item in collection]
        elif isinstance(collection, dict) and api.get("fields"):
            items = list(collection.items())
        else:
            # Single-document case: one record from the whole (sub)tree.
            items = [(None, collection)]

        return [
            ScrapeRecord(
                source_url=config.url,
                scraped_at=scraped_at,
                fields=_record_from_item(item, key, config),
            )
            for key, item in items
        ]
