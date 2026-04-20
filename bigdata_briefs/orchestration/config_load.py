"""Load pipeline YAML/JSON config for entity runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_RELATIVE = (
    Path(__file__).resolve().parent / "default_pipeline_config.yaml"
)


def resolve_config_path(
    cli_path: str | None,
    env_var: str = "BRIEF_PIPELINE_CONFIG",
) -> Path | None:
    if cli_path:
        p = Path(cli_path).expanduser()
        return p if p.is_file() else None
    env = os.environ.get(env_var, "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    if DEFAULT_CONFIG_RELATIVE.is_file():
        return DEFAULT_CONFIG_RELATIVE
    return None


def load_pipeline_config_dict(
    path: Path | None = None,
) -> dict[str, Any]:
    """Load YAML or JSON config; returns empty dict if no file."""
    if path is None or not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return data if isinstance(data, dict) else {}
