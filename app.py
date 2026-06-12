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

SOURCE_TYPES = ["api", "ose_chain", "html", "browser"]
STEP_NAMES = sorted(_STEPS)  # strip, drop_empty, numeric, sort, log_moneyness, ...
# config keys we render with dedicated widgets -- the rest fall through to "Advanced".
_API_HANDLED = {"code", "months", "fields"}
_OPT_HANDLED = {"concurrency", "processing", "row_selector"}


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


st.set_page_config(page_title="Flexible Web Scraper", page_icon="📊", layout="wide")
st.title("Flexible Web Scraper")
st.caption("Pick a source and run it. Open **Configuration** to tweak any field.")

configs = list_configs()
if not configs:
    st.warning("No configs found in `configs/`. Add a YAML file there first.")
    st.stop()

name = st.selectbox("Source", configs, help="Each is a configs/<name>.yaml file.")
base = load_config(name)  # file values -> defaults for every widget below

# collected from the widgets, consumed by the Run handler
in_code = in_months = in_row_selector = None
concurrency = None
edited_fields = edited_selectors = edited_proc = None

with st.expander("Configuration", expanded=False):
    sel_type = st.selectbox(
        "type", SOURCE_TYPES,
        index=SOURCE_TYPES.index(base.type) if base.type in SOURCE_TYPES else 0,
        help="Which extractor to use. Drives which settings below apply.",
    )
    api_used = sel_type in {"api", "ose_chain"}
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

    st.markdown("Processing steps — pick a step, give its argument (blank if none):")
    proc_rows = []
    for step in base.options.get("processing", []):
        if isinstance(step, str):
            proc_rows.append({"step": step, "argument": ""})
        else:  # a {name: arg} mapping
            (sname, sarg), = step.items()
            proc_rows.append({"step": sname, "argument": _arg_to_text(sarg)})
    proc_df = pd.DataFrame(proc_rows, columns=["step", "argument"])
    edited_proc = st.data_editor(
        proc_df, num_rows="dynamic", use_container_width=True, key=f"proc::{name}",
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
        with open(out_path, "rb") as fh:
            st.download_button(
                "⬇ Download Excel", fh, file_name=out_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    except Exception as exc:
        st.error(f"Scrape failed: {exc}")
        with st.expander("Details"):
            st.code(traceback.format_exc())
