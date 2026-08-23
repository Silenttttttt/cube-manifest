"""Deployment, StatefulSet, DaemonSet, and Job dict-builders.

Deployment/StatefulSet/DaemonSet are faithful ports of `generate_deployment`/
`generate_statefulset`/`generate_daemonset` (and their shared `_build_*`
helpers). `app_type: job` was a confirmed STUB in the old system
(`generate_job_terraform` emitted nothing but a comment) - `build_job` here
is a real, from-scratch implementation built from the same shared
container-spec/scheduling/init-container helpers as the other three, since
there was no old behavior to preserve for it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cube_manifest.schema.models import AppConfig, EnvVar, InitContainer, SchedulingConfig, Toleration

from . import config as config_gen
from . import health_security, scaling, storage
from ._common import (
    default_pull_policy,
    effective_replicas,
    get_image_reference,
    namespace_ref,
    resolve_pull_policy,
)

# Global scheduling presets, applied when `scheduling.node_preference` is set
# and not overridden by an explicit, non-empty value for the same field -
# ported from `_apply_scheduling_presets`/`_smart_merge_scheduling`'s net
# effect (an explicit non-empty list/value always wins outright over the
# whole preset; an absent/empty one falls back to the preset's value as a
# whole, rather than merging item-by-item).
_NODE_PREFERENCE_PRESETS: dict[str, dict[str, Any]] = {
    "critical": {
        "priority_class": "critical-workload",
        "required": [{"key": "node-type", "operator": "In", "values": ["server"]}],
        "preferred": [],
        "anti_affinity": None,
        "tolerations": [{"key": "node-role.kubernetes.io/critical", "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
    },
    "indifferent": {
        "priority_class": "flexible-workload",
        "required": [],
        "preferred": [
            {"weight": 70, "key": "node-type", "operator": "In", "values": ["server"]},
            {"weight": 30, "key": "node-type", "operator": "In", "values": ["worker"]},
        ],
        "anti_affinity": None,
        "tolerations": [{"key": "node-role.kubernetes.io/critical", "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
    },
    "workload": {
        "priority_class": "batch-workload",
        "required": [],
        "preferred": [
            {"weight": 70, "key": "node-type", "operator": "In", "values": ["worker"]},
            {"weight": 30, "key": "node-type", "operator": "In", "values": ["server"]},
        ],
        "anti_affinity": "soft",
        "tolerations": [{"key": "node-role.kubernetes.io/critical", "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
    },
}


def _build_toleration(t: Toleration) -> dict[str, Any]:
    body: dict[str, Any] = {"key": t.key or "", "operator": t.operator or "Equal", "effect": t.effect or "NoSchedule"}
    if t.value:
        body["value"] = t.value
    return body


def _build_affinity_block(required: list[dict[str, Any]], preferred: list[dict[str, Any]], anti: str | None, app_name: str) -> dict[str, Any]:
    """Ported from `_build_affinity_config`."""
    if not required and not preferred and not anti:
        return {}

    block: dict[str, Any] = {}
    node_affinity: dict[str, Any] = {}
    if required:
        node_affinity["required_during_scheduling_ignored_during_execution"] = {
            "node_selector_term": [
                {"match_expressions": {"key": r["key"], "operator": r["operator"], "values": list(r["values"])}}
                for r in required
            ]
        }
    if preferred:
        node_affinity["preferred_during_scheduling_ignored_during_execution"] = [
            {
                "weight": p.get("weight", 100),
                "preference": {"match_expressions": {"key": p["key"], "operator": p["operator"], "values": list(p["values"])}},
            }
            for p in preferred
        ]
    if node_affinity:
        block["node_affinity"] = node_affinity

    if anti == "soft":
        block["pod_anti_affinity"] = {
            "preferred_during_scheduling_ignored_during_execution": {
                "weight": 50,
                "pod_affinity_term": {
                    "label_selector": {"match_expressions": {"key": "app", "operator": "In", "values": [app_name]}},
                    "topology_key": "kubernetes.io/hostname",
                },
            }
        }
    elif anti == "hard":
        block["pod_anti_affinity"] = {
            "required_during_scheduling_ignored_during_execution": {
                "label_selector": {"match_expressions": {"key": "app", "operator": "In", "values": [app_name]}},
                "topology_key": "kubernetes.io/hostname",
            }
        }
    return block


def _build_scheduling(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `_build_scheduling_config`/`_apply_scheduling_presets`/
    `_smart_merge_scheduling`, simplified to a direct field-by-field
    explicit-wins-else-preset resolution (rather than the old code's
    recursive dict merge) - the net result is the same for every concrete
    field that matters (priority_class, node affinity required/preferred,
    pod anti-affinity, tolerations)."""
    sched: SchedulingConfig = app.scheduling
    preset = _NODE_PREFERENCE_PRESETS.get(sched.node_preference)

    priority_class = sched.priority_class or (preset["priority_class"] if preset else None)

    explicit_required: list[dict[str, Any]] = []
    explicit_preferred: list[dict[str, Any]] = []
    explicit_anti: str | None = None

    # Two independent real shapes coexist in the schema (see models.py):
    # scheduling.affinity.node_affinity.{required,preferred} and the bare
    # scheduling.node_affinity.{required,preferred} (rabbitmq/postgres'
    # shape) - neither is unified into the other, matching real data.
    if sched.affinity is not None and sched.affinity.node_affinity is not None:
        na = sched.affinity.node_affinity
        explicit_required = [{"key": t.key, "operator": t.operator, "values": list(t.values)} for t in na.required]
        explicit_preferred = [{"weight": t.weight, "key": t.key, "operator": t.operator, "values": list(t.values)} for t in na.preferred]
    if sched.node_affinity is not None:
        if not explicit_required:
            explicit_required = [{"key": t.key, "operator": t.operator, "values": list(t.values)} for t in sched.node_affinity.required]
        if not explicit_preferred:
            explicit_preferred = [{"weight": t.weight, "key": t.key, "operator": t.operator, "values": list(t.values)} for t in sched.node_affinity.preferred]

    if sched.anti_affinity is not None and sched.anti_affinity.enabled:
        explicit_anti = sched.anti_affinity.type or "soft"

    required = explicit_required or (preset["required"] if preset else [])
    preferred = explicit_preferred or (preset["preferred"] if preset else [])
    anti = explicit_anti if explicit_anti is not None else (preset["anti_affinity"] if preset else None)

    explicit_tolerations = [_build_toleration(t) for t in sched.tolerations]
    tolerations = explicit_tolerations or (preset["tolerations"] if preset else [])

    result: dict[str, Any] = {}
    if priority_class:
        result["priority_class_name"] = priority_class
    affinity_block = _build_affinity_block(required, preferred, anti, app_name)
    if affinity_block:
        result["affinity"] = affinity_block
    if tolerations:
        result["toleration"] = tolerations
    return result


