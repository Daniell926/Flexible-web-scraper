"""HKEX Hang Seng Index (HSI) options source: parse the daily market report.

HKEX publishes a free end-of-day report per trading day as a FIXED-WIDTH TEXT table
inside a <pre> tag -- not JSON, not an HTML <table> -- so the generic api/html
sources can't read it; it needs this small parser. But one file holds the WHOLE
chain: every expiry month x every strike x call & put, each with the settlement
price (O.Q.P. close), implied vol (IV%, integer), day volume and open interest.

    https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/hsio<yymmdd>.htm

Each data line has three "|"-separated sections (After-Hours | Day | Combined):

  MONTH  STRIKE C/P  ah_open ah_high ah_low ah_close ah_vol | \
        day_open day_high day_low OQP_CLOSE OQP_change IV% day_vol | \
        comb_high comb_low comb_vol OPEN_INTEREST oi_change

We keep the useful columns: month, strike, type, settlement (OQP close), IV, volume,
open interest -- one record per option contract.

Config:
    type: hkex_hsi
    url: https://www.hkex.com.hk/eng/stat/dmstat/dayrpt
    api:
      date: latest          # or a specific report date as YYMMDD, e.g. 260615
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import requests

from ..config import SourceConfig
from ..records import ScrapeRecord

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_MONTH = re.compile(r"^[A-Z]{3}-\d{2}$")              # e.g. JUN-26
_DATE_HDR = re.compile(r"(\d{1,2} [A-Z]{3} \d{4}),")  # e.g. "15 JUN 2026,"


def _num(s: str) -> Any:
    """A report cell -> int/float (dropping +/, signs), or None if not numeric."""
    s = s.replace("+", "").replace(",", "")
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def _pre_text(html: str) -> str:
    """Pull the <pre> body out of the report and strip any inner tags."""
    match = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S | re.I)
    body = match.group(1) if match else html
    return re.sub(r"<[^>]+>", "", body)


def _trade_date(lines: list[str]) -> str | None:
    """The current trading day from the header (the LAST date on the day-pair line)."""
    for line in lines:
        dates = _DATE_HDR.findall(line)
        if dates:  # the header line carries prev-day then current-day; take current
            try:
                return datetime.strptime(dates[-1].title(), "%d %b %Y").date().isoformat()
            except ValueError:
                continue
    return None


def _parse_row(line: str) -> dict | None:
    """One report line -> a record dict, or None if the line isn't an option row."""
    parts = line.split("|")
    if len(parts) != 3:
        return None  # only data rows have the three |-separated sections
    a, b, c = parts[0].split(), parts[1].split(), parts[2].split()
    # section A starts: MONTH STRIKE C/P
    if len(a) < 3 or not _MONTH.match(a[0]) or a[2] not in ("C", "P"):
        return None
    if len(b) < 7:  # day session: open high low OQP_close OQP_change IV% volume
        return None
    return {
        "contract_month": a[0],          # e.g. JUN-26
        "strike": int(a[1]),
        "option_type": a[2],             # C or P
        "settlement": _num(b[3]),        # O.Q.P. close = official settlement price
        "iv": _num(b[5]),                # IV% (integer percent; 0 where not meaningful)
        "volume": _num(b[6]),            # day-session volume
        "open_interest": _num(c[3]) if len(c) >= 4 else None,
    }


class HkexHsiSource:
    """Fetch + parse the HKEX HSI options daily report into per-contract records."""

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        base = config.url.rstrip("/")
        timeout = config.options.get("timeout", 30)

        date = str(config.api.get("date", "latest"))
        if date.lower() == "latest":
            # the index page links the newest hsio<yymmdd>.htm; grab the last one.
            idx = requests.get(f"{base}/dmreport8.htm", headers=_HEADERS, timeout=timeout)
            idx.raise_for_status()
            files = re.findall(r"hsio\d{6}\.htm", idx.text)
            if not files:
                raise RuntimeError("No HSI report link found on dmreport8.htm")
            filename = sorted(set(files))[-1]
        else:
            filename = f"hsio{date}.htm"

        url = f"{base}/{filename}"
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        lines = _pre_text(resp.text).splitlines()

        trade_date = _trade_date(lines)
        scraped_at = datetime.now(timezone.utc)
        fields_map = config.api.get("fields")  # optional: select/rename columns

        records: list[ScrapeRecord] = []
        for line in lines:
            row = _parse_row(line)
            if row is None:
                continue
            row["trade_date"] = trade_date
            fields = (
                {col: row.get(path) for col, path in fields_map.items()}
                if fields_map else row
            )
            records.append(
                ScrapeRecord(source_url=url, scraped_at=scraped_at, fields=dict(fields))
            )
        return records
