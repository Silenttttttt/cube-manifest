"""PersistentVolumeClaim dict-builder plus the volume/volume_mount fragments
shared by every workload builder (Deployment/StatefulSet/DaemonSet/Job).

`StorageEntry` (schema.models) already unifies the old system's two
independent shapes - `storage[]` (PVC-backed) and the legacy `volumes[]`
(hostPath-only, merged into `storage[]` by compat.py's `normalize_storage`
before an AppConfig ever exists) - into ONE list where `host_path` set means
"generate a hostPath volume" and `size` set means "generate a PVC", exactly
one of the two ever being set (enforced by the model itself). That
unification is what lets this module iterate `app.storage` once instead of
the old code's two separate loops (one over `volumes[]`, one over
`storage[]`) repeated in every one of `_build_volume_mounts`/`_build_volumes`/
`_build_volumes_for_statefulset`.
"""

from __future__ import annotations

from typing import Any

from cube_manifest.schema.models import AppConfig

from ._common import namespace_ref


def build_volume_mounts(app: AppConfig) -> list[dict[str, Any]]:
    """Container-spec `volume_mount` blocks - ported from `_build_volume_mounts`.

    (The old code's `config_mount_path` override for registry_config's mount
    path is dead, unmodeled data - `config.get('config_mount_path')` was
    never a real field on any of the 26 apps' app.yml and isn't in the
    schema, so it's dropped here too rather than resurrected.)
    """
    mounts: list[dict[str, Any]] = []
    if app.registry_config is not None:
        mounts.append({"name": "registry-config", "mount_path": "/etc/docker/registry", "read_only": True})
    for s in app.storage:
        mounts.append({"name": s.name, "mount_path": s.mount_path, "read_only": s.read_only})
    return mounts


def build_volumes(app: AppConfig, app_name: str, *, include_fresh_pvc: bool = True) -> list[dict[str, Any]]:
    """Pod-spec `volume` blocks - ported from `_build_volumes` (Deployment/
    DaemonSet path) and `_build_volumes_for_statefulset` (same logic, minus
    the "fresh PVC" case, which a StatefulSet instead references via its own
    `volume_claim_template`).

    Precedence per storage entry, matching the unified StorageEntry shape:
    host_path -> hostPath volume; existing_pvc -> reference it directly;
    get_or_create -> reference the get-or-create-named PVC resource;
    otherwise (fresh, non-get_or_create PVC) -> reference the
    `{name}_pvc`-named PVC resource, but only when `include_fresh_pvc` is
    True (StatefulSet callers pass False since that case is handled by
    `volume_claim_template` instead).
    """
    volumes: list[dict[str, Any]] = []
    if app.registry_config is not None:
        volumes.append({
            "name": "registry-config",
            "config_map": {"name": f"${{kubernetes_config_map.{app_name}.metadata[0].name}}"},
        })
    for s in app.storage:
        if s.host_path is not None:
            volumes.append({"name": s.name, "host_path": {"path": s.host_path}})
        elif s.existing_pvc is not None:
            volumes.append({"name": s.name, "persistent_volume_claim": {"claim_name": s.existing_pvc}})
        elif s.get_or_create and s.size is not None:
            pvc_resource_name = s.pvc_name or f"{app_name}-pvc"
            pvc_tf_name = pvc_resource_name.replace("-", "_")
            volumes.append({
                "name": s.name,
                "persistent_volume_claim": {
                    "claim_name": f"${{kubernetes_persistent_volume_claim.{pvc_tf_name}.metadata[0].name}}"
                },
            })
        elif s.size is not None and include_fresh_pvc:
            pvc_tf_name = f"{s.name}_pvc"
            volumes.append({
                "name": s.name,
                "persistent_volume_claim": {
                    "claim_name": f"${{kubernetes_persistent_volume_claim.{pvc_tf_name}.metadata[0].name}}"
                },
            })
    return volumes


def build_pvcs(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Real `kubernetes_persistent_volume_claim` resources - ported from
    `generate_pvcs`.

    Faithfully preserves a real quirk of the old generator: for a
    StatefulSet app with a non-get_or_create, size-set storage entry, the
    old code emits BOTH a standalone PVC resource here (get_or_create=False
    branch) AND a `volume_claim_template` in `generate_statefulset` for the
    very same entry - a real, currently-deployed redundancy (the standalone
    PVC is not actually referenced by the StatefulSet's own pods, which get
    their per-replica PVCs from the volume_claim_template instead). Since
    golden-file diffing needs to match currently-deployed Terraform state,
    this is intentionally kept rather than "fixed" here.
    """
    resources: dict[str, Any] = {}
    for s in app.storage:
        if s.size is None or s.existing_pvc is not None:
            continue
        if s.get_or_create:
            pvc_resource_name = s.pvc_name or f"{app_name}-pvc"
            pvc_tf_name = pvc_resource_name.replace("-", "_")
        else:
            pvc_tf_name = f"{s.name}_pvc"
            pvc_resource_name = f"{app_name}-{s.name}"
        resources[pvc_tf_name] = {
            "metadata": {
                "name": pvc_resource_name,
                "namespace": namespace_ref(app.namespace),
                "labels": {"app": app_name, "storage": s.name, "managed-by": "cube_manifest"},
            },
            "spec": {
                "access_modes": [s.access_mode],
                "storage_class_name": s.storage_class or "local-path",
                "resources": {"requests": {"storage": s.size}},
            },
            "wait_until_bound": False,
            "lifecycle": {"prevent_destroy": s.prevent_destroy},
        }
    if not resources:
        return {}
    return {"resource": {"kubernetes_persistent_volume_claim": resources}}
