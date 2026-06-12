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
from datetime import datetime, timezone
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

    # sorted() calls this on each record to get the value to order by.
    def sort_key(rec: ScrapeRecord):
        value = rec.fields.get(key)
        # return a TUPLE: (is-it-None?, value). False(0) sorts before True(1), so real
        # values come before None. comparing the tuple's first item avoids python's
        # "can't compare None to a number" error when some cells are missing.
        return (value is None, value)

    return sorted(records, key=sort_key, reverse=descending)


def _epoch_to_date(records: list[ScrapeRecord], columns: Any) -> list[ScrapeRecord]:
    # convert epoch-MILLISECOND columns to plain ISO dates ("2026-07-09"). many JSON
    # APIs (incl. OSE) give dates as ms-since-1970; this makes them Excel-readable.
    columns = columns if isinstance(columns, list) else [columns]
    for rec in records:
        for col in columns:
            v = rec.fields.get(col)
            if isinstance(v, (int, float)):  # /1000 -> seconds; .date() drops the time
                rec.fields[col] = (
                    datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
                )
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


_STEPS: dict[str, Step] = {
    "strip": _strip,
    "drop_empty": _drop_empty,
    "dedupe": _dedupe,
    "numeric": _numeric,
    "rename": _rename,
    "sort": _sort,
    "epoch_to_date": _epoch_to_date,
    "log_moneyness": _log_moneyness,
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
