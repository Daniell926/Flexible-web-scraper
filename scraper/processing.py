"""Processing stage: clean up records between extract and export.

Raw scraped data is messy -- numbers arrive as strings ("$72.40"), rows come in
empty, the same row appears twice, columns want renaming before they hit Excel.
This stage fixes that. It takes a list of ScrapeRecord and returns a list of
ScrapeRecord (same contract in, same contract out), so it slots between any
Source and the Export with neither side knowing it ran.

It's *declarative*: the per-site YAML lists the steps to apply, in order, under
`options.processing`. No config -> the data passes through untouched.

    options:
      processing:
        - strip                      # trim whitespace on all string fields
        - drop_empty                 # remove rows whose mapped fields are all blank
        - dedupe                     # remove exact-duplicate rows
        - numeric: [price, rate]     # parse these columns to numbers ("$1,234" -> 1234.0)
        - rename: {rate: fx_rate}    # rename columns
        - sort: price                # sort by a column ("-price" for descending)

Each step is either a bare string (no args) or a single-key mapping (key = step
name, value = its argument). New steps are one entry in `_STEPS` below.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import SourceConfig
from .records import ScrapeRecord

# a "type alias": just a readable name for the function signature every step has.
# Callable[[args...], return] -- here: takes (records, arg), returns records.
Step = Callable[[list[ScrapeRecord], Any], list[ScrapeRecord]]

# a compiled regex. the pattern [^...] means "any char NOT in this set"; the set is
# digits 0-9, e/E (exponent), +/- (sign), and "." -- so .sub("", x) deletes everything
# else. compile() once up here is faster than re-parsing the pattern on every call.
_NUMERIC_NOISE = re.compile(r"[^0-9eE+\-.]")


def _to_number(value: Any) -> Any:
    """Coerce a scraped value to int/float; leave it untouched if it won't parse."""
    if value is None or isinstance(value, (int, float)):
        return value  # already a number (or nothing) -> nothing to do
    cleaned = _NUMERIC_NOISE.sub("", str(value))  # "£1,234.50" -> "1234.50"
    if cleaned in ("", "+", "-", "."):
        return value  # nothing numeric was left; keep the original so junk stays visible
    try:
        number = float(cleaned)
    except ValueError:
        return value  # still un-parseable (e.g. "1.2.3") -> leave it alone
    # 17910.0 -> 17910 (int) but 0.92 stays a float, so whole numbers look clean in Excel.
    return int(number) if number.is_integer() else number


# every step takes the same (records, arg) so they're interchangeable in the table
# below. `_arg` with a leading underscore = "this step ignores its argument" (strip
# takes no options). the rebuild-the-dict pattern keeps only string values stripped.
def _strip(records: list[ScrapeRecord], _arg: Any) -> list[ScrapeRecord]:
    for rec in records:
        rec.fields = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in rec.fields.items()
        }
    return records


def _drop_empty(records: list[ScrapeRecord], _arg: Any) -> list[ScrapeRecord]:
    # any(...) = True if AT LEAST ONE value is real (not None/empty). so a row survives
    # if it has any data; it's dropped only when every field is None or "".
    def has_data(rec: ScrapeRecord) -> bool:
        return any(v not in (None, "") for v in rec.fields.values())

    return [rec for rec in records if has_data(rec)]


def _dedupe(records: list[ScrapeRecord], _arg: Any) -> list[ScrapeRecord]:
    seen: set[tuple] = set()  # a set gives O(1) "have I seen this?" lookups
    out: list[ScrapeRecord] = []
    for rec in records:
        # a dict isn't hashable so it can't go in a set. sorted(items) -> a tuple of
        # (key,value) pairs IS hashable, and sorting makes key order not matter.
        key = tuple(sorted(rec.fields.items()))
        if key not in seen:
            seen.add(key)
            out.append(rec)  # first time we've seen this exact row -> keep it
    return out


