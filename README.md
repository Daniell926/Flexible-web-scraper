# web scraper

A component-based scraper for the brokerage internship: pull data off the web,
clean it, and drop it into a ready-to-use Excel file. Built so you **don't need
to know the target site in advance** — a new site is a new YAML file in
`configs/`, not new code.

## The three components

```
   Extract            Process            Export
  (sources/)       (processing.py)     (export.py)
      │                  │                  │
   site → list of  →  cleaned list of  →  formatted .xlsx
        ScrapeRecord     ScrapeRecord
```

Every stage speaks one shared data contract — `ScrapeRecord` (see
`scraper/records.py`). No stage knows how the others work, so you can swap any of
them without touching the rest.

1. **Extract** — three interchangeable sources, picked by `type:` in the config:
   - `api` — read JSON straight from an endpoint (most robust; **prefer this**).
   - `html` — HTTP GET + CSS selectors (for server-rendered pages).
   - `browser` — headless Chromium via Playwright (for JavaScript-rendered pages).
2. **Process** — declarative clean-up listed under `options.processing`
   (`strip`, `drop_empty`, `dedupe`, `numeric`, `rename`, `sort`).
3. **Export** — a formatted `.xlsx`: bold frozen header, auto-filter, auto-sized
   columns, plus a `_meta` sheet recording the source and timestamp.

## Setup

```bash
pip install -r requirements.txt
# for `type: browser` configs only:
# pip install playwright && playwright install chromium
```

## Usage

```bash
python -m scraper                 # list available configs
python -m scraper fx_rates        # run configs/fx_rates.yaml -> output/fx_rates.xlsx
python -m scraper fx_rates -o /tmp/today.xlsx   # override the output path
```

## GUI (optional)

A Streamlit front end wraps the same pipeline for non-technical users — pick a
source from a dropdown, tweak parameters, run, preview the table, download the Excel.

```bash
pip install -r requirements-gui.txt   # core stack + streamlit
streamlit run app.py                   # opens in your browser
```

It's a thin shell over `run_config()` — no scraping logic lives in `app.py`.

## Adding a new site

Drop a YAML file in `configs/`. No code needed. See the worked examples:

- `configs/fx_rates.yaml` — **API**, ECB FX rates (free, no key). Good first run.
- `configs/wti_eia.yaml` — **API**, WTI crude spot prices from the EIA (needs a
  free API key). The oil-desk-relevant one.
- `configs/example_html.yaml` — **HTML**, shows the `row_selector` + CSS pattern.

### Config reference

```yaml
name: my_site              # optional; defaults to the filename
type: api | html | browser # which Source to use
url: https://...

# --- api sources ---
api:
  method: GET              # optional
  params: {symbol: BRENT}  # optional query string
  headers: {Authorization: "Bearer ..."}   # optional
  records_path: data.rows  # dotted path to the collection; omit for whole document
  fields:                  # output column -> dotted path in each item
    symbol: ticker         #   ("@key"/"@value" when the collection is a dict)
    price:  last.value

# --- html / browser sources ---
selectors:                 # output column -> CSS selector ("@attr" reads an attribute)
  title: "h3 a@title"
  price: "p.price_color"
options:
  row_selector: "article"  # one record per match; selectors run inside each

# --- processing (any source) ---
options:
  processing:
    - strip
    - drop_empty
    - dedupe
    - numeric: [price]
    - rename: {price: price_usd}
    - sort: "-price"

output: output/my_site.xlsx
```

## Layout

```
scraper/
  config.py        # load + validate configs/<name>.yaml
  records.py       # the ScrapeRecord data contract (shared by every stage)
  pipeline.py      # orchestrator: load -> fetch -> process -> export
  __main__.py      # CLI
  processing.py    # the Process stage
  export.py        # the Export stage (Excel)
  sources/         # the Extract stage
    base.py        #   Source protocol + get_source() dispatch
    api_source.py
    html_source.py
    browser_source.py
    _selectors.py  #   shared HTML->records logic (html + browser)
configs/           # one YAML per site
output/            # generated .xlsx files
```
