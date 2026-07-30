"""The ONLY module in the whole tool allowed to call yaml.safe_load on an
app.yml file. Every other module (generators, build orchestrator, deploy
orchestrator, plugins) receives an already-validated AppConfig - never a raw
dict - which is what retires the old system's 4+ independent yaml.safe_load
call sites that let the schema silently drift (e.g. node_port ending up in
two incompatible shapes across real apps)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .compat import normalize
from .errors import ConfigError
from .models import AppConfig


def load_raw(path: Path) -> dict[str, Any]:
    text = path.read_text()
    raw = yaml.safe_load(text)
    if raw is None:
        raise ConfigError(path, "file is empty")
    if not isinstance(raw, dict):
        raise ConfigError(path, f"expected a YAML mapping at the top level, got {type(raw).__name__}")
    return raw


def load_app_config(path: Path) -> AppConfig:
    """Load and fully validate one app.yml. Raises ConfigError with the file
    path attached on any schema violation - never lets a pydantic
    ValidationError (or a raw KeyError from unvalidated legacy code) escape
    without knowing which file caused it."""
    raw = load_raw(path)
    raw = normalize(raw)
    try:
        return AppConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(path, str(exc)) from exc


def discover_apps(apps_dir: Path) -> dict[str, Path]:
    """Maps app name -> its app.yml path, for every apps/*/app.yml under
    apps_dir. Does not load/validate them yet - callers decide whether a
    partial failure (one bad app.yml) should block everything else."""
    result: dict[str, Path] = {}
    for app_yml in sorted(apps_dir.glob("*/app.yml")):
        result[app_yml.parent.name] = app_yml
    return result
