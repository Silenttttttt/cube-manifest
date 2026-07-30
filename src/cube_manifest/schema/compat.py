"""Normalizes every legacy/competing app.yml shape from the old app-generator
system into the one canonical shape schema.models.AppConfig expects.

This is the ONLY place that ever needs to know the old shapes existed. Every
normalizer here is a small, named, independently-testable function, tried in
the same precedence order the old terraform_generator.py actually used (so
existing apps keep behaving identically) with a visible DeprecationWarning
logged whenever a non-canonical shape is used - never a silent behavior fork.

Called by loader.py on the raw dict BEFORE constructing an AppConfig; nothing
outside this module (and loader.py) should ever look for `health_checks`,
`security`, `docker_config.user_config` as a security-context source,
`node_port` nested under `service`, `volumes` as a list, a dict-shaped or
`env`-merged `environment`, or a list-shaped `secrets`.
"""

from __future__ import annotations

import warnings
from typing import Any


def _warn(old_shape: str, canonical: str) -> None:
    warnings.warn(
        f"app.yml uses the legacy {old_shape!r} shape - normalizing to the canonical "
        f"{canonical!r} shape. This still works but is deprecated; migrate the app.yml "
        f"to the canonical shape to silence this warning.",
        DeprecationWarning,
        stacklevel=3,
    )


def normalize_app_type(raw: dict[str, Any]) -> None:
    """The old system's docs/examples universally used app_type: "deployment",
    but the actual enforced enum never included it (a real, confirmed doc/code
    mismatch) - accept it here as an alias for "service" rather than fixing
    the docs and breaking nothing that already relied on the literal bug."""
    if raw.get("app_type") == "deployment":
        _warn("app_type: deployment", "app_type: service")
        raw["app_type"] = "service"


def normalize_node_preference(raw: dict[str, Any]) -> None:
    """Real precedent from terraform_generator.py::_validate_scheduling_config:
    'flexible' is not a valid node_preference preset (valid_presets =
    {'critical', 'indifferent', 'workload'}) but the old code doesn't reject
    it - it prints an ERROR and auto-corrects to 'indifferent' rather than
    failing the deploy. paper-trader/app.yml's history shows exactly this
    correction already applied by hand once; normalize it the same way here
    so a stray 'flexible' elsewhere doesn't hard-fail."""
    scheduling = raw.get("scheduling")
    if isinstance(scheduling, dict) and scheduling.get("node_preference") == "flexible":
        _warn("scheduling.node_preference: flexible", "scheduling.node_preference: indifferent")
        scheduling["node_preference"] = "indifferent"


def _probe_from_exec_shape(exec_block: dict, defaults: dict) -> dict:
    return {
        "command": list(exec_block["command"]),
        "initial_delay_seconds": defaults.get("initial_delay_seconds", 5),
        "period_seconds": defaults.get("period_seconds", 10),
        "timeout_seconds": defaults.get("timeout_seconds", 3),
        "failure_threshold": defaults.get("failure_threshold", 3),
    }


