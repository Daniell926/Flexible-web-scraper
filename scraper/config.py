"""Per-site configuration.

A new site is, ideally, a new YAML file in `configs/` -- not new code. The
config says which Source type to use and carries that source's settings
(URL + selectors, or an API endpoint).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# __file__ = this file's path. .resolve() makes it absolute; .parent = the scraper/
# package folder, .parent.parent = the project root; then `/ "configs"` joins on the
# folder name. so CONFIG_DIR is always <project>/configs, no matter the working dir.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


@dataclass
class SourceConfig:
    """Parsed contents of a configs/<name>.yaml file."""

    # fields with NO default (name/type/url) are required when building the object;
    # fields WITH a default below are optional -- a config can leave them out.
    name: str
    type: str  # "html" | "browser" | "api"
    url: str
    # For html/browser: a mapping of column-name -> CSS selector.
    selectors: dict[str, str] = field(default_factory=dict)
    # For api: how to reach + walk the JSON (endpoint, json path, etc).
    api: dict[str, Any] = field(default_factory=dict)
    # Where the Excel file should land (relative paths resolved from cwd).
    output: str = "output.xlsx"
    # Anything else the source/export wants, kept open-ended for flexibility.
    options: dict[str, Any] = field(default_factory=dict)


def load_config(name: str) -> SourceConfig:
    """Load and validate a single site config by name (without .yaml)."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No config '{name}' at {path}. Available: {list_configs() or '(none)'}"
        )
    # read_text() = the whole file as a string; safe_load parses that YAML string
    # into python dict/list. `or {}` handles an empty file (safe_load returns None).
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("name", name)  # only set "name" if the yaml didn't already
    if "type" not in data or "url" not in data:
        raise ValueError(f"Config '{name}' must define at least 'type' and 'url'.")
    known = {f for f in SourceConfig.__dataclass_fields__}  # this class' fields
    # check the yaml config matches
    # set(data) = just the yaml's KEYS; minus `known` = any key the class doesn't have
    # (i.e. a typo). only catches EXTRA keys, not missing ones (those use defaults).
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Config '{name}' has unknown keys: {sorted(unknown)}")
    # **data spreads the dict into keyword args: SourceConfig(name=..., type=..., ...).
    # keys not present fall back to the dataclass defaults above.
    return SourceConfig(**data)


def list_configs() -> list[str]:
    """Names of all available site configs."""
    if not CONFIG_DIR.exists():
        return []
    # glob("*.yaml") = every .yaml file in the folder; p.stem = filename without the
    # extension (configs/fx_rates.yaml -> "fx_rates"). sorted() for stable A-Z order.
    return sorted(p.stem for p in CONFIG_DIR.glob("*.yaml"))
