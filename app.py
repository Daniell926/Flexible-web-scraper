"""Streamlit GUI for the web scraper -- a friendly front end over the pipeline.

Run it:  streamlit run app.py   (then it opens in your browser)

Thin shell: lists configs, lets you edit any field (file values as defaults) through
friendly widgets, calls `run_config()`, shows a preview + Excel download. All real
work stays in the scraper package.

The page stays clean -- just Source + Run -- with everything behind one collapsed
"Configuration" dropdown. Inside, the common things are proper widgets (a concurrency
slider, an add-a-row table for processing steps, an add-a-row table for fields); any
remaining keys live in a small "Advanced (raw YAML)" box so nothing is ever lost.
"""

from __future__ import annotations

import base64
import copy
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from scraper.config import SourceConfig, list_configs, load_config
from scraper.pipeline import run_config
from scraper.processing import _STEPS  # the registry of valid processing step names
from scraper.records import records_to_frame

SOURCE_TYPES = ["api", "ose_chain", "hkex_hsi", "taifex", "taifex_live", "html", "browser"]
STEP_NAMES = sorted(_STEPS)  # strip, drop_empty, numeric, sort, log_moneyness, ...
# config keys we render with dedicated widgets -- the rest fall through to "Advanced".
_API_HANDLED = {"code", "months", "fields"}
_OPT_HANDLED = {"concurrency", "processing", "row_selector"}

# short blurb per source type -- explains what it does and why some config blocks
# (fields / processing / selectors) are or aren't set for a given source.
SOURCE_INFO = {
    "api": (
        "**JSON API.** Reads one endpoint and maps the JSON to rows. Configure "
        "`params`, `records_path`, and a **fields** map (column → JSON path); add "
        "**processing** steps to clean or convert values."
    ),
    "ose_chain": (
        "**OSE options chain.** Joins three Osaka-Exchange endpoints across every "
        "contract month. Set `code` + `months`; the **fields** map pulls from each "
        "month/strike, and **processing** converts the epoch dates and adds "
        "log-moneyness."
    ),
    "hkex_hsi": (
        "**HKEX Hang Seng options report.** Parses HKEX's fixed-width daily report "
        "into one row per option (month, strike, C/P, settlement, IV%, volume, open "
        "interest). The source emits a **fixed set of columns**, so there's no "
        "**fields** map to fill in; the data comes out clean, so no **processing** is "
        "needed either. Only `date` (`latest` or a `YYMMDD`) is adjustable. Note IV is "
        "integer-percent and 0 on deep-ITM rows where it isn't meaningful."
    ),
    "taifex": (
        "**TAIFEX TAIEX options.** Pulls TAIFEX's end-of-day TXO options **and** TX "
        "index-futures CSVs, joins each future's settlement onto its option month as "
        "the forward. TAIFEX doesn't publish vols, so the **implied_vol** processing "
        "step inverts Black-76 on each premium to get the vol (in %). Monthly standard "
        "expiries only; one row per expiry × strike × call/put. The source emits a "
        "**fixed set of columns**, so there's no **fields** map — only `date` (`latest` "
        "or a `YYYY/MM/DD`) is adjustable. Use the **Compute implied vols** toggle to "
        "switch between solved vols and raw inputs only (premium, forward, strike, "
        "t_years) — handy for feeding your own vol model or comparing against it."
    ),
    "taifex_live": (
        "**TAIFEX TAIEX options — live (15-min delayed).** Same surface as `taifex` "
        "but intraday: pulls TAIFEX's MIS delayed quote feed and uses the **bid-ask "
        "midpoint** as the premium (joining the live TXF futures mid as the forward), "
        "then inverts Black-76. Monthly standard expiries only; one row per expiry × "
        "strike × call/put with bid, ask, mid, iv, vega. Only `session` (`N` day / `O` "
        "after-hours) is adjustable. Use the **Compute implied vols** toggle to switch "
        "between solved vols and raw quotes (bid, ask, mid, forward, t_years)."
    ),
    "html": (
        "**HTML page.** Fetches a page and extracts with CSS **selectors** "
        "(column → selector); set `row_selector` for the repeating element."
    ),
    "browser": (
        "**JavaScript page.** Like `html`, but renders the page in a real browser "
        "first (Playwright). Needs Playwright installed; won't run on Streamlit Cloud."
    ),
}


def _yaml_dump(value: dict) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).strip() if value else ""


def _yaml_load(text: str, label: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"`{label}` must be a YAML mapping, got {type(parsed).__name__}.")
    return parsed