def _rebalancing_annotations(app: AppConfig) -> dict[str, str]:
    """Ported from `_build_rebalancing_annotations`."""
    reb = app.scheduling.rebalancing
    if reb is None or not reb.enabled:
        return {}
    return {
        "rebalancing.cubernetes.io/enabled": "true",
        "rebalancing.cubernetes.io/strategy": reb.strategy or "gradual",
        "rebalancing.cubernetes.io/trigger": reb.trigger or "node_availability",
        "rebalancing.cubernetes.io/min-age-minutes": str(reb.min_age_minutes if reb.min_age_minutes is not None else 5),
        "rebalancing.cubernetes.io/cooldown-minutes": str(reb.cooldown_minutes if reb.cooldown_minutes is not None else 10),
    }


def _policy_hash(image_ref: str, pull_policy: str) -> str:
    return hashlib.md5(f"{image_ref}:{pull_policy}".encode()).hexdigest()[:8]


def _build_strategy(app: AppConfig) -> dict[str, Any]:
    """Ported from `_build_deployment_strategy` for the `RollingUpdate` case.

    `type: Recreate` now actually emits `{"type": "Recreate"}` instead of the
    old generator's silently-preserved no-op gap (confirmed nothing in the
    26 pre-existing app.yml files ever set `deployment_strategy.type:
    Recreate` - grepped for real - so fixing this changes no currently
    deployed app's behavior). This was a real, not just theoretical, gap:
    a single-replica Deployment requesting the cluster's only
    `nvidia.com/gpu` unit deadlocks under the default/RollingUpdate
    behavior (Kubernetes' own default surge tries to schedule a second,
    new pod ALONGSIDE the still-running old one before tearing it down -
    impossible with only one GPU in the whole cluster to go around, so the
    new pod sits Pending forever and the rollout times out) - confirmed by
    reproducing it live against meeting-transcriber. `Recreate` (old pod
    fully terminated, freeing the GPU, before the new one is even created)
    is the correct, standard fix for exactly this "scarce, non-shareable
    resource" shape, and app.yml needs a way to actually request it."""
    ds = app.deployment_strategy
    if ds is None:
        return {}
    if ds.type == "Recreate":
        return {"type": "Recreate"}
    if ds.type != "RollingUpdate":
        return {}
    ru = ds.rolling_update
    max_unavailable = ru.max_unavailable if ru is not None else 1
    max_surge = ru.max_surge if ru is not None else 1
    return {"type": "RollingUpdate", "rolling_update": {"max_unavailable": str(max_unavailable), "max_surge": str(max_surge)}}


