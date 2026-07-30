"""Covers `cube_manifest.config`: the generic default, `.cube-manifest.yaml`
discovery (including walking up to a parent directory), the env var
override, and - the important part per the task spec - that
`set_cluster_config` actually changes what the Terraform generator emits
(not just that the config object itself holds a new value). Test isolation
for the module-level "current config" is handled by the autouse fixture in
tests/conftest.py; this file's own `test_set_cluster_config_...` tests still
double as documentation that the fixture is doing its job (each test starts
from the generic default, not whatever the previous test set).
"""

from __future__ import annotations

import os

import pytest

from cube_manifest.config import (
    ClusterConfig,
    load_cluster_config,
    set_cluster_config,
)
from cube_manifest.generators.terraform._common import (
    default_pull_policy,
    get_image_reference,
    registry_image_reference,
)
from cube_manifest.schema.models import AppConfig, DockerConfig


def _locally_built_app(name: str) -> AppConfig:
    # A non-default docker_config is the signal `is_locally_built` uses -
    # see _common.py's docstring on that function.
    return AppConfig(name=name, enabled=True, docker_config=DockerConfig(language="python"))


# ---------------------------------------------------------------------------
# The generic OSS default - never a homelab-specific literal.
# ---------------------------------------------------------------------------


def test_default_registry_url_is_generic() -> None:
    assert ClusterConfig().registry_url == "localhost:5000"


def test_unset_config_uses_generic_default_in_generated_image_ref() -> None:
    # No set_cluster_config call in this test - the autouse fixture already
    # reset the module-level config to ClusterConfig() before this ran.
    assert registry_image_reference("myapp") == "localhost:5000/myapp:latest"
    app = _locally_built_app("myapp")
    assert get_image_reference("myapp", app) == "localhost:5000/myapp:latest"


# ---------------------------------------------------------------------------
# set_cluster_config actually changes generator output - not just the config
# object's own field.
# ---------------------------------------------------------------------------


def test_set_cluster_config_changes_registry_image_reference() -> None:
    set_cluster_config(ClusterConfig(registry_url="192.168.1.105:30500"))
    assert registry_image_reference("rabbitmq-sender") == "192.168.1.105:30500/rabbitmq-sender:latest"


def test_set_cluster_config_changes_get_image_reference_for_locally_built_app() -> None:
    set_cluster_config(ClusterConfig(registry_url="192.168.1.105:30500"))
    app = _locally_built_app("rabbitmq-sender")
    assert get_image_reference("rabbitmq-sender", app) == "192.168.1.105:30500/rabbitmq-sender:latest"


def test_default_pull_policy_matches_the_configured_registry_url() -> None:
    # docker-registry-service.<ns>.svc references hit the "Always" branch
    # unconditionally - use that as a stable control to prove the *other*
    # half of the startswith() tuple is really driven by the live config,
    # not a leftover hardcoded literal.
    set_cluster_config(ClusterConfig(registry_url="my-registry.example:5000"))
    assert default_pull_policy("my-registry.example:5000/rabbitmq-sender:latest") == "Always"
    # The OLD homelab-specific literal must NOT get special-cased anymore
    # once the config points elsewhere - proves this isn't still hardcoded.
    assert default_pull_policy("192.168.1.105:30500/rabbitmq-sender:latest") == "Never"


def test_config_isolated_between_tests_by_autouse_fixture() -> None:
    # If the previous test's set_cluster_config leaked, this would see
    # 192.168.1.105:30500 instead of the generic default.
    assert registry_image_reference("leak-check") == "localhost:5000/leak-check:latest"


# ---------------------------------------------------------------------------
# .cube-manifest.yaml discovery
# ---------------------------------------------------------------------------


def test_load_cluster_config_no_file_returns_generic_default(tmp_path) -> None:
    cfg = load_cluster_config(tmp_path)
    assert cfg.registry_url == "localhost:5000"


def test_load_cluster_config_finds_file_in_start_dir(tmp_path) -> None:
    (tmp_path / ".cube-manifest.yaml").write_text("registry_url: 10.0.0.1:5000\n")
    cfg = load_cluster_config(tmp_path)
    assert cfg.registry_url == "10.0.0.1:5000"


def test_load_cluster_config_walks_up_to_parent(tmp_path) -> None:
    (tmp_path / ".cube-manifest.yaml").write_text("registry_url: 10.0.0.2:5000\n")
    nested = tmp_path / "apps" / "someapp"
    nested.mkdir(parents=True)
    cfg = load_cluster_config(nested)
    assert cfg.registry_url == "10.0.0.2:5000"


def test_load_cluster_config_env_var_overrides_file(tmp_path, monkeypatch) -> None:
    (tmp_path / ".cube-manifest.yaml").write_text("registry_url: 10.0.0.3:5000\n")
    monkeypatch.setenv("CUBE_MANIFEST_REGISTRY_URL", "env-wins:5000")
    cfg = load_cluster_config(tmp_path)
    assert cfg.registry_url == "env-wins:5000"


def test_load_cluster_config_env_var_without_any_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CUBE_MANIFEST_REGISTRY_URL", "env-only:5000")
    cfg = load_cluster_config(tmp_path)
    assert cfg.registry_url == "env-only:5000"


def test_load_cluster_config_rejects_non_mapping_yaml(tmp_path) -> None:
    (tmp_path / ".cube-manifest.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_cluster_config(tmp_path)


@pytest.fixture(autouse=True)
def _clean_registry_env(monkeypatch):
    # Belt-and-suspenders: make sure a real CUBE_MANIFEST_REGISTRY_URL set in
    # the surrounding shell can't leak into the "no env var" test cases.
    monkeypatch.delenv("CUBE_MANIFEST_REGISTRY_URL", raising=False)
    yield


def test_env_var_name_matches_documented_constant() -> None:
    from cube_manifest.config import REGISTRY_URL_ENV_VAR

    assert REGISTRY_URL_ENV_VAR == "CUBE_MANIFEST_REGISTRY_URL"
    assert os.environ.get(REGISTRY_URL_ENV_VAR) is None
