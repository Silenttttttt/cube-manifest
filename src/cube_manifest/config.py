"""Cluster-specific configuration - the layer that keeps one homelab's real
values (registry URL, at minimum) out of the generators themselves.

`_common.py`'s `registry_image_reference()` used to hardcode a specific
homelab's registry IP directly in source. That's exactly the kind of value a
public, installable package shouldn't ship as its actual default - so it
lives here instead, in a small `ClusterConfig` model with a genuinely generic
OSS default, loaded from an optional `.cube-manifest.yaml` file (discovered
by walking up from a start directory, the same way `.git`/`.eslintrc` get
found) with an env var override on top.

Every other module that needs "the current cluster's config" reads it via
`get_cluster_config()` rather than taking it as a parameter - threading a
config object through every generator function in `generators/terraform/*.py`
would be a large, invasive refactor for what's currently a single value.
`set_cluster_config()` is the one place that mutates the module-level
default; `cli.py` calls it once per command, right after resolving
`apps_dir`, so the CLI is the only thing that ever changes it in a real run.
Tests reset it via an autouse fixture (see tests/conftest.py) so one test's
`set_cluster_config()` call can never leak into another test.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

CONFIG_FILENAME = ".cube-manifest.yaml"
REGISTRY_URL_ENV_VAR = "CUBE_MANIFEST_REGISTRY_URL"


class ClusterConfig(BaseModel):
    """This specific deployment's own values. Defaults here are meant to be
    generic enough to work out of the box for anyone installing this as a
    public package - a real homelab overrides them via `.cube-manifest.yaml`
    (or the env var), never by editing this file."""

    model_config = ConfigDict(extra="forbid")
    registry_url: str = "localhost:5000"


def _find_config_file(start_dir: Path) -> Path | None:
    """Walks start_dir, then its parents, up to (but not past) the
    filesystem root, looking for CONFIG_FILENAME - the same discovery
    pattern tools like `.git`/`.eslintrc` use."""
    current = start_dir.resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_cluster_config(start_dir: Path) -> ClusterConfig:
    """Searches start_dir and its parents for a `.cube-manifest.yaml`.
    `CUBE_MANIFEST_REGISTRY_URL`, if set, always overrides whatever the file
    says. With neither a file nor the env var, returns the model's own
    generic default."""
    data: dict[str, object] = {}
    config_path = _find_config_file(start_dir)
    if config_path is not None:
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path}: expected a YAML mapping at the top level, got {type(loaded).__name__}")
        data = dict(loaded)

    env_registry_url = os.environ.get(REGISTRY_URL_ENV_VAR)
    if env_registry_url is not None:
        data["registry_url"] = env_registry_url

    return ClusterConfig(**data)


_current_cluster_config = ClusterConfig()


def set_cluster_config(cfg: ClusterConfig) -> None:
    """The one function that mutates the module-level "current config"."""
    global _current_cluster_config
    _current_cluster_config = cfg


def get_cluster_config() -> ClusterConfig:
    """What every generator reads instead of a hardcoded literal. Defaults to
    a plain `ClusterConfig()` (the generic default) if `set_cluster_config`
    was never called - e.g. any test/script that imports a generator
    function directly without going through the CLI."""
    return _current_cluster_config