def _numeric(records: list[ScrapeRecord], columns: Any) -> list[ScrapeRecord]:
    # accept both `numeric: price` (one) and `numeric: [price, rate]` (list); wrap a
    # lone value in a list so the loop below is the same either way.
    columns = columns if isinstance(columns, list) else [columns]
    for rec in records:
        for col in columns:
            if col in rec.fields:  # skip columns a given row happens not to have
                rec.fields[col] = _to_number(rec.fields[col])
    return records


def _rename(records: list[ScrapeRecord], mapping: Any) -> list[ScrapeRecord]:
    for rec in records:
        # mapping.get(k, k): new name if k is in the rename map, else keep k unchanged.
        rec.fields = {mapping.get(k, k): v for k, v in rec.fields.items()}
    return records


def _sort(records: list[ScrapeRecord], column: Any) -> list[ScrapeRecord]:
    # leading "-" means descending, e.g. "-rate". strip it off to get the real column.
    descending = isinstance(column, str) and column.startswith("-")
    key = column[1:] if descending else column

    # split out rows missing the column so they ALWAYS land last, in either
    # direction. (a single sorted(reverse=True) would flip a None-last tuple to
    # None-FIRST, floating the blanks to the top -- wrong for e.g. `sort: -vega`.)
    present = [rec for rec in records if rec.fields.get(key) is not None]
    missing = [rec for rec in records if rec.fields.get(key) is None]
    present.sort(key=lambda rec: rec.fields.get(key), reverse=descending)
    return present + missing


def _epoch_to_date(records: list[ScrapeRecord], arg: Any) -> list[ScrapeRecord]:
    # convert epoch-MILLISECOND columns to plain ISO dates ("2026-07-10").
    #
    # CRITICAL: an epoch is an absolute instant; turning it into a CALENDAR DATE
    # depends on the timezone. Market timestamps encode midnight in the EXCHANGE's
    # zone -- read them in the wrong zone and the date lands a day off. So pass the
    # exchange's UTC offset. OSE encodes midnight JST, so use tz: 9 (Japan, no DST).
    #
    # arg forms:
    #   [col, ...]                       -> convert those columns in UTC (offset 0)
    #   {columns: [col, ...], tz: 9}     -> convert in UTC+9 (the correct OSE form)
    if isinstance(arg, dict):
        columns, offset = arg.get("columns", []), arg.get("tz", 0)
    else:
        columns, offset = arg, 0
    columns = columns if isinstance(columns, list) else [columns]
    tz = timezone(timedelta(hours=offset))
    for rec in records:
        for col in columns:
            v = rec.fields.get(col)
            if isinstance(v, (int, float)):  # /1000 -> seconds; .date() drops the time
                rec.fields[col] = datetime.fromtimestamp(v / 1000, tz=tz).date().isoformat()
    return records