def _cell(v) -> str:
    """A data_editor cell -> clean string ('' for blanks/NaN)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _arg_to_text(a) -> str:
    """A processing step's argument -> compact text for the table."""
    if a is None:
        return ""
    if isinstance(a, str):
        return a
    return yaml.safe_dump(a, default_flow_style=True, allow_unicode=True).strip()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _auto_download(data: bytes, filename: str) -> None:
    """Trigger an immediate browser download.

    Streamlit has no native "download without a click" -- a download must come from a
    user gesture. So we embed the file as a base64 data: URI in a hidden <a download>
    and click it via JS inside a 0-height component. This runs once, right after a
    scrape finishes (it's inside the Run block, so it doesn't re-fire on later reruns).
    """
    b64 = base64.b64encode(data).decode()
    st.components.v1.html(
        f'<a id="auto-dl" download="{filename}" href="data:{_XLSX_MIME};base64,{b64}"></a>'
        '<script>document.getElementById("auto-dl").click();</script>',
        height=0,
    )


st.set_page_config(page_title="Flexible Web Scraper", page_icon="📊", layout="wide")
st.title("Flexible Web Scraper")
st.caption("Pick a source and run it. Open **Configuration** to tweak any field.")

configs = list_configs()
if not configs:
    st.warning("No configs found in `configs/`. Add a YAML file there first.")
    st.stop()

name = st.selectbox("Source", configs, help="Each is a configs/<name>.yaml file.")
base = load_config(name)  # file values -> defaults for every widget below

with st.expander(f"ℹ️ About this source — `{base.type}`", expanded=False):
    st.markdown(SOURCE_INFO.get(base.type, f"Source type `{base.type}`."))

# collected from the widgets, consumed by the Run handler
in_code = in_months = in_row_selector = None
concurrency = None
compute_iv = None
edited_fields = edited_selectors = edited_proc = None

# processing steps that COMPUTE implied vols (added by the taifex config). The
# "compute vols" toggle drops these so the source can emit raw inputs only --
# useful when you'd rather feed the premiums to your own in-house vol model.
_IV_STEPS = {"implied_vol", "vega", "log_moneyness", "vol_surface_grid"}
# columns those steps produce -- so we can also drop a `sort` that targets one
# of them (e.g. `sort: -vega`) when the vols aren't being computed.
_IV_COLS = {"iv", "vega", "log_moneyness"}


def _drop_iv_rows(rows: list[dict]) -> list[dict]:
    """Remove the vol-computing rows from a processing-steps table (raw mode).

    Drops the implied_vol/vega/log_moneyness steps and any `sort` on a column
    those steps produce, so the on-screen table matches what the toggle does.
    """
    def is_iv(row: dict) -> bool:
        step = row["step"]
        if step in _IV_STEPS:
            return True
        return step == "sort" and str(row["argument"]).lstrip("-").strip() in _IV_COLS

    return [r for r in rows if not is_iv(r)]

with st.expander("Configuration", expanded=False):
    sel_type = st.selectbox(
        "type", SOURCE_TYPES,
        index=SOURCE_TYPES.index(base.type) if base.type in SOURCE_TYPES else 0,
        help="Which extractor to use. Drives which settings below apply.",
    )
    api_used = sel_type in {"api", "ose_chain", "hkex_hsi", "taifex", "taifex_live"}
    sel_used = sel_type in {"html", "browser"}

    st.markdown("**Core**")
    in_name = st.text_input("name", base.name)
    in_url = st.text_input("url", base.url)
    in_output = st.text_input("output", base.output, help="Where the .xlsx is written.")

    # --- source settings ---------------------------------------------------
    if sel_type == "ose_chain":
        st.markdown("**Product**")
        c1, c2 = st.columns(2)
        in_code = c1.text_input("code", str(base.api.get("code", "")),
                                help="e.g. nkopm. Browse codes at /api/optionsInfo.")
        in_months = c2.text_input("first _ months", str(base.api.get("months", "all")),
                                  help="'all' or a number (e.g. 6).")

    if api_used:
        st.markdown("**Fields** — output column → path (add/remove rows)")
        fields_df = pd.DataFrame(
            [{"column": k, "path": v} for k, v in (base.api.get("fields") or {}).items()],
            columns=["column", "path"],
        )
        edited_fields = st.data_editor(
            fields_df, num_rows="dynamic", use_container_width=True, key=f"fields::{name}",
        )

    if sel_used:
        st.markdown("**Selectors** — output column → CSS selector (add/remove rows)")
        sel_df = pd.DataFrame(
            [{"column": k, "selector": v} for k, v in (base.selectors or {}).items()],
            columns=["column", "selector"],
        )
        edited_selectors = st.data_editor(
            sel_df, num_rows="dynamic", use_container_width=True, key=f"sel::{name}",
        )
        in_row_selector = st.text_input(
            "row_selector", str(base.options.get("row_selector", "")),
            help="CSS for the repeating element; blank = whole document is one row.",
        )

    # --- options -----------------------------------------------------------
    st.markdown("**Options**")
    if sel_type == "ose_chain":
        concurrency = st.slider(
            "concurrency — months fetched at once", 1, 12,
            int(base.options.get("concurrency", 1)),
        )
    if sel_type in {"taifex", "taifex_live"}:
        # default the toggle to whatever the loaded config does (on if it has the
        # implied_vol step), so the widget mirrors the file.
        _has_iv = any(
            (next(iter(s)) if isinstance(s, dict) else s) == "implied_vol"
            for s in base.options.get("processing", [])
        )
        compute_iv = st.toggle(
            "Compute implied vols (Black-76)", value=_has_iv,
            help="On: add the solved `iv` (and `log_moneyness`) columns. Off: emit the "
                 "raw inputs only (premium, forward, strike, t_years) for your own vol model.",
        )

    st.markdown("Processing steps — pick a step, give its argument (blank if none):")
    proc_rows = []
    for step in base.options.get("processing", []):
        if isinstance(step, str):
            proc_rows.append({"step": step, "argument": ""})
        else:  # a {name: arg} mapping
            (sname, sarg), = step.items()
            proc_rows.append({"step": sname, "argument": _arg_to_text(sarg)})
    # when the vols toggle is OFF, actually remove the vol-computing rows so the
    # table on screen shows exactly what will run (no hidden run-time stripping).
    if compute_iv is False:
        proc_rows = _drop_iv_rows(proc_rows)
        st.caption("↳ vol steps hidden — turn on **Compute implied vols** to add them back.")
    proc_df = pd.DataFrame(proc_rows, columns=["step", "argument"])
    edited_proc = st.data_editor(
        # the toggle state is part of the key, so flipping it reseeds the table
        # cleanly (no stale edits carried across the on/off switch).
        proc_df, num_rows="dynamic", use_container_width=True,
        key=f"proc::{name}::{compute_iv}",
        column_config={
            "step": st.column_config.SelectboxColumn("step", options=STEP_NAMES, required=True),
            "argument": st.column_config.TextColumn("argument (YAML, e.g. [price, rate] or -rate)"),
        },
    )

    # --- advanced: any keys not covered by the widgets above ---------------
    api_other = {k: v for k, v in base.api.items() if k not in _API_HANDLED}
    opt_other = {k: v for k, v in base.options.items() if k not in _OPT_HANDLED}
    with st.expander("Advanced (raw YAML for any other keys)", expanded=False):
        in_api_other = st.text_area(
            "other api settings", _yaml_dump(api_other), height=120, disabled=not api_used,
            help="e.g. params, records_path, headers, method.",
        )
        in_opt_other = st.text_area(
            "other options", _yaml_dump(opt_other), height=100, help="e.g. timeout.",
        )

