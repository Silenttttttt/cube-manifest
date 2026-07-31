"""Service (+ the `_backend` variant used for scale-to-zero apps), headless
Service (for StatefulSets), and Ingress dict-builders.

Ported from `generate_service`/`generate_headless_service`/`generate_ingress`.
"""

from __future__ import annotations

from typing import Any

from cube_manifest.annotations import is_scale_to_zero
from cube_manifest.schema.models import AppConfig, ExternalEndpoint, ServiceSpec, ServiceType

from ._common import namespace_ref


def _legacy_port(app: AppConfig) -> int:
    """Ported from the repeated `docker_config.exposed_ports[0] or port`
    derivation used by both `generate_service`'s legacy single-port branch
    and (independently) annotations.py's backend-port precedence."""
    if app.docker_config.exposed_ports:
        return app.docker_config.exposed_ports[0]
    return app.port


def _ports_from_service_spec(service: ServiceSpec, service_type_value: str) -> list[dict[str, Any]]:
    ports = []
    for p in service.ports:
        entry: dict[str, Any] = {
            "name": p.name or "main",
            "port": p.port,
            "target_port": p.target_port if p.target_port is not None else p.port,
            "protocol": p.protocol,
        }
        if service_type_value == "NodePort" and p.node_port is not None:
            entry["node_port"] = p.node_port
        ports.append(entry)
    return ports


def _legacy_single_port(app: AppConfig, service_type_value: str) -> list[dict[str, Any]]:
    port = _legacy_port(app)
    entry: dict[str, Any] = {"name": "main", "port": port, "target_port": port, "protocol": "TCP"}
    if service_type_value == "NodePort":
        if app.node_port is None:
            raise ValueError(f"NodePort service '{app.name}' must have an explicit 'node_port' field in app.yml")
        entry["node_port"] = app.node_port
    return [entry]