def _log_moneyness(records: list[ScrapeRecord], arg: Any) -> list[ScrapeRecord]:
    # add a computed column ln(strike / futures) -- the natural x-axis for a vol smile.
    # arg names the input columns and the output, e.g.
    #   {strike: strike, futures: futures_price, as: log_moneyness}
    arg = arg or {}
    s_col = arg.get("strike", "strike")
    f_col = arg.get("futures", "futures_price")
    out_col = arg.get("as", "log_moneyness")
    for rec in records:
        s, f = rec.fields.get(s_col), rec.fields.get(f_col)
        try:
            rec.fields[out_col] = round(math.log(s / f), 6) if s and f else None
        except (TypeError, ValueError, ZeroDivisionError):
            rec.fields[out_col] = None  # missing/zero/garbage -> blank, not a crash
    return records


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function (no SciPy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _black76_price(is_call: bool, F: float, K: float, T: float, r: float, sigma: float) -> float:
    """European option on a forward (Black-76): the model premium for a given vol."""
    disc = math.exp(-r * T)
    vol = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol) / vol
    d2 = d1 - vol
    if is_call:
        return disc * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return disc * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def _implied_vol_one(is_call: bool, price: float, F: float, K: float, T: float, r: float) -> Any:
    """Invert Black-76 for sigma given a premium. None if it can't be solved.

    Newton-Raphson on vega, with a bisection fallback when Newton wanders out of
    bounds. Returns None below the no-arbitrage floor (price <= intrinsic) or on
    any bad input -- a missing vol, not a crash or a garbage number.
    """
    if not (price and F and K and T and price > 0 and F > 0 and K > 0 and T > 0):
        return None
    disc = math.exp(-r * T)
    intrinsic = disc * (F - K) if is_call else disc * (K - F)
    # below intrinsic there's no real vol; above the forward's discounted value a
    # call can't trade either. clamp out both to avoid a non-converging solve.
    if price <= max(intrinsic, 0.0) or price >= disc * (F if is_call else K):
        return None

    sqrtT = math.sqrt(T)
    sigma = 0.2  # a sensible starting guess (~20% vol)
    lo, hi = 1e-4, 5.0  # bracket for the fallback: 0.01% .. 500%
    for _ in range(100):
        diff = _black76_price(is_call, F, K, T, r, sigma) - price
        if abs(diff) < 1e-6:
            return round(sigma * 100, 4)  # report as a percent, e.g. 18.53
        vol = sigma * sqrtT
        d1 = (math.log(F / K) + 0.5 * vol * vol) / vol
        vega = disc * F * sqrtT * _norm_pdf(d1)
        # keep the bracket tight around the root for the fallback
        if diff > 0:
            hi = sigma
        else:
            lo = sigma
        if vega < 1e-8:  # vega too small for a reliable Newton step -> bisect
            sigma = 0.5 * (lo + hi)
            continue
        step = diff / vega
        sigma -= step
        if not (lo <= sigma <= hi):  # Newton left the bracket -> bisect instead
            sigma = 0.5 * (lo + hi)
    return None  # didn't converge


def _implied_vol(records: list[ScrapeRecord], arg: Any) -> list[ScrapeRecord]:
    """Add an implied-vol column by inverting Black-76 on each option's premium.

    Markets like TAIFEX publish the premium but not the vol; this backs the vol
    out. Each row needs a forward, strike, premium, option type, and time to
    expiry (a year-fraction). The risk-free rate is a single assumption.

        implied_vol:
          price: settlement      # the premium column
          forward: forward       # forward/underlier column
          strike: strike
          type: option_type      # call/put flag (C/P, call/put, 1/-1...)
          t: t_years             # time to expiry, in years
          rate: 0.015            # risk-free rate (default 1.5%)
          as: iv                 # output column (vol in %)
    """
    arg = arg or {}
    p_col = arg.get("price", "settlement")
    f_col = arg.get("forward", "forward")
    k_col = arg.get("strike", "strike")
    type_col = arg.get("type", "option_type")
    t_col = arg.get("t", "t_years")
    rate = float(arg.get("rate", 0.015))
    out_col = arg.get("as", "iv")

    for rec in records:
        flag = str(rec.fields.get(type_col, "")).strip().lower()
        is_call = flag in ("c", "call", "1", "買權")
        try:
            iv = _implied_vol_one(
                is_call,
                float(rec.fields.get(p_col)),
                float(rec.fields.get(f_col)),
                float(rec.fields.get(k_col)),
                float(rec.fields.get(t_col)),
                rate,
            )
        except (TypeError, ValueError):
            iv = None  # a missing/non-numeric input -> blank vol, not a crash
        rec.fields[out_col] = iv
    return records


