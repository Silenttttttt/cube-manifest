"""Contract test: cube_manifest.annotations.build_activator_annotations() must
produce exactly what the REAL, separately-deployed activator process
(Cubernetes/apps/activator/scaler.py::AppConfig.from_deployment_annotations)
expects to parse back.

This is the one place a silent spelling/encoding drift would be invisible
until an idle app mysteriously never wakes up again, so this test imports
and calls the actual activator module rather than hand-maintaining a second
copy of the expected keys - a typo here would make the *test* wrong in the
exact same way as the generator, defeating the point.

Import-safety check performed before writing this: scaler.py's only
module-level imports are stdlib (asyncio/logging/time/dataclasses/typing)
plus `from kubernetes import client` / `from kubernetes.client.rest import
ApiException`. Neither of those two touches the network or requires a live
cluster at *import* time - client construction (`client.AppsV1Api()`) only
happens inside `Scaler.__init__`, which this test never calls. So the real
module is loaded directly via importlib (by file path, since
apps/activator/ is a separate repo/sibling project with no installed
package name) - no live k8s connection, no fallback to a hand-maintained
comparison needed. `kubernetes` itself is a test-only dev dependency of
cube_manifest (see pyproject.toml), pinned to match
apps/activator/requirements.txt's kubernetes==32.0.1 exactly; cube_manifest's own
runtime code never imports it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from cube_manifest.annotations import build_activator_annotations, is_scale_to_zero
from cube_manifest.schema.models import (
    Activation,
    ActivationType,
    AppConfig,
    DockerConfig,
    QueueRef,
    Scaling,
    ServicePort,
    ServiceSpec,
)

ACTIVATOR_SCALER_PATH = Path(
    "/home/silent/Documents/Cubernetes/apps/activator/scaler.py"
)


def _load_real_scaler():
    """Loads the real activator scaler.py module by file path (it lives in
    a separate, unmodified sibling repo with no installed package name), so
    the round-trip below calls the ACTUAL parsing code, not a copy of it."""
    if not ACTIVATOR_SCALER_PATH.exists():
        pytest.skip(f"real activator scaler.py not found at {ACTIVATOR_SCALER_PATH}")
    spec = importlib.util.spec_from_file_location("activator_scaler_real", ACTIVATOR_SCALER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


real_scaler = _load_real_scaler()
RealAppConfig = real_scaler.AppConfig


def _minimal_app(name: str, **overrides) -> AppConfig:
    return AppConfig(name=name, enabled=True, **overrides)


# ---------------------------------------------------------------------------
# Fixture AppConfigs covering distinct scaling: permutations
# ---------------------------------------------------------------------------

def fixture_no_scaling_block() -> AppConfig:
    """No scaling: block at all -> min_replicas defaults to 1 -> the
    activator should never manage this app, so no annotations/label at
    all should be generated."""
    return _minimal_app("plain-app")


def fixture_http_min_zero() -> AppConfig:
    """min_replicas: 0, activation.type: http, otherwise all-default -
    the simplest real scale-to-zero shape (e.g. a bare web app fronted by
    the activator's HTTP proxy)."""
    return _minimal_app(
        "web-app",
        port=8080,
        scaling=Scaling(
            min_replicas=0,
            max_replicas=1,
            activation=Activation(type=ActivationType.http),
        ),
    )


def fixture_tcp_extra_ports() -> AppConfig:
    """activation.type: tcp with extra_ports - rabbitmq's real shape (AMQP
    5672 + management UI 15672), plus a docker_config.exposed_ports-derived
    backend port (no explicit activation.port) to exercise the port
    precedence fallback chain."""
    return _minimal_app(
        "rabbitmq",
        docker_config=DockerConfig(exposed_ports=[5672]),
        dependencies=["postgres"],
        scaling=Scaling(
            min_replicas=0,
            max_replicas=3,
            target_cpu_utilization_percentage=80,
            idle_timeout_seconds=600,
            activation=Activation(type=ActivationType.tcp, extra_ports=[15672]),
        ),
    )


def fixture_queue_depth() -> AppConfig:
    """activation.type: queue_depth with a queue block - the worker-behind-
    a-queue shape, activation.port explicitly set (takes precedence over
    everything else)."""
    return _minimal_app(
        "queue-worker",
        service=ServiceSpec(ports=[ServicePort(port=9000)]),
        scaling=Scaling(
            min_replicas=0,
            activation=Activation(
                type=ActivationType.queue_depth,
                port=9001,
                queue=QueueRef(name="worker-queue", host="rabbitmq-service.dev.svc"),
            ),
        ),
    )


def fixture_write_protected() -> AppConfig:
    """write_protected: true - gates mutating HTTP methods behind the
    shared activator secret (local-storage's real shape today)."""
    return _minimal_app(
        "local-storage",
        port=9090,
        dependencies=["postgres", "rabbitmq"],
        scaling=Scaling(
            min_replicas=0,
            write_protected=True,
            activation=Activation(type=ActivationType.http),
        ),
    )


ALL_FIXTURES = [
    fixture_no_scaling_block,
    fixture_http_min_zero,
    fixture_tcp_extra_ports,
    fixture_queue_depth,
    fixture_write_protected,
]


# ---------------------------------------------------------------------------
# Non-scale-to-zero app: no annotations at all
# ---------------------------------------------------------------------------

def test_no_scaling_block_produces_no_annotations():
    app = fixture_no_scaling_block()
    assert is_scale_to_zero(app) is False
    assert build_activator_annotations(app) == {}


# ---------------------------------------------------------------------------
# Exact key-set check: every scale-to-zero app must emit exactly the 11 keys
# scaler.py's from_deployment_annotations actually reads - no more, no fewer.
# ---------------------------------------------------------------------------

EXPECTED_KEYS = {
    "activator.cubernetes.io/activation-type",
    "activator.cubernetes.io/backend-service",
    "activator.cubernetes.io/backend-port",
    "activator.cubernetes.io/extra-backend-ports",
    "activator.cubernetes.io/hpa-max-replicas",
    "activator.cubernetes.io/hpa-target-cpu-percentage",
    "activator.cubernetes.io/idle-timeout-seconds",
    "activator.cubernetes.io/queue-name",
    "activator.cubernetes.io/queue-host",
    "activator.cubernetes.io/write-protected",
    "activator.cubernetes.io/depends-on",
}


@pytest.mark.parametrize(
    "make_app",
    [fixture_http_min_zero, fixture_tcp_extra_ports, fixture_queue_depth, fixture_write_protected],
)
def test_exact_key_set(make_app):
    app = make_app()
    assert is_scale_to_zero(app) is True
    annotations = build_activator_annotations(app)
    assert set(annotations.keys()) == EXPECTED_KEYS
    for value in annotations.values():
        assert isinstance(value, str)


# ---------------------------------------------------------------------------
# The real round-trip: feed our generated annotations into the ACTUAL
# activator parsing function and assert the logical config it reconstructs
# matches what the fixture AppConfig actually says.
# ---------------------------------------------------------------------------

def test_round_trip_http_min_zero():
    app = fixture_http_min_zero()
    annotations = build_activator_annotations(app)
    parsed = RealAppConfig.from_deployment_annotations(app.name, annotations)

    assert parsed.activation_type == "http"
    assert parsed.backend_service == f"{app.name}-backend-service"
    assert parsed.backend_port == 8080  # falls back to top-level port: 8080
    assert parsed.extra_backend_ports == ()
    assert parsed.hpa_max_replicas == 1
    assert parsed.hpa_target_cpu_percentage == 70
    assert parsed.idle_timeout_seconds == 300
    assert parsed.queue_name == ""
    assert parsed.queue_host == "rabbitmq-service"
    assert parsed.depends_on == ()
    assert parsed.write_protected is False


def test_round_trip_tcp_extra_ports():
    app = fixture_tcp_extra_ports()
    annotations = build_activator_annotations(app)
    parsed = RealAppConfig.from_deployment_annotations(app.name, annotations)

    assert parsed.activation_type == "tcp"
    assert parsed.backend_port == 5672  # docker_config.exposed_ports[0] fallback
    assert parsed.extra_backend_ports == (15672,)
    assert parsed.hpa_max_replicas == 3
    assert parsed.hpa_target_cpu_percentage == 80
    assert parsed.idle_timeout_seconds == 600
    assert parsed.depends_on == ("postgres",)
    assert parsed.write_protected is False


def test_round_trip_queue_depth():
    app = fixture_queue_depth()
    annotations = build_activator_annotations(app)
    parsed = RealAppConfig.from_deployment_annotations(app.name, annotations)

    assert parsed.activation_type == "queue_depth"
    assert parsed.backend_port == 9001  # explicit activation.port wins over service.ports[0]
    assert parsed.queue_name == "worker-queue"
    assert parsed.queue_host == "rabbitmq-service.dev.svc"
    assert parsed.write_protected is False


def test_round_trip_write_protected():
    app = fixture_write_protected()
    annotations = build_activator_annotations(app)
    parsed = RealAppConfig.from_deployment_annotations(app.name, annotations)

    assert parsed.activation_type == "http"
    assert parsed.backend_port == 9090  # top-level port fallback (no exposed_ports/service.ports/activation.port)
    assert parsed.depends_on == ("postgres", "rabbitmq")
    assert parsed.write_protected is True


@pytest.mark.parametrize("make_app", ALL_FIXTURES)
def test_round_trip_generic_over_all_fixtures(make_app):
    """Belt-and-suspenders: for every fixture (including the non-scale-to-
    zero one), whatever build_activator_annotations() emits is at minimum
    parseable by the real activator without raising, and re-encodes
    write_protected/activation_type consistently."""
    app = make_app()
    annotations = build_activator_annotations(app)
    parsed = RealAppConfig.from_deployment_annotations(app.name, annotations)

    if is_scale_to_zero(app):
        assert parsed.activation_type == app.scaling.activation.type.value
        assert parsed.write_protected == app.scaling.write_protected
    else:
        # {} annotations -> the real parser's own hardcoded defaults apply,
        # NOT this (non-managed) app's actual scaling config - proving that
        # an unmanaged app really does produce nothing an activator restart
        # could misinterpret as "managed with default settings".
        assert annotations == {}
        assert parsed.activation_type == "http"
        assert parsed.write_protected is False
