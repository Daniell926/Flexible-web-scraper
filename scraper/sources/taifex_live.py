"""TAIFEX live (15-min delayed) TAIEX-options source via the MIS quote API.

Same TAIEX (TXO) vol surface as the end-of-day `taifex` source, but INTRADAY:
it reads TAIFEX's Market Information System (MIS) delayed-quote feed and uses the
BID-ASK MIDPOINT as the premium, instead of the daily settlement price. Monthly
standard contracts only. The output columns mirror the `taifex` source (plus bid/
ask/quote_time), so the same implied_vol / vega / log_moneyness processing applies.

MIS endpoint (JSON POST), one call each for options and futures:

    https://mis.taifex.com.tw/futures/api/getQuoteList
      options:  {"SymbolType":"O","KindID":"1","CID":"TXO","RowSize":"全部"}
      futures:  {"SymbolType":"F","KindID":"1","CID":"TXF","RowSize":"全部"}
    -> RtData.QuoteList: [{SymbolID, CBidPrice1, CAskPrice1, CLastPrice, ...}]

Monthly OPTION symbols decode as  TXO<strike><M><Y>-<sess>:
    strike  digits after TXO                 e.g. 45000
    M       month letter: A-L = CALL Jan-Dec, M-X = PUT Jan-Dec  (G=call Jul, S=put Jul)
    Y       last digit of the year           6 -> 2026
    sess    N = regular day session, O = after-hours
Monthly FUTURES symbols decode as  TXF<M><Y>-<sess>   (M: A-L = Jan-Dec; sess F=day, M=night).
Monthly TAIEX options expire on the 3rd WEDNESDAY of the contract month.

Config:
    type: taifex_live
    url: https://mis.taifex.com.tw/futures/api
    api:
      session: N        # N = regular day session (default), O = after-hours
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timezone
from typing import Any

import requests

from ..config import SourceConfig
from ..records import ScrapeRecord
from .taifex import _num, _t_years  # reuse the cell parser + year-fraction helper

_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://mis.taifex.com.tw"}
# TXO<strike><monthletter><yeardigit>-<session>   (monthly options only)
_OPT = re.compile(r"^TXO(\d+)([A-X])(\d)-([NO])$")
# TXF<monthletter><yeardigit>-<session>           (monthly futures)
_FUT = re.compile(r"^TXF([A-L])(\d)-([FM])$")


def _opt_month(letter: str) -> tuple[int, bool]:
    """Option month letter -> (month 1-12, is_call). A-L = calls, M-X = puts."""
    i = ord(letter) - ord("A")
    return (i + 1, True) if i < 12 else (i - 11, False)


def _fut_month(letter: str) -> int:
    """Futures month letter -> month 1-12 (A-L = Jan-Dec)."""
    return ord(letter) - ord("A") + 1


def _year(digit: int, ref_year: int) -> int:
    """Single year digit -> full year in the current decade, rolled forward if past."""
    year = (ref_year // 10) * 10 + digit
    return year + 10 if year < ref_year else year


def _third_wednesday(year: int, month: int) -> date:
    """The 3rd Wednesday of a month = TAIEX monthly option expiry."""
    weds = [d for d in calendar.Calendar().itermonthdates(year, month)
            if d.month == month and d.weekday() == 2]  # Monday=0 .. Wednesday=2
    return weds[2]


def _mid(q: dict) -> Any:
    """Bid-ask midpoint from a quote, or None if it isn't a two-sided market."""
    bid, ask = _num(q.get("CBidPrice1")), _num(q.get("CAskPrice1"))
    if bid and ask and bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    return None


class TaifexLiveSource:
    """Fetch MIS delayed option+future quotes; mid as premium; per-contract rows."""

    def _quotes(self, base: str, symbol_type: str, cid: str, timeout: int) -> list[dict]:
        body = {
            "SymbolType": symbol_type, "KindID": "1", "CID": cid,
            "ExpireMonth": "", "RowSize": "全部", "PageNo": "",
            "SortColumn": "", "AscDesc": "A",
        }
        resp = requests.post(f"{base}/getQuoteList", headers=_HEADERS, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("RtData", {}).get("QuoteList", []) or []

    def fetch(self, config: SourceConfig) -> list[ScrapeRecord]:
        base = config.url.rstrip("/")
        timeout = config.options.get("timeout", 30)
        session = str(config.api.get("session", "N")).upper()  # N=day, O=after-hours

        opt = self._quotes(base, "O", "TXO", timeout)
        fut = self._quotes(base, "F", "TXF", timeout)

        # "as of" date: the feed's own quote date (Taiwan session), else today.
        as_of = next((q["CDate"].strip() for q in opt
                      if len((q.get("CDate") or "").strip()) == 8), None) \
            or date.today().strftime("%Y%m%d")
        ref_year = int(as_of[:4])
        as_of_iso = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}"
        as_of_slash = f"{as_of[:4]}/{as_of[4:6]}/{as_of[6:]}"

        # forward per (month, year) from the day-session futures midpoint (last as fallback).
        forward: dict[tuple[int, int], Any] = {}
        for q in fut:
            m = _FUT.match(q.get("SymbolID", ""))
            if not m or m.group(3) != "F":  # day session only
                continue
            price = _mid(q)
            if price is None:
                price = _num(q.get("CLastPrice"))  # futures: fall back to last trade
            if price is None:
                continue
            month, year = _fut_month(m.group(1)), _year(int(m.group(2)), ref_year)
            forward[(month, year)] = price

        scraped_at = datetime.now(timezone.utc)
        url = f"{base}/getQuoteList?CID=TXO"
        records: list[ScrapeRecord] = []
        for q in opt:
            m = _OPT.match(q.get("SymbolID", ""))
            if not m or m.group(4) != session:
                continue
            strike, letter, ydig, _sess = m.groups()
            month, is_call = _opt_month(letter)
            year = _year(int(ydig), ref_year)
            expiry_date = _third_wednesday(year, month)
            ctime = (q.get("CTime") or "").strip()
            fields = {
                "trade_date": as_of_iso,
                "quote_time": f"{ctime[:2]}:{ctime[2:4]}:{ctime[4:6]}" if len(ctime) == 6 else None,
                "expiry": f"{year}{month:02d}",
                "strike": int(strike),
                "option_type": "C" if is_call else "P",
                "bid": _num(q.get("CBidPrice1")),
                "ask": _num(q.get("CAskPrice1")),
                "mid": _mid(q),               # bid-ask midpoint = the premium we invert
                "volume": _num(q.get("CTotalVolume")),
                "expiry_date": expiry_date.isoformat(),
                "t_years": _t_years(as_of_slash, expiry_date.strftime("%Y%m%d")),
                "forward": forward.get((month, year)),
            }
            records.append(ScrapeRecord(source_url=url, scraped_at=scraped_at, fields=fields))
        return records