def _vega(records: list[ScrapeRecord], arg: Any) -> list[ScrapeRecord]:
    """Add a Black-76 vega column: the premium's sensitivity to vol.

    Vega peaks at-the-money and decays toward zero in the wings, so it doubles as
    a DATA-QUALITY score: high vega = the premium carries a strong, well-
    conditioned vol signal (a trustworthy implied vol); near-zero vega = vol can't
    be read reliably off this strike. Sort by it descending to put the most
    reliable rows first. Vega is identical for a call and a put, so no type is
    needed. It's evaluated at the `vol` column (a percent vol, e.g. the solved iv).

        vega:
          forward: forward
          strike: strike
          t: t_years
          vol: iv            # vol to evaluate at, in % (default the solved iv)
          rate: 0.015
          as: vega           # output: premium points per 1 vol-point (1%) move
    """
    arg = arg or {}
    f_col = arg.get("forward", "forward")
    k_col = arg.get("strike", "strike")
    t_col = arg.get("t", "t_years")
    vol_col = arg.get("vol", "iv")
    rate = float(arg.get("rate", 0.015))
    out_col = arg.get("as", "vega")

    for rec in records:
        try:
            F = float(rec.fields.get(f_col))
            K = float(rec.fields.get(k_col))
            T = float(rec.fields.get(t_col))
            sigma = float(rec.fields.get(vol_col)) / 100.0  # iv is a percent
        except (TypeError, ValueError):
            rec.fields[out_col] = None  # missing iv/inputs -> blank, not a crash
            continue
        if not (F > 0 and K > 0 and T > 0 and sigma > 0):
            rec.fields[out_col] = None
            continue
        vol = sigma * math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * vol * vol) / vol
        raw = math.exp(-rate * T) * F * math.sqrt(T) * _norm_pdf(d1)
        # raw vega is d(premium)/d(sigma) for sigma in decimals; *0.01 rescales it
        # to "premium points per 1 vol-point (1%) move", the readable convention.
        rec.fields[out_col] = round(raw * 0.01, 4)
    return records


def _interp(points: list[tuple[float, float]], x: float) -> Any:
    """Linear-interpolate y at x over sorted (x, y) points; None if out of range."""
    if len(points) < 2 or x < points[0][0] or x > points[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return round(y0, 4)
            return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 4)
    return None