def normalize_health_check(raw: dict[str, Any]) -> None:
    """Real precedence order from terraform_generator.py::_build_health_checks:
    docker_config.health_check first, then health_check (singular), then
    health_checks (plural) - first match wins, matching current behavior.

    docker_config.health_check is READ here (never removed) - it's a
    dual-purpose, genuinely different Docker-native HEALTHCHECK-instruction
    shape (see models.DockerHealthCheck), so DockerConfig still needs to see
    it untouched afterwards."""
    docker_config = raw.get("docker_config") or {}

    if "health_check" in docker_config:
        _warn("docker_config.health_check", "health_check")
        # Real "first match wins" precedence (terraform_generator.py::
        # _build_health_checks): docker_config.health_check entirely
        # supersedes a co-present health_checks (plural) block - several
        # real apps (activator, jobber, paipai, paipai-ui, paper-trader,
        # pod-rebalancer, service-discovery) set both, with health_checks
        # silently dead/overridden. Drop it here rather than leaving it
        # behind to trip extra="forbid".
        raw.pop("health_checks", None)
        hc = docker_config["health_check"]
        if hc.get("enabled", True):
            command = hc.get("command", "exit 0")
            if isinstance(command, str):
                command = command.split() if not any(c in command for c in ("||", ";", '"')) else ["/bin/sh", "-c", command]
            raw["health_check"] = {
                "enabled": True,
                "liveness": {
                    "command": list(command),
                    "initial_delay_seconds": 30,
                    "period_seconds": 30,
                    "timeout_seconds": 5,
                    "failure_threshold": 3,
                },
                "readiness": {
                    "command": list(command),
                    "initial_delay_seconds": 5,
                    "period_seconds": 10,
                    "timeout_seconds": 5,
                    "failure_threshold": 3,
                },
            }
        else:
            raw["health_check"] = {"enabled": False}
        return

    existing = raw.get("health_check")
    if existing is not None and ("readiness_probe" in existing or "liveness_probe" in existing):
        _warn("health_check.{readiness,liveness}_probe", "health_check.{readiness,liveness}")
        raw.pop("health_checks", None)  # same "first match wins" precedence as above
        hc = existing
        normalized: dict[str, Any] = {"enabled": hc.get("enabled", True)}
        if "readiness_probe" in hc and "exec" in hc["readiness_probe"]:
            r = hc["readiness_probe"]
            normalized["readiness"] = _probe_from_exec_shape(
                r["exec"], {"initial_delay_seconds": r.get("initial_delay_seconds", 10), "period_seconds": r.get("period_seconds", 5), "timeout_seconds": 5}
            )
        if "liveness_probe" in hc and "exec" in hc["liveness_probe"]:
            lv = hc["liveness_probe"]
            normalized["liveness"] = _probe_from_exec_shape(
                lv["exec"], {"initial_delay_seconds": lv.get("initial_delay_seconds", 30), "period_seconds": lv.get("period_seconds", 10), "timeout_seconds": 5}
            )
        raw["health_check"] = normalized
        return

    if "health_checks" in raw:
        _warn("health_checks.{liveness,readiness}", "health_check.{liveness,readiness}")
        hcs = raw.pop("health_checks")
        normalized = {"enabled": True}
        if "liveness" in hcs:
            lv = hcs["liveness"]
            normalized["liveness"] = {
                "command": list(lv["command"]),
                "initial_delay_seconds": lv.get("initial_delay", 30),
                "period_seconds": lv.get("period", 30),
                "timeout_seconds": 5,
                "failure_threshold": 3,
            }
        if "readiness" in hcs:
            r = hcs["readiness"]
            normalized["readiness"] = {
                "command": list(r["command"]),
                "initial_delay_seconds": r.get("initial_delay", 5),
                "period_seconds": r.get("period", 10),
                "timeout_seconds": 5,
                "failure_threshold": 3,
            }
        raw["health_check"] = normalized


def normalize_security_context(raw: dict[str, Any]) -> None:
    """Real precedence order from terraform_generator.py::_build_security_context:
    security_context first, then security, then docker_config.user_config.

    The legacy `security:` block also carries fs_group/fs_group_change_policy
    (read directly by _build_pod_config for the pod-level securityContext,
    a separate code path from _build_security_context's container-level one)
    - both are carried over into the canonical security_context so nothing
    the user declared is silently dropped, even though the two old code paths
    only ever read a subset of it between them."""
    if "security_context" in raw:
        return  # already canonical shape (superset - extra keys just pass through)

    if "security" in raw:
        _warn("security", "security_context")
        sec = raw.pop("security")
        normalized: dict[str, Any] = {}
        for key in ("run_as_user", "run_as_group", "fs_group", "fs_group_change_policy"):
            if key in sec:
                normalized[key] = sec[key]
        raw["security_context"] = normalized
        return

    if "docker_config" not in raw:
        return

    # Real precedent from terraform_generator.py::_build_security_context's
    # third branch: `elif 'docker_config' in config:` triggers on the mere
    # PRESENCE of a docker_config block, independent of whether it declares
    # its own nested `user_config` sub-key - `config['docker_config'].get(
    # 'user_config', {})` defaults to `{}` when absent, and `{}.get(
    # 'create_user', True)` still resolves True, still defaulting `uid` to
    # 1001. A docker_config block with no user_config at all (e.g.
    # local-storage/app.yml, external_repo/Dockerfile-built apps in general)
    # is real, currently-live data relying on exactly this implicit default
    # (confirmed: local-storage's real Deployment has runAsUser: 1001 with
    # no explicit security/security_context/user_config anywhere in its
    # app.yml) - checking `"user_config" in docker_config` here missed this
    # case entirely, silently dropping runAsUser for every such app.
    docker_config = raw["docker_config"] or {}
    user_config = docker_config.get("user_config") or {}
    if user_config.get("create_user", True):
        _warn("docker_config.user_config", "security_context")
        raw["security_context"] = {"run_as_user": user_config.get("uid", 1001)}