def _build_pod_extras(app: AppConfig) -> dict[str, Any]:
    """host_network + pod-level security_context (fs_group/*) - ported from
    `_build_pod_config` (its third branch, `privileged`, is confirmed dead:
    no `privileged` field exists anywhere in the schema, matching that it
    was never a real, modeled field in the old system either - just a
    comment with no output)."""
    extra: dict[str, Any] = {}
    if app.host_network:
        extra["host_network"] = True
    pod_sc = health_security.build_pod_security_context(app.security_context)
    if pod_sc:
        extra["security_context"] = pod_sc
    # infrastructure_config.runtime_class_name previously only ever reached
    # a pod spec via build_daemonset (nvidia-device-plugin's own real use).
    # A regular Deployment/StatefulSet/Job requesting a GPU resource needs
    # this too - confirmed live against this cluster: a `RuntimeClass
    # nvidia` object exists separately from the default runc runtime
    # (`kubectl get runtimeclass`), so a pod that only declares
    # `resources.limits: {gpu: "1"}` gets scheduled onto the GPU node and
    # satisfies the resource accounting, but never actually gets a working
    # NVIDIA device inside the container unless it also opts into this
    # RuntimeClass explicitly. Reusing the existing infrastructure_config
    # field (rather than adding a new one) matches how the schema already
    # models this exact concept for DaemonSets.
    if app.infrastructure_config.runtime_class_name:
        extra["runtime_class_name"] = app.infrastructure_config.runtime_class_name
    return extra


def _build_init_containers(app: AppConfig) -> list[dict[str, Any]]:
    """Ported from `_build_init_containers`."""
    result: list[dict[str, Any]] = []
    for ic in app.init_containers:
        body: dict[str, Any] = {"name": ic.name, "image": ic.image}
        if ic.command:
            body["command"] = list(ic.command)
        if ic.args:
            body["args"] = list(ic.args)
        env = _init_container_env(ic)
        if env:
            body["env"] = env
        if ic.volume_mounts:
            body["volume_mount"] = [{"name": m.name, "mount_path": m.mount_path, "read_only": m.read_only} for m in ic.volume_mounts]
        if ic.security_context is not None:
            sc = health_security.build_container_security_context(ic.security_context)
            if sc:
                body["security_context"] = sc
        result.append(body)
    return result


def _init_container_env(ic: InitContainer) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = []
    for e in ic.env:
        if e.value is not None:
            env.append({"name": e.name, "value": e.value})
        elif e.value_from is not None:
            ref = e.value_from.secret_key_ref
            env.append({"name": e.name, "value_from": {"secret_key_ref": {"name": ref.name, "key": ref.key}}})
    return env


def _build_container_spec(app: AppConfig, app_name: str, image_ref: str, image_pull_policy: str) -> dict[str, Any]:
    resources = app.resources
    container: dict[str, Any] = {
        "name": app_name,
        "image": image_ref,
        "image_pull_policy": image_pull_policy,
    }
    env = config_gen.build_env_vars(app, app_name)
    if env:
        container["env"] = env
    mounts = storage.build_volume_mounts(app)
    if mounts:
        container["volume_mount"] = mounts
    limits: dict[str, Any] = {"cpu": resources.limits.cpu, "memory": resources.limits.memory}
    requests: dict[str, Any] = {"cpu": resources.requests.cpu, "memory": resources.requests.memory}
    # Extended resource (nvidia.com/gpu) - only emitted when app.yml actually
    # sets it. k8s treats requests as defaulting to the limit for extended
    # resources, so most real app.yml files will only ever set this under
    # `limits`, but requests.gpu is honored too if set explicitly.
    if resources.limits.gpu:
        limits["nvidia.com/gpu"] = resources.limits.gpu
    if resources.requests.gpu:
        requests["nvidia.com/gpu"] = resources.requests.gpu
    container["resources"] = {"limits": limits, "requests": requests}
    container.update(health_security.build_probes(app.health_check))
    sc = health_security.build_container_security_context(app.security_context)
    if sc:
        container["security_context"] = sc
    return container