def _vol_surface_grid(records: list[ScrapeRecord], arg: Any) -> list[ScrapeRecord]:
    """Pivot a long option chain into a vol SURFACE GRID: expiry x moneyness.

    Reshapes one-row-per-contract into one-row-per-expiry, with a column for each
    target moneyness level (strike / forward). Each cell is the implied vol at
    that moneyness, linearly interpolated from the smile -- because real strikes
    don't land exactly on 0.95 x forward etc.

    The smile per expiry is built from the OTM side (puts where strike <= forward,
    calls where strike > forward), which is the liquid/reliable wing; the ATM
    columns naturally blend the two via interpolation. Rows come out ordered by
    time to expiry (nearest first / top).

        vol_surface_grid:
          moneyness: [0.90, 0.95, 1.00, 1.05, 1.10]   # strike/forward columns
          forward: forward
          strike: strike
          iv: iv
          type: option_type     # to pick the OTM side (C/P)
          expiry: expiry        # group key (one row per distinct value)
          t: t_years            # passthrough + sort key
          expiry_date: expiry_date
          snap: false           # true = use the nearest REAL strike's IV (no interpolation)
          show_strikes: false   # true = also emit the actual strike used per column

    With `snap: true` each cell is the implied vol of an ACTUAL listed contract --
    the strike nearest to moneyness x forward -- instead of a value interpolated
    between two strikes. `show_strikes: true` adds a `<level>_K` column naming the
    real strike each cell came from, so the mapping is auditable.
    """
    arg = arg or {}
    levels = [float(x) for x in (arg.get("moneyness") or [0.90, 0.95, 1.00, 1.05, 1.10])]
    f_col = arg.get("forward", "forward")
    k_col = arg.get("strike", "strike")
    iv_col = arg.get("iv", "iv")
    type_col = arg.get("type", "option_type")
    exp_col = arg.get("expiry", "expiry")
    t_col = arg.get("t", "t_years")
    expd_col = arg.get("expiry_date", "expiry_date")
    snap = bool(arg.get("snap", False))
    show_strikes = bool(arg.get("show_strikes", False))

    # group rows by expiry, preserving first-seen order
    groups: dict[Any, list[ScrapeRecord]] = {}
    for rec in records:
        groups.setdefault(rec.fields.get(exp_col), []).append(rec)

    out: list[ScrapeRecord] = []
    for expiry, recs in groups.items():
        forward = t = expd = None
        points: list[tuple[float, float, Any]] = []  # (moneyness, iv, strike)
        for rec in recs:
            F, K = rec.fields.get(f_col), rec.fields.get(k_col)
            iv, typ = rec.fields.get(iv_col), str(rec.fields.get(type_col, "")).strip().upper()
            if F:
                forward = F
            if rec.fields.get(t_col) is not None:
                t = rec.fields.get(t_col)
            if rec.fields.get(expd_col) is not None:
                expd = rec.fields.get(expd_col)
            if not (F and K and iv):
                continue
            m = K / F
            is_call = typ.startswith("C") or typ in ("1", "買權")
            # keep the OTM side: calls above the forward, puts below.
            if (m > 1 and is_call) or (m <= 1 and not is_call):
                points.append((m, iv, K))
        points.sort()

        row: dict[str, Any] = {exp_col: expiry}
        if expd is not None:
            row[expd_col] = expd
        if t is not None:
            row[t_col] = t
        if forward is not None:
            row[f_col] = round(forward, 2)
        for lvl in levels:
            # "{:g}" -> 0.9 / 0.95 / 1 / 1.05 / 1.1 (matches how traders write it)
            label = f"{lvl:g}"
            if snap:
                # nearest ACTUAL listed strike to moneyness x forward -> its real IV
                nearest = min(points, key=lambda p: abs(p[0] - lvl)) if points else None
                row[label] = nearest[1] if nearest else None
                if show_strikes:
                    row[f"{label}_K"] = nearest[2] if nearest else None
            else:
                row[label] = _interp([(m, iv) for m, iv, _ in points], lvl)
        out.append(ScrapeRecord(
            source_url=recs[0].source_url, scraped_at=recs[0].scraped_at, fields=row,
        ))

    # term structure top-to-bottom: nearest expiry first (missing t sorts last)
    out.sort(key=lambda r: (r.fields.get(t_col) is None, r.fields.get(t_col)))
    return out


_STEPS: dict[str, Step] = {
    "strip": _strip,
    "drop_empty": _drop_empty,
    "dedupe": _dedupe,
    "numeric": _numeric,
    "rename": _rename,
    "sort": _sort,
    "epoch_to_date": _epoch_to_date,
    "log_moneyness": _log_moneyness,
    "implied_vol": _implied_vol,
    "vega": _vega,
    "vol_surface_grid": _vol_surface_grid,
}


def _parse_step(step: Any) -> tuple[str, Any]:
    """Normalise a YAML step into (name, argument)."""
    if isinstance(step, str):
        return step, None  # bare step like "strip" -> no argument
    if isinstance(step, dict) and len(step) == 1:
        # `(name, arg), =` unpacks the single (key, value) pair out of a 1-item dict.
        # the trailing comma is what does the unpacking -- it says "one item only".
        (name, arg), = step.items()
        return name, arg
    raise ValueError(
        f"Bad processing step {step!r}: use a name, or a single-key mapping "
        "{name: argument}."
    )


def process(records: list[ScrapeRecord], config: SourceConfig) -> list[ScrapeRecord]:
    """Run the config's processing steps over the records, in order."""
    steps = config.options.get("processing", [])  # [] = no processing configured
    for step in steps:
        name, arg = _parse_step(step)
        func = _STEPS.get(name)  # look the step name up in the dispatch table
        if func is None:
            raise ValueError(
                f"Unknown processing step '{name}'. Known: {sorted(_STEPS)}."
            )
        # reassign: each step takes the list and returns the (possibly new) list, so
        # they chain -- the output of one becomes the input of the next.
        records = func(records, arg)
    return records