def normalize_node_port(raw: dict[str, Any]) -> None:
    """Real dual/triple shape confirmed live in production apps (register_vps_route.py's
    _extract_node_port): top-level node_port, service.node_port, or
    service.ports[].node_port. Precedence matches that function exactly.

    Unlike the other normalizers here, this one only ever COPIES a value up
    to the canonical top-level node_port - it never removes the source key,
    since `service` itself stays in the raw dict for ServiceSpec to also
    parse (see normalize(), which used to drop the whole `service` block)."""
    if "node_port" in raw:
        return
    service = raw.get("service") or {}
    if "node_port" in service:
        _warn("service.node_port", "node_port")
        raw["node_port"] = service["node_port"]
        return
    for port_entry in service.get("ports", []):
        if "node_port" in port_entry:
            _warn("service.ports[].node_port", "node_port")
            raw["node_port"] = port_entry["node_port"]
            return


def normalize_storage(raw: dict[str, Any]) -> None:
    """Merges the legacy volumes[] (hostPath-only) list into storage[] (the
    new unified StorageEntry shape covers both PVC and hostPath backing).
    Matches the old code exactly: entries with type other than "hostPath"
    (e.g. redis/app.yml's persistentVolumeClaim/configMap-typed volumes[])
    are silently dropped here too - terraform_generator.py's own top-level
    `volumes:` handling (_build_volumes) only ever matches `vol['type'] ==
    'hostPath'` as well, so those other entries were already dead data,
    not something this normalizer is newly discarding."""
    volumes = raw.pop("volumes", None)
    if not volumes:
        return
    _warn("volumes[]", "storage[] (with host_path set)")
    storage = raw.setdefault("storage", [])
    for vol in volumes:
        if vol.get("type") != "hostPath":
            continue  # the old code only ever handled hostPath here too
        storage.append(
            {
                "name": vol["name"],
                "mount_path": vol["mount_path"],
                "host_path": vol["host_path"],
                "read_only": vol.get("read_only", False),
            }
        )


def normalize_environment(raw: dict[str, Any]) -> None:
    """Unifies THREE environment-adjacent shapes into one canonical
    `environment: list[EnvVar]`:

      1. `environment` as a dict ({KEY: "value"}) - postgres/rabbitmq's shape.
      2. `environment` as a list of {name,value} / {name,value_from:{...}} -
         the canonical EnvVar shape already.
      3. A SEPARATE top-level `env: [{name,value}]` field (docker-registry's
         shape) - not the same key as `environment` at all.

    Real merge order: env entries are appended AFTER whatever `environment`
    already resolved to (env is genuinely dead in terraform_generator.py
    today - no generator function reads config['env'] at all, only
    container_config['env'], a different nested path - but a future
    generator consuming it should see it layered after `environment`, the
    same "more specific overrides/extends the general list" precedent used
    everywhere else in this codebase)."""
    environment = raw.get("environment")
    merged: list[Any] = []

    if isinstance(environment, dict):
        _warn("environment (dict of KEY: value)", "environment (list of {name, value})")
        merged.extend({"name": k, "value": str(v)} for k, v in environment.items())
    elif isinstance(environment, list):
        merged.extend(environment)

    env = raw.pop("env", None)
    if env:
        _warn("env[] (separate top-level field)", "environment (env entries appended after existing environment entries)")
        merged.extend(env)

    if merged or environment is not None or env:
        raw["environment"] = merged


def normalize_secrets(raw: dict[str, Any]) -> None:
    """Unifies the two real `secrets:` shapes into one canonical dict:

      1. `secrets: {KEY: "value"}` - the far more common shape.
      2. `secrets: [{name: ..., value: ...}]` - a list shape used by
         local-storage, shy-worm-*, voice-cloning, whatsapp-clone.
    """
    secrets = raw.get("secrets")
    if isinstance(secrets, list):
        _warn("secrets[] (list of {name, value})", "secrets (dict of KEY: value)")
        normalized: dict[str, str] = {}
        for entry in secrets:
            normalized[entry["name"]] = entry.get("value", "")
        raw["secrets"] = normalized


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply every normalizer, in dependency order, to a raw app.yml dict.
    Returns the same dict, mutated in place, ready for AppConfig(**raw)."""
    normalize_app_type(raw)
    normalize_node_preference(raw)
    normalize_health_check(raw)
    normalize_security_context(raw)
    normalize_node_port(raw)
    normalize_storage(raw)
    normalize_environment(raw)
    normalize_secrets(raw)
    # NOTE: `service` is deliberately NOT dropped here (unlike earlier
    # versions of this normalizer) - schema.models.ServiceSpec now models
    # the rest of that shape (.type, .ports[].name/.port/.target_port/
    # .protocol) properly, since terraform_generator.py's generate_service()
    # genuinely reads it (service_config wins over the legacy service_type/
    # port/node_port fields whenever present) and later golden-file
    # Terraform-diff testing needs that fidelity.
    #
    # `docker_config.health_check` is also deliberately NOT popped - it's a
    # real DockerConfig field (models.DockerHealthCheck), read but not
    # consumed-and-discarded above.
    return raw