def build_deployment(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_deployment`."""
    if not app.enabled:
        return {}

    image_ref = get_image_reference(app_name, app)
    image_pull_policy = resolve_pull_policy(app, default_pull_policy(image_ref))
    container = _build_container_spec(app, app_name, image_ref, image_pull_policy)

    pod_spec: dict[str, Any] = {
        "container": [container],
        "restart_policy": "Always",
        "termination_grace_period_seconds": 30,
    }
    volumes = storage.build_volumes(app, app_name, include_fresh_pvc=True)
    if volumes:
        pod_spec["volume"] = volumes
    if app.rbac.enabled:
        pod_spec["service_account_name"] = f"${{kubernetes_service_account.{app_name}_service_account.metadata[0].name}}"
    pod_spec.update(_build_pod_extras(app))
    pod_spec.update(_build_scheduling(app, app_name))
    init_containers = _build_init_containers(app)
    if init_containers:
        pod_spec["init_container"] = init_containers

    activator_extra = scaling.activator_deployment_extras(app)

    labels = {"app": app_name, **activator_extra["labels"]}
    annotations: dict[str, str] = {}
    annotations.update(_rebalancing_annotations(app))
    annotations["deployment.kubernetes.io/image-policy-hash"] = _policy_hash(image_ref, image_pull_policy)
    annotations.update(activator_extra["annotations"])

    spec: dict[str, Any] = {
        "replicas": effective_replicas(app),
        "selector": {"match_labels": {"app": app_name}},
        "template": {"metadata": {"labels": {"app": app_name}}, "spec": pod_spec},
    }
    strategy = _build_strategy(app)
    if strategy:
        spec["strategy"] = strategy

    body = {
        "metadata": {
            "name": app_name,
            "namespace": namespace_ref(app.namespace),
            "labels": labels,
            "annotations": annotations,
        },
        "spec": spec,
        # "update"/"create" matched to Kubernetes' own real default here, not
        # picked arbitrarily: `progressDeadlineSeconds` defaults to 600s (10m)
        # when a Deployment doesn't set it explicitly (which this generator
        # never does) - meaning k8s itself is willing to keep waiting on a
        # slow-but-legitimate rollout for twice as long as this timeout used
        # to allow. Real incident: chat-server's CI deploy step failed on
        # every push with "Error: Waiting for rollout to start" while the
        # rollout was genuinely still progressing (observed image pulls alone
        # taking 60-100+s) - terraform gave up at 5m, right around where a
        # slower pull or real load would still be within k8s's own 10m
        # patience. Bumping to 10m only makes an apply more patient, never
        # less correct - a genuinely broken/crash-looping rollout still fails
        # via kubectl's own progress-deadline-exceeded condition either way.
        "timeouts": {"create": "10m", "update": "10m", "delete": "2m"},
        "lifecycle": {
            "create_before_destroy": True,
            "ignore_changes": [
                'metadata[0].annotations["kubectl.kubernetes.io/last-applied-configuration"]',
                'metadata[0].annotations["deployment.kubernetes.io/revision"]',
                *activator_extra["ignore_changes"],
            ],
        },
    }
    return {"resource": {"kubernetes_deployment": {app_name: body}}}


def build_statefulset(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_statefulset`."""
    if not app.enabled:
        return {}

    image_ref = get_image_reference(app_name, app)
    image_pull_policy = resolve_pull_policy(app, default_pull_policy(image_ref))
    container = _build_container_spec(app, app_name, image_ref, image_pull_policy)

    pod_spec: dict[str, Any] = {
        "container": [container],
        "restart_policy": app.restart_policy or "Always",
        "termination_grace_period_seconds": app.termination_grace_period if app.termination_grace_period is not None else 30,
    }
    volumes = storage.build_volumes(app, app_name, include_fresh_pvc=False)
    if volumes:
        pod_spec["volume"] = volumes
    if app.rbac.enabled:
        pod_spec["service_account_name"] = f"${{kubernetes_service_account.{app_name}_service_account.metadata[0].name}}"
    pod_spec.update(_build_pod_extras(app))
    pod_spec.update(_build_scheduling(app, app_name))
    init_containers = _build_init_containers(app)
    if init_containers:
        pod_spec["init_container"] = init_containers

    volume_claim_templates = []
    for s in app.storage:
        if s.size is not None and not s.get_or_create and s.existing_pvc is None:
            volume_claim_templates.append({
                "metadata": {
                    "name": s.name,
                    "namespace": namespace_ref(app.namespace),
                    "labels": {"app": app_name, "storage": s.name, "managed-by": "cube_manifest"},
                },
                "spec": {
                    "access_modes": [s.access_mode],
                    "storage_class_name": s.storage_class or "local-path",
                    "resources": {"requests": {"storage": s.size}},
                },
            })

    spec: dict[str, Any] = {
        "service_name": f"{app_name}-headless",
        "replicas": effective_replicas(app),
        "selector": {"match_labels": {"app": app_name}},
        "template": {"metadata": {"labels": {"app": app_name}}, "spec": pod_spec},
    }
    if volume_claim_templates:
        spec["volume_claim_template"] = volume_claim_templates

    body = {
        "metadata": {
            "name": app_name,
            "namespace": namespace_ref(app.namespace),
            "labels": {"app": app_name},
            "annotations": _rebalancing_annotations(app),
        },
        "spec": spec,
        "wait_for_rollout": False,
        "lifecycle": {"ignore_changes": ["spec[0].template[0].spec[0].container[0].image"]},
    }
    return {"resource": {"kubernetes_stateful_set": {app_name: body}}}


def _container_config_env(entries: list[EnvVar]) -> list[dict[str, Any]]:
    """Ported from `_build_container_env_vars` - narrower than the general
    env-var builder on purpose (matches old behavior: no value_from support
    for container_config.env)."""
    return [{"name": e.name, "value": e.value or ""} for e in entries]


def build_daemonset(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_daemonset`.

    DaemonSet always references a ServiceAccount (`service_account_name` is
    unconditional here, unlike Deployment/StatefulSet/Job where it's gated
    on `rbac.enabled`) - a real hard dependency in the old generator too
    (every real DaemonSet app, e.g. nvidia-device-plugin, sets
    `rbac.enabled: true`); this is preserved rather than "fixed" since
    golden-file diffing needs to match currently-deployed state.
    """
    if not app.enabled:
        return {}

    cc = app.container_config
    image_ref = cc.image if cc is not None else f"local/{app_name}:latest"
    image_pull_policy = cc.image_pull_policy if cc is not None else "IfNotPresent"

    container: dict[str, Any] = {"name": app_name, "image": image_ref, "image_pull_policy": image_pull_policy}

    env = config_gen.build_env_vars(app, app_name)
    if cc is not None and cc.env:
        env = env + _container_config_env(cc.env)
    if env:
        container["env"] = env

    if cc is not None and cc.ports:
        container["port"] = [{"name": p.name, "container_port": p.container_port, "protocol": p.protocol} for p in cc.ports]

    mounts = storage.build_volume_mounts(app)
    infra = app.infrastructure_config
    mounts = mounts + [{"name": m.name, "mount_path": m.mount_path, "read_only": m.read_only} for m in infra.volume_mounts]
    if mounts:
        container["volume_mount"] = mounts

    container["resources"] = {
        "requests": {"cpu": app.resources.requests.cpu, "memory": app.resources.requests.memory},
        "limits": {"cpu": app.resources.limits.cpu, "memory": app.resources.limits.memory},
    }

    container.update(health_security.build_probes(app.health_check))
    sc = health_security.build_container_security_context(app.security_context)
    if infra.security_context is not None:
        sc = {**sc, **health_security.build_container_security_context(infra.security_context)}
    if sc:
        container["security_context"] = sc

    volumes = storage.build_volumes(app, app_name, include_fresh_pvc=True)
    volumes = volumes + [{"name": v.name, "host_path": {"path": v.host_path.path, "type": v.host_path.type}} for v in infra.volumes]

    pod_spec: dict[str, Any] = {
        "service_account_name": f"${{kubernetes_service_account.{app_name}_service_account.metadata[0].name}}",
        "container": [container],
    }
    pod_spec.update(_build_pod_extras(app))
    pod_spec.update(_build_scheduling(app, app_name))
    if infra.tolerations:
        # Real precedent from terraform_generator.py::generate_daemonset: the
        # node_preference-preset/scheduling.tolerations block (`{scheduling}`)
        # and infrastructure_config.tolerations (`{tolerations}`) are two
        # SEPARATE, ADDITIVE HCL `toleration {}` block groups emitted one
        # after the other in the same pod spec - never a replace. Confirmed
        # live: nvidia-device-plugin's real DaemonSet has BOTH its
        # infrastructure_config.tolerations entry (nvidia.com/gpu) AND the
        # `critical` node_preference preset's toleration
        # (node-role.kubernetes.io/critical) at once. Overwriting
        # pod_spec["toleration"] here (instead of appending to whatever
        # _build_scheduling already put there) silently dropped the preset's
        # toleration for every DaemonSet that also sets infra tolerations.
        pod_spec["toleration"] = pod_spec.get("toleration", []) + [_build_toleration(t) for t in infra.tolerations]
    if infra.node_selector:
        pod_spec["node_selector"] = dict(infra.node_selector)
    if infra.runtime_class_name:
        pod_spec["runtime_class_name"] = infra.runtime_class_name
    if volumes:
        pod_spec["volume"] = volumes

    namespace = namespace_ref(app.namespace)
    body = {
        "metadata": {
            "name": app_name,
            "namespace": namespace,
            "labels": {"app": app_name, "type": "infrastructure", "managed-by": "cube_manifest"},
            "annotations": {
                "cubernetes.io/description": app.description or "Infrastructure component",
                "cubernetes.io/managed-by": "cubernetes",
                **_rebalancing_annotations(app),
            },
        },
        "spec": {
            "selector": {"match_labels": {"app": app_name}},
            "template": {
                "metadata": {"labels": {"app": app_name, "type": "infrastructure", "managed-by": "cube_manifest"}},
                "spec": pod_spec,
            },
        },
        "lifecycle": {"create_before_destroy": True, "ignore_changes": ["metadata[0].annotations"]},
    }
    return {"resource": {"kubernetes_daemonset": {app_name: body}}}


def build_job(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Real implementation for `app_type: job` - the old generator's
    `generate_job_terraform` was a confirmed STUB emitting only a comment,
    with nothing to port; this reuses the same container-spec/scheduling/
    init-container/RBAC/volume helpers as the other three workload types for
    consistency, with the adaptations a real k8s Job needs:

    - `restart_policy` must be `OnFailure`/`Never` (k8s API constraint,
      unlike Deployment/StatefulSet's `Always`) - defaults to `OnFailure`
      unless app.yml's `restart_policy` is already one of the two valid
      values.
    - No liveness/readiness probes - a Job that never becomes "Ready" would
      just hang forever instead of ever completing, so probes (even if
      `health_check` is set) are deliberately omitted.
    - `active_deadline_seconds` from `microservice_config.max_execution_time`
      when present (the closest schema-modeled "how long can this run"
      signal, even though `microservice_config` is nominally a
      microservice-app_type field - a Job-shaped app declaring one is taken
      at face value).
    - `backoff_limit`: 3 retries if `microservice_config.retry_failed_jobs`
      is set, else 0 (fail once, don't auto-retry) - a reasonable default
      given the old system had no real Job semantics to match here.
    """
    if not app.enabled:
        return {}

    image_ref = get_image_reference(app_name, app)
    image_pull_policy = resolve_pull_policy(app, default_pull_policy(image_ref))
    container = _build_container_spec(app, app_name, image_ref, image_pull_policy)
    container.pop("liveness_probe", None)
    container.pop("readiness_probe", None)

    restart_policy = app.restart_policy if app.restart_policy in ("OnFailure", "Never") else "OnFailure"
    pod_spec: dict[str, Any] = {"container": [container], "restart_policy": restart_policy}

    volumes = storage.build_volumes(app, app_name, include_fresh_pvc=True)
    if volumes:
        pod_spec["volume"] = volumes
    if app.rbac.enabled:
        pod_spec["service_account_name"] = f"${{kubernetes_service_account.{app_name}_service_account.metadata[0].name}}"
    pod_spec.update(_build_pod_extras(app))
    pod_spec.update(_build_scheduling(app, app_name))
    init_containers = _build_init_containers(app)
    if init_containers:
        pod_spec["init_container"] = init_containers

    spec: dict[str, Any] = {"template": {"metadata": {"labels": {"app": app_name}}, "spec": pod_spec}}
    mc = app.microservice_config
    if mc is not None and mc.max_execution_time:
        spec["active_deadline_seconds"] = mc.max_execution_time
    spec["backoff_limit"] = 3 if (mc is not None and mc.retry_failed_jobs) else 0

    body = {
        "metadata": {
            "name": app_name,
            "namespace": namespace_ref(app.namespace),
            "labels": {"app": app_name, "type": "job", "managed-by": "cube_manifest"},
        },
        "spec": spec,
        "wait_for_completion": False,
        "lifecycle": {"ignore_changes": ["metadata[0].annotations"]},
    }
    return {"resource": {"kubernetes_job": {app_name: body}}}
