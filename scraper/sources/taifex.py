"""TAIFEX TAIEX-options source: build a TXO chain with a forward, ready for IV.

Taiwan's exchange (TAIFEX) publishes a free end-of-day CSV for TAIEX options
(TXO) -- every expiry x strike x call/put with the daily SETTLEMENT PRICE -- but,
unlike OSE or HKEX, it does NOT publish implied vols. We have to back those out
ourselves from the premium. Black-76 inversion needs a forward, so this source
ALSO pulls the TX index-futures daily CSV and joins each future's settlement onto
the matching option month as the forward. The actual vol solve is the
`implied_vol` processing step; this source just assembles its inputs.

Two Big5-encoded CSV downloads (POST form), one date at a time:

    options:  https://www.taifex.com.tw/cht/3/optDataDown   commodity_id=TXO
    futures:  https://www.taifex.com.tw/cht/3/futDataDown   commodity_id=TX

We keep the DAY session (一般), MONTHLY standard contracts only (expiry code is a
bare YYYYMM -- weeklies carry a W/F suffix and have no matching monthly future),
and emit one record per (expiry, strike, call/put):

    trade_date, expiry (YYYYMM), strike, option_type (C/P), settlement (premium),
    volume, open_interest, expiry_date (YYYY-MM-DD), t_years (to expiry),
    forward (TX futures settlement for that month).

Config:
    type: taifex
    url: https://www.taifex.com.tw/cht/3
    api:
      date: latest        # or a trading day as YYYY/MM/DD (e.g. 2026/06/24)
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from ..config import SourceConfig
from ..records import ScrapeRecord

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_DAY_SESSION = "一般"          # 一般 = regular day session; 盤後 = after-hours
_MONTHLY = re.compile(r"^\d{6}$")  # bare YYYYMM = a monthly standard (no W/F weekly suffix)
_CP = {"買權": "C", "賣權": "P"}    # 買權 = call, 賣權 = put


def _num(s: Any) -> Any:
    """A TAIFEX cell -> int/float, or None (its blanks are '-' or '')."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _fetch_csv(url: str, commodity_id: str, day: str, timeout: int) -> list[list[str]]:
    """POST the data-download form and decode the Big5 CSV into rows of cells."""
    resp = requests.post(
        url,
        headers=_HEADERS,
        data={
            "down_type": "1",
            "commodity_id": commodity_id,
            "queryStartDate": day,
            "queryEndDate": day,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.content.decode("big5", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    return rows[1:] if rows else []  # drop the header line


def _futures_forward(rows: list[list[str]]) -> dict[str, Any]:
    """Map each TX futures month (YYYYMM) -> its day-session settlement price."""
    forward: dict[str, Any] = {}
    for r in rows:
        # cols: 0 date,1 contract,2 month,...,10 settlement,...,17 session
        if len(r) < 18 or r[1].strip() != "TX" or r[17].strip() != _DAY_SESSION:
            continue
        month = r[2].strip()
        settle = _num(r[10])
        if _MONTHLY.match(month) and settle is not None:
            forward[month] = settle
    return forward


def _has_day_session(rows: list[list[str]]) -> bool:
    """True if the options rows contain any regular day-session line."""
    return any(len(r) > 17 and r[17].strip() == _DAY_SESSION for r in rows)


def _t_years(trade_date: str, expiry_date: str) -> Any:
    """Calendar year-fraction from the trade day to the contract expiry (YYYYMMDD)."""
    try:
        t0 = datetime.strptime(trade_date.strip(), "%Y/%m/%d").date()
        t1 = datetime.strptime(expiry_date.strip(), "%Y%m%d").date()
    except (ValueError, AttributeError):
        return None
    days = (t1 - t0).days
    return round(days / 365, 6) if days >= 0 else None


class TaifexSource:
    """Fetch TXO options + TX futures, join the forward, emit per-contract rows."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        base = config.url.rstrip("/")
        timeout = config.options.get("timeout", 30)

        day = str(config.api.get("date", "latest"))
        if day.lower() in ("latest", ""):
            # "latest" must mean the latest SETTLED day, not just today: only the
            # day session (一般) carries a settlement price, and it isn't published
            # until after the close. Today's file often holds only the after-hours
            # session (no settlement -> nothing to invert), and weekends/holidays
            # have no day session at all. So walk back until we find a day with
            # day-session data. `lookback` caps the search (default ~10 days).
            lookback = int(config.options.get("lookback", 10))
            opt_rows: list[list[str]] = []
            for back in range(lookback):
                day = (date.today() - timedelta(days=back)).strftime("%Y/%m/%d")
                opt_rows = _fetch_csv(f"{base}/optDataDown", "TXO", day, timeout)
                if _has_day_session(opt_rows):
                    break
        else:
            opt_rows = _fetch_csv(f"{base}/optDataDown", "TXO", day, timeout)

        # futures for the SAME resolved day, so the forward matches the options.
        fut_rows = _fetch_csv(f"{base}/futDataDown", "TX", day, timeout)
        forward = _futures_forward(fut_rows)

        opt_url = f"{base}/optDataDown?commodity_id=TXO&date={day}"
        scraped_at = datetime.now(timezone.utc)

        records: list[ScrapeRecord] = []
        for r in opt_rows:
            # cols: 0 date,1 contract,2 expiry,3 strike,4 C/P,...,9 vol,10 settle,
            #       11 OI,...,17 session,...,20 expiry_date
            if len(r) < 21 or r[1].strip() != "TXO" or r[17].strip() != _DAY_SESSION:
                continue
            expiry = r[2].strip()
            if not _MONTHLY.match(expiry):  # monthly standards only
                continue
            option_type = _CP.get(r[4].strip())
            if option_type is None:
                continue

            expiry_date = r[20].strip()
            fields = {
                "trade_date": r[0].strip().replace("/", "-"),
                "expiry": expiry,
                "strike": _num(r[3]),
                "option_type": option_type,
                "settlement": _num(r[10]),     # daily settlement = the premium to invert
                "volume": _num(r[9]),
                "open_interest": _num(r[11]),
                "expiry_date": f"{expiry_date[:4]}-{expiry_date[4:6]}-{expiry_date[6:]}"
                               if len(expiry_date) == 8 else None,
                "t_years": _t_years(r[0], expiry_date),
                "forward": forward.get(expiry),  # TX futures settlement for this month
            }
            records.append(
                ScrapeRecord(source_url=opt_url, scraped_at=scraped_at, fields=fields)
            )
        return records
