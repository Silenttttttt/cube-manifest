"""Shared pytest fixtures.

`cube_manifest.config` holds one module-level "current cluster config"
(see its docstring) that generators read instead of taking a parameter.
That's real global mutable state - without resetting it between tests, a
test that calls `set_cluster_config(...)` would leak its override into
every test that runs afterward, in file order, silently. This autouse
fixture resets it to the generic default before AND after every test, so
`test_cluster_config.py`'s override test can never affect any other test
file's assertions (or vice versa, depending on collection order).
"""

from __future__ import annotations

import pytest

from cube_manifest.config import ClusterConfig, set_cluster_config


@pytest.fixture(autouse=True)
def _reset_cluster_config():
    set_cluster_config(ClusterConfig())
    yield
    set_cluster_config(ClusterConfig())