# --- run -------------------------------------------------------------------
auto_dl = st.toggle(
    "Auto-download Excel when finished", value=False,
    help="Download the file automatically the moment the scrape completes.",
)
if st.button("▶ Run scrape", type="primary"):
    try:
        # reassemble each block: start from the Advanced leftovers, then layer the
        # widget values on top so the friendly fields win.
        api_block: dict = _yaml_load(in_api_other, "api") if api_used else {}
        if sel_type == "ose_chain":
            api_block["code"] = in_code
            api_block["months"] = int(in_months) if in_months.strip().isdigit() else "all"
        if edited_fields is not None:
            fields = {_cell(r["column"]): _cell(r["path"])
                      for _, r in edited_fields.iterrows() if _cell(r["column"])}
            if fields:
                api_block["fields"] = fields

        selectors_block: dict = {}
        if edited_selectors is not None:
            selectors_block = {_cell(r["column"]): _cell(r["selector"])
                               for _, r in edited_selectors.iterrows() if _cell(r["column"])}

        options_block: dict = _yaml_load(in_opt_other, "options")
        if sel_used and in_row_selector and in_row_selector.strip():
            options_block["row_selector"] = in_row_selector.strip()
        if concurrency is not None:
            options_block["concurrency"] = concurrency
        processing = []
        for _, r in edited_proc.iterrows():
            step = _cell(r["step"])
            if not step:
                continue
            arg = _cell(r["argument"])
            processing.append(step if arg == "" else {step: yaml.safe_load(arg)})
        # no run-time stripping needed: the steps table already reflects the toggle,
        # so whatever is shown is exactly what runs.
        if processing:
            options_block["processing"] = processing

        cfg = SourceConfig(
            name=in_name, type=sel_type, url=in_url,
            selectors=selectors_block, api=api_block,
            output=in_output, options=options_block,
        )
    except (yaml.YAMLError, ValueError) as exc:
        st.error(f"Couldn't read the configuration: {exc}")
        st.stop()

    out_path = Path(cfg.output)
    try:
        with st.spinner("Scraping… (this can take a few seconds)"):
            result = run_config(copy.deepcopy(cfg), output=str(out_path))
        frame = records_to_frame(result.records)
        st.success(f"Scraped {len(result.records):,} rows → `{out_path}`")
        st.dataframe(frame, use_container_width=True, height=420)
        data = out_path.read_bytes()
        st.download_button(
            "⬇ Download Excel", data, file_name=out_path.name, mime=_XLSX_MIME,
        )
        if auto_dl:
            _auto_download(data, out_path.name)  # fire the download immediately
    except Exception as exc:
        st.error(f"Scrape failed: {exc}")
        with st.expander("Details"):
            st.code(traceback.format_exc())
