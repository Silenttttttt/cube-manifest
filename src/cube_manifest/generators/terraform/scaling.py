"""HorizontalPodAutoscaler dict-builder, plus the glue that stamps a
scale-to-zero app's activator annotations/label/ignore_changes onto its
Deployment (workloads.py merges this in - the actual annotation *values* are
built by `cube_manifest.annotations.build_activator_annotations`, owned by a
parallel task; this module only ever consumes that function).
"""

from __future__ import annotations

from typing import Any

from cube_manifest.annotations import (
    LAST_ACTIVE_ANNOTATION,
    MANAGED_LABEL_KEY,
    build_activator_annotations,
    is_scale_to_zero,
)
from cube_manifest.schema.models import AppConfig

from ._common import namespace_ref


def build_hpa(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_hpa`. A real HPA can't validly hold
    `min_replicas: 0` with only a CPU metric, so scale-to-zero apps
    (`scaling.min_replicas == 0`) get none here at all - the activator
    creates/deletes this exact HPA object directly via the k8s API around
    the 0-replica window instead, using the same min/max stamped as
    Deployment annotations by `activator_deployment_extras` below."""
    if not app.enabled:
        return {}

    max_replicas = app.scaling.max_replicas
    if max_replicas <= 1:
        return {}
    if app.scaling.min_replicas == 0:
        return {}

    min_replicas = max(1, app.scaling.min_replicas)
    target_cpu = app.scaling.target_cpu_utilization_percentage
    namespace = namespace_ref(app.namespace)

    return {"resource": {"kubernetes_horizontal_pod_autoscaler_v2": {app_name: {
        "metadata": {
            "name": f"{app_name}-hpa",
            "namespace": namespace,
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        },
        "spec": {
            "scale_target_ref": {"api_version": "apps/v1", "kind": "Deployment", "name": app_name},
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "metric": {
                "type": "Resource",
                "resource": {"name": "cpu", "target": {"type": "Utilization", "average_utilization": target_cpu}},
            },
        },
    }}}}


def activator_deployment_extras(app: AppConfig) -> dict[str, Any]:
    """Returns the extra pieces a scale-to-zero app's Deployment needs:
    `annotations` (the activator's own config, from `build_activator_annotations`),
    `labels` (just `activator.cubernetes.io/managed: "true"`), and
    `ignore_changes` (replica count + the activator's own runtime-written
    last-active timestamp - both owned by the activator at runtime, never
    reconciled back by Terraform). Returns all-empty when the app isn't
    scale-to-zero, so workloads.py can merge this in unconditionally.
    """
    if not is_scale_to_zero(app):
        return {"annotations": {}, "labels": {}, "ignore_changes": []}

    return {
        "annotations": dict(build_activator_annotations(app)),
        "labels": {MANAGED_LABEL_KEY: "true"},
        "ignore_changes": ["spec[0].replicas", f'metadata[0].annotations["{LAST_ACTIVE_ANNOTATION}"]'],
    }
