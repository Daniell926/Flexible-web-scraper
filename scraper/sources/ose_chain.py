"""OSE OPC options-chain source: build a full per-month option chain.

The plain `api` source does one endpoint -> records. An option chain needs more:
for ONE product (e.g. Nikkei 225 options, code "nkopm") it must loop every contract
month and, per month, JOIN three endpoints into each strike row:

    /tradingInfo?code=C            -> the list of months (+ expiry dates)   [once]
    /underlierPriceInfo?code=C&gengetsu=M -> that month's futures price     [per month]
    /priceInfo?code=C&gengetsu=M   -> data.values: per-strike call/put IV   [per month]

So one record = one (month, strike): the strike's vols + that month's futures price
+ the month's dates. That cross-endpoint join is why this is its own source.

It stays config-driven via `api.fields`, whose values are NAMESPACED dotted paths
into the four things in scope for each row:

    month.<x>      a row from /tradingInfo      (gengetsu, deliveryMonthEn, lastTradingDay, maturityDay)
    underlier.<x>  /underlierPriceInfo data     (refPrice, date)
    smile.<x>      /priceInfo data              (date, values)
    row.<x>        one strike row               (strikePrice, call, put)

    api:
      code: nkopm
      months: all            # or an int to cap how many months
      fields:
        contract_month: month.deliveryMonthEn
        futures_price:  underlier.refPrice
        strike:         row.strikePrice
        call_vol:       row.call
        put_vol:        row.put
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests

from ..config import SourceConfig
from ..records import ScrapeRecord
from .api_source import _dig  # reuse the dotted-path walker

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _resolve(namespaces: dict[str, Any], path: str) -> Any:
    """Resolve a namespaced path like "month.deliveryMonthEn".

    First token picks the namespace (month/underlier/smile/row); the rest is a
    normal dotted path dug into that object.
    """
    head, _, tail = path.partition(".")
    obj = namespaces.get(head)
    return _dig(obj, tail) if tail else obj


class OseChainSource:
    """Loop a product's contract months and join the chain into (month, strike) rows."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        api = config.api
        base = config.url.rstrip("/")          # e.g. https://apiopc.qri.jp/api
        code = api["code"]
        fields = api.get("fields", {})
        timeout = config.options.get("timeout", 30)
        # how many months to fetch at once. the work is network-bound (~95% idle
        # waiting on round-trips), so parallelism is a big win; cap it to stay polite
        # to the server. default 1; set options.concurrency: 1 to go fully sequential.
        concurrency = config.options.get("concurrency", 1)

        # every call carries code=<code>; this helper merges that in and unwraps "data".
        def get(path: str, **params: Any) -> Any:
            resp = requests.get(
                base + path, headers=_HEADERS,
                params={"code": code, **params}, timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json().get("data")

        months = get("/tradingInfo") or []      # [once] the month list (needed first)
        limit = api.get("months", "all")
        if isinstance(limit, int):
            months = months[:limit]

        # fetch one month's two endpoints. independent per month, so safe to run many
        # of these concurrently -- the slow part is just waiting on the network.
        def fetch_month(month: dict) -> tuple[dict, Any, Any]:
            gengetsu = month.get("gengetsu")
            underlier = get("/underlierPriceInfo", gengetsu=gengetsu) or {}
            smile = get("/priceInfo", gengetsu=gengetsu) or {}
            return month, underlier, smile

        # threads overlap because network I/O releases the GIL. pool.map preserves
        # input order, so output rows stay grouped month-by-month (deterministic).
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            fetched = list(pool.map(fetch_month, months))

        scraped_at = datetime.now(timezone.utc)  # one timestamp for the whole run
        records: list[ScrapeRecord] = []

        for month, underlier, smile in fetched:
            gengetsu = month.get("gengetsu")
            strike_rows = (smile or {}).get("values") or []
            row_url = f"{base}/priceInfo?code={code}&gengetsu={gengetsu}"
            for row in strike_rows:
                # the four objects this strike row can pull from, by namespace
                ns = {"month": month, "underlier": underlier, "smile": smile, "row": row}
                built = {col: _resolve(ns, path) for col, path in fields.items()}
                records.append(
                    ScrapeRecord(source_url=row_url, scraped_at=scraped_at, fields=built)
                )

        return records
