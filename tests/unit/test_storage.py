"""StorageEntry's three mutually-exclusive backings (PVC / hostPath /
emptyDir) - added alongside the first real emptyDir use case (a small
config file shared between an init container and the main container within
one pod, with no need to persist across pod restarts or tie to a specific
node's filesystem)."""

from __future__ import annotations

import pytest

from cube_manifest.generators.terraform.storage import build_volume_mounts, build_volumes
from cube_manifest.schema.models import AppConfig, StorageEntry


def _app(**storage_kwargs) -> AppConfig:
    return AppConfig(
        name="test-app",
        enabled=True,
        storage=[StorageEntry(name="cfg", mount_path="/etc/cfg", **storage_kwargs)],
    )


def test_empty_dir_is_a_valid_sole_backing():
    entry = StorageEntry(name="cfg", mount_path="/etc/cfg", empty_dir=True)
    assert entry.empty_dir is True
    assert entry.size is None
    assert entry.host_path is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # none of size/host_path/empty_dir set
        {"size": "1Gi", "host_path": "/data"},
        {"size": "1Gi", "empty_dir": True},
        {"host_path": "/data", "empty_dir": True},
        {"size": "1Gi", "host_path": "/data", "empty_dir": True},
    ],
)
def test_rejects_zero_or_multiple_backings(kwargs):
    with pytest.raises(ValueError, match="exactly one of"):
        StorageEntry(name="cfg", mount_path="/etc/cfg", **kwargs)


def test_build_volumes_emits_empty_dir():
    app = _app(empty_dir=True)
    volumes = build_volumes(app, "test-app")
    assert volumes == [{"name": "cfg", "empty_dir": {}}]


def test_build_volume_mounts_works_for_empty_dir_like_any_other_entry():
    app = _app(empty_dir=True)
    mounts = build_volume_mounts(app)
    assert mounts == [{"name": "cfg", "mount_path": "/etc/cfg", "read_only": False}]