def build_service(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_service`. `app.service` (the newer, richer
    `service:` block) wins over the legacy `service_type`/`port`/`node_port`
    top-level fields whenever its `ports[]` list is non-empty - matching the
    old generator's own "new format vs legacy format" branch exactly.

    Scale-to-zero apps (`scaling.min_replicas == 0`) get the app's own
    `-service` repointed to select the activator's pods instead of the real
    ones, plus a second, real `-backend-service` (ClusterIP-only, no
    NodePort even if the app itself is NodePort) that only the activator
    talks to directly - so any caller that bypasses an Ingress and hits the
    plain in-cluster Service still goes through the activator, not straight
    to (possibly-scaled-to-0) pods.
    """
    if not app.enabled:
        return {}

    service = app.service
    service_type_value = service.type.value if service is not None else app.service_type.value
    if service_type_value == "none":
        return {}

    namespace = namespace_ref(app.namespace)

    if service is not None and service.ports:
        ports = _ports_from_service_spec(service, service_type_value)
        backend_ports = _ports_from_service_spec(service, "ClusterIP")  # never NodePort for the backend
    else:
        ports = _legacy_single_port(app, service_type_value)
        backend_ports = _legacy_single_port(app, "ClusterIP")

    if is_scale_to_zero(app):
        return {"resource": {"kubernetes_service": {
            f"{app_name}_backend": {
                "metadata": {
                    "name": f"{app_name}-backend-service",
                    "namespace": namespace,
                    "labels": {"app": app_name, "managed-by": "cube_manifest"},
                },
                "spec": {"selector": {"app": app_name}, "port": backend_ports, "type": "ClusterIP"},
            },
            app_name: {
                "metadata": {"name": f"{app_name}-service", "namespace": namespace, "labels": {"app": app_name}},
                "spec": {"selector": {"app": "activator"}, "port": ports, "type": service_type_value},
            },
        }}}

    return {"resource": {"kubernetes_service": {app_name: {
        "metadata": {"name": f"{app_name}-service", "namespace": namespace, "labels": {"app": app_name}},
        "spec": {"selector": {"app": app_name}, "port": ports, "type": service_type_value},
    }}}}


def build_external_service_and_endpoints(app: AppConfig, app_name: str) -> dict[str, Any]:
    """`app_type: external`'s only real resources: the standard k8s "Service
    without selector" pattern for a real device that can never run as a pod
    in this cluster (see schema.models.ExternalEndpoint). A selector-less
    Service gives the device a clean in-cluster DNS name
    (`<app>-service.<namespace>.svc.cluster.local`); the matching Endpoints
    resource (same name as the Service - required by the k8s API for the two
    to be linked) is what actually tells kube-proxy where to route traffic,
    since there's no pod label for a Service selector to match here.

    All of this app's real ports live in ONE Endpoints subset, matching one
    address (the device's `host`) - Kubernetes applies every port in a
    subset to every address in that same subset, which is exactly what's
    wanted for a single external host exposing multiple distinct ports.

    Optionally, the top-level `service_type`/`port`/`node_port` fields (the
    same fields other app_types use for their single-Service NodePort case -
    see `_legacy_single_port`) can additionally NodePort-expose exactly ONE
    of this device's ports - e.g. fin-esp's web UI (port 80) needs to be
    reachable from outside the LAN (via the VPS over Tailscale), but its
    raw device-protocol ports (mic/media) don't. Since a k8s Service's `type`
    applies to the whole Service (every port in a NodePort-type Service gets
    a node port), this is done as a SEPARATE, second Service+Endpoints pair
    (`<app>-nodeport-service`) carrying only the matched port - the original
    selector-less Service/Endpoints above (and anything pointing at it, e.g.
    this app's own Ingress) is completely untouched.
    """
    if not app.enabled:
        return {}

    endpoint: ExternalEndpoint | None = app.external_endpoint
    if endpoint is None:
        return {}

    namespace = namespace_ref(app.namespace)
    service_name = f"{app_name}-service"

    service_ports = [
        {"name": p.name, "port": p.port, "target_port": p.port, "protocol": p.protocol} for p in endpoint.ports
    ]
    endpoint_ports = [{"name": p.name, "port": p.port, "protocol": p.protocol} for p in endpoint.ports]

    resources: dict[str, Any] = {
        "resource": {
            "kubernetes_service": {
                app_name: {
                    "metadata": {"name": service_name, "namespace": namespace, "labels": {"app": app_name}},
                    "spec": {"port": service_ports, "type": "ClusterIP"},
                }
            },
            "kubernetes_endpoints": {
                app_name: {
                    "metadata": {"name": service_name, "namespace": namespace, "labels": {"app": app_name}},
                    "subset": {
                        "address": {"ip": endpoint.host},
                        "port": endpoint_ports,
                    },
                }
            },
        }
    }

    if app.service_type == ServiceType.node_port:
        if app.node_port is None:
            raise ValueError(f"NodePort service '{app.name}' must have an explicit 'node_port' field in app.yml")
        matches = [p for p in endpoint.ports if p.port == app.port]
        if not matches:
            raise ValueError(
                f"external app '{app.name}' has service_type NodePort for port {app.port}, but no "
                f"external_endpoint.ports entry has port {app.port}"
            )
        node_port_port = matches[0]
        nodeport_service_name = f"{app_name}-nodeport-service"
        resources["resource"]["kubernetes_service"][f"{app_name}_nodeport"] = {
            "metadata": {"name": nodeport_service_name, "namespace": namespace, "labels": {"app": app_name}},
            "spec": {
                "port": [
                    {
                        "name": node_port_port.name,
                        "port": node_port_port.port,
                        "target_port": node_port_port.port,
                        "protocol": node_port_port.protocol,
                        "node_port": app.node_port,
                    }
                ],
                "type": "NodePort",
            },
        }
        resources["resource"]["kubernetes_endpoints"][f"{app_name}_nodeport"] = {
            "metadata": {"name": nodeport_service_name, "namespace": namespace, "labels": {"app": app_name}},
            "subset": {
                "address": {"ip": endpoint.host},
                "port": [{"name": node_port_port.name, "port": node_port_port.port, "protocol": node_port_port.protocol}],
            },
        }

    return resources


def build_headless_service(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_headless_service` - the stable-network-identity
    Service every StatefulSet needs."""
    namespace = namespace_ref(app.namespace)
    return {"resource": {"kubernetes_service": {f"{app_name}_headless": {
        "metadata": {"name": f"{app_name}-headless", "namespace": namespace, "labels": {"app": app_name}},
        "spec": {
            "cluster_ip": "None",
            "selector": {"app": app_name},
            "port": {"port": app.port, "target_port": app.port, "protocol": "TCP"},
        },
    }}}}


def build_ingress(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_ingress`. `IngressConfig.tls` defaults to True,
    matching the old generator's real default exactly (confirmed via a real
    cutover attempt against local-storage-ui's live Ingress - defaulting to
    False here would have silently stripped its real, working TLS cert)."""
    if not app.enabled:
        return {}
    ing = app.ingress
    if not ing.enabled:
        return {}

    namespace = namespace_ref(app.namespace)
    body: dict[str, Any] = {
        "metadata": {
            "name": f"{app_name}-ingress",
            "namespace": namespace,
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        },
        "spec": {
            "ingress_class_name": "traefik",
            "rule": {
                "host": ing.host,
                "http": {
                    "path": {
                        "path": "/",
                        "path_type": "Prefix",
                        "backend": {"service": {"name": f"{app_name}-service", "port": {"number": ing.service_port}}},
                    }
                },
            },
        },
    }
    if ing.tls:
        body["spec"]["tls"] = {"hosts": [ing.host], "secret_name": ing.tls_secret or "internal-wildcard-tls"}

    return {"resource": {"kubernetes_ingress_v1": {app_name: body}}}
