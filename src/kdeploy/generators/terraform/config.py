"""ConfigMap, Secret, and container environment-variable dict-builders.

This is the most safety-critical file in the package: every value here
(env var values, configmap data, secret data) was a prime f-string-injection
target in the old terraform_generator.py (secret/configmap values were
interpolated directly into HCL heredoc/quoted-string text with zero
escaping). Here every one of those values is instead assigned as a plain
Python dict value - a `"`, a `${...}`, a full fake `resource "..." {}` block,
or a literal newline in an app.yml string is just inert string content once
`json.dumps()` serializes the final document; it can never be parsed back out
as new HCL/JSON structure. json.dumps() has to escape backslashes/quotes for
this to be safe, and it always does (Python's stdlib json encoder), which is
exactly why building a plain dict instead of interpolating into text closes
this vulnerability class structurally rather than "by being careful in the
right places."
"""

from __future__ import annotations

from typing import Any

import yaml

from kdeploy.schema.models import AppConfig

from ._common import get_image_reference, namespace_ref

SENSITIVE_KEY_SUBSTRINGS = ("password", "key", "secret", "token")


def build_env_vars(app: AppConfig, app_name: str) -> list[dict[str, Any]]:
    """Container-spec `env` blocks - ported from `_build_environment_variables`.

    compat.py's `normalize_environment` has already unified the old system's
    three environment-adjacent shapes (dict-shaped `environment`, list-shaped
    `environment`, and the separate top-level `env[]` field) into one
    canonical `environment: list[EnvVar]`, so unlike the old code this only
    needs to walk that one list - no more dict-vs-list branching.
    """
    env: list[dict[str, Any]] = []

    for key, value in app.docker_config.environment_vars.items():
        env.append({"name": key, "value": str(value)})

    for e in app.environment:
        if e.value is not None:
            env.append({"name": e.name, "value": e.value})
        elif e.value_from is not None:
            ref = e.value_from.secret_key_ref
            env.append({"name": e.name, "value_from": {"secret_key_ref": {"name": ref.name, "key": ref.key}}})
        # else: neither value nor value_from set - matches the old code's
        # silent skip (no `else` branch at all in `_build_environment_variables`),
        # a real, if probably accidental, precedent (polymarket-collector's
        # MIN_LIQUIDITY_USD entry) rather than something to now start erroring on.

    if app.secrets_as_env_vars:
        for secret_name in app.secrets:
            env.append({
                "name": secret_name,
                "value_from": {"secret_key_ref": {"name": f"{app_name}-secret", "key": secret_name}},
            })

    return env


def _registry_config_to_yaml(app: AppConfig) -> str:
    assert app.registry_config is not None
    data = app.registry_config.model_dump(exclude_none=True)
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


def build_configmap(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_configmap` - same first-match-wins priority
    order: `registry_config` block > explicit `configmap:` dict >
    docker_config.environment_vars fallback (sensitive-looking keys
    filtered out, matching the old code's substring filter).

    JSON natively handles multi-line string values (as an escaped-newline
    JSON string) - the old code's heredoc-vs-single-line branching for
    multi-line configmap values is unnecessary here and is not reproduced.
    """
    if app.registry_config is not None:
        return {"resource": {"kubernetes_config_map": {app_name: {
            "metadata": {"name": f"{app_name}-config", "namespace": namespace_ref(app.namespace)},
            "data": {"config.yml": _registry_config_to_yaml(app)},
        }}}}

    if app.configmap:
        return {"resource": {"kubernetes_config_map": {f"{app_name}_config": {
            "metadata": {
                "name": f"{app_name}-config",
                "namespace": namespace_ref(app.namespace),
                "labels": {"app": app_name},
            },
            "data": dict(app.configmap),
        }}}}

    env_vars = app.docker_config.environment_vars
    safe_env_vars = {k: v for k, v in env_vars.items() if not any(s in k.lower() for s in SENSITIVE_KEY_SUBSTRINGS)}
    if not safe_env_vars:
        return {}
    return {"resource": {"kubernetes_config_map": {f"{app_name}_config": {
        "metadata": {
            "name": f"{app_name}-config",
            "namespace": namespace_ref(app.namespace),
            "labels": {"app": app_name},
        },
        "data": dict(safe_env_vars),
    }}}}


def build_secret(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_secret`. compat.py's `normalize_secrets` has
    already unified the old system's dict-shaped and list-shaped `secrets:`
    into one canonical `dict[str, str]`, so (unlike the old code) there's no
    isinstance branching needed here either."""
    if not app.secrets:
        return {}
    return {"resource": {"kubernetes_secret": {f"{app_name}_secret": {
        "metadata": {
            "name": f"{app_name}-secret",
            "namespace": namespace_ref(app.namespace),
            "labels": {"app": app_name, "managed-by": "kdeploy"},
        },
        "type": "Opaque",
        "wait_for_service_account_token": True,
        "data": dict(app.secrets),
        "lifecycle": {
            "ignore_changes": [
                'metadata[0].annotations["kubectl.kubernetes.io/last-applied-configuration"]',
                'metadata[0].annotations["deployment.kubernetes.io/revision"]',
                "metadata[0].resource_version",
                "metadata[0].uid",
            ],
            "prevent_destroy": True,
            "create_before_destroy": True,
        },
    }}}}


def build_microservice_secret(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_microservice_secret` - the sole Terraform
    resource for `app_type: microservice` (executed dynamically by jobber,
    never deployed as its own workload).

    Judgment call: the old code resolved resource_profile presets into
    concrete cpu/memory via a separate `AppConfigParser.get_resource_allocation`
    helper (not in this task's read-first list, and not modeled by the
    schema at all beyond the descriptive `resource_profile` field) - this
    uses `microservice_config.custom_resources` if set, else the app's own
    `resources`, as the concrete allocation instead. Every one of the 26 real
    apps that sets `resource_profile` is presumably relying on that preset
    table for the actual numbers, so this is a real, flagged fidelity gap a
    later golden-file diff would need to resolve (either by porting that
    parser helper's table into the schema/generator, or confirming
    `custom_resources`/`resources` already covers every real case).
    """
    mc = app.microservice_config
    assert mc is not None  # enforced by AppConfig's own validator for app_type: microservice

    resources = mc.custom_resources or app.resources
    image_ref = get_image_reference(app_name, app)

    data: dict[str, str] = {
        "image": image_ref,
        "resource_profile": mc.resource_profile.value,
        "max_execution_time": str(mc.max_execution_time),
        "cleanup_after_completion": str(mc.cleanup_after_completion).lower(),
        "retry_failed_jobs": str(mc.retry_failed_jobs).lower(),
        "cpu_request": resources.requests.cpu,
        "memory_request": resources.requests.memory,
        "cpu_limit": resources.limits.cpu,
        "memory_limit": resources.limits.memory,
    }

    if app.docker_config.environment_vars:
        data["environment_vars"] = ",".join(f"{k}={v}" for k, v in app.docker_config.environment_vars.items())

    custom_env = [f"{e.name}={e.value}" for e in app.environment if e.value is not None]
    if custom_env:
        data["custom_environment_vars"] = ",".join(custom_env)

    return {"resource": {"kubernetes_secret": {f"{app_name}_microservice": {
        "metadata": {
            "name": app_name,
            "namespace": namespace_ref(app.namespace),
            "labels": {"app": app_name, "type": "microservice", "managed-by": "kdeploy"},
        },
        "type": "Opaque",
        "data": data,
        "lifecycle": {"create_before_destroy": True, "ignore_changes": ["metadata[0].annotations"]},
    }}}}
