"""Command-line entry point:  python -m scraper <config> [-o OUTPUT]

Examples:
    python -m scraper                 # list available configs
    python -m scraper fx_rates        # run configs/fx_rates.yaml
    python -m scraper fx_rates -o /tmp/today.xlsx
"""

from __future__ import annotations

import argparse
import sys

from .config import list_configs
from .pipeline import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scraper",
        description="Scrape a configured site to Excel (extract -> process -> export).",
    )
    parser.add_argument(
        "config",
        nargs="?",  # "?" = optional positional arg; if omitted, args.config is None
        help="Config name (file in configs/, without .yaml). Omit to list configs.",
    )
    parser.add_argument(
        "-o", "--output", help="Override the output path from the config."
    )
    args = parser.parse_args(argv)  # argv=None -> argparse reads the real command line

    configs = list_configs()
    if not args.config:
        if configs:
            print("Available configs:")
            for name in configs:
                print(f"  {name}")
            print("\nRun one with:  python -m scraper <config>")
        else:
            print("No configs found. Add a YAML file under configs/.")
        return 0

    try:
        result = run(args.config, output=args.output)
    except Exception as exc:  # surface a clean message, not a traceback, to the desk
        print(f"error: {exc}", file=sys.stderr)  # stderr = the error stream, not stdout
        return 1  # non-zero exit code = "this failed" (shells/CI check this)

    print(
        f"Scraped {len(result.records)} row(s) from '{result.config.name}' "
        f"-> {result.output}"
    )
    return 0  # 0 = success


# this block runs only when the file is executed directly (python -m scraper), not
# when it's imported. SystemExit(code) makes the process exit with main()'s return code.
if __name__ == "__main__":
    raise SystemExit(main())
