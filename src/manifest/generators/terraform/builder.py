"""Top-level orchestrator: `generate_terraform(app) -> dict` returns the full
`.tf.json` document for one app, dispatching by `app.app_type` - ported from
the old `generate_terraform_file`'s dispatch (service/microservice/job/
infrastructure) and the four `generate_*_terraform` assembly functions that
followed from it.

The returned dict is plain Python data (built entirely from the other
modules' dict fragments via `merge_fragments`) - the ONLY thing that ever
turns it into `.tf.json` text is `json.dumps()` at the call site (see
`render`), which is what makes the HCL-injection vulnerability class this
tool replaces structurally impossible: there is no string-templating step
for an app.yml value to escape out of.
"""

from __future__ import annotations

import json
from typing import Any

from manifest.schema.models import AppConfig, AppType

from . import config as config_gen
from . import networking, rbac, scaling, storage, workloads
from ._common import merge_fragments


def _rbac_fragment(app: AppConfig, app_name: str) -> dict[str, Any]:
    if not app.rbac.enabled:
        return {}
    return rbac.build_rbac(app, app_name)


def _generate_service(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_service_terraform`."""
    return merge_fragments(
        config_gen.build_secret(app, app_name),
        _rbac_fragment(app, app_name),
        storage.build_pvcs(app, app_name),
        workloads.build_deployment(app, app_name),
        networking.build_service(app, app_name),
        config_gen.build_configmap(app, app_name),
        networking.build_ingress(app, app_name),
        scaling.build_hpa(app, app_name),
    )


def _generate_infrastructure(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_infrastructure_terraform` - same three-way
    workload-shape branch (DaemonSet / Deployment+PVCs (get_or_create
    storage) / StatefulSet+headless-Service+PVCs (fresh storage))."""
    fragments = [
        config_gen.build_secret(app, app_name),
        _rbac_fragment(app, app_name),
        config_gen.build_configmap(app, app_name),
    ]

    if app.infrastructure_config.daemonset:
        fragments.append(workloads.build_daemonset(app, app_name))
        fragments.append(networking.build_service(app, app_name))
    elif app.storage:
        has_get_or_create = any(s.get_or_create for s in app.storage)
        fragments.append(storage.build_pvcs(app, app_name))
        if has_get_or_create:
            fragments.append(workloads.build_deployment(app, app_name))
            fragments.append(networking.build_service(app, app_name))
        else:
            fragments.append(workloads.build_statefulset(app, app_name))
            fragments.append(networking.build_headless_service(app, app_name))
            fragments.append(networking.build_service(app, app_name))
    else:
        fragments.append(storage.build_pvcs(app, app_name))  # no-op: no storage entries
        fragments.append(workloads.build_deployment(app, app_name))
        fragments.append(networking.build_service(app, app_name))

    fragments.append(networking.build_ingress(app, app_name))
    fragments.append(scaling.build_hpa(app, app_name))
    return merge_fragments(*fragments)


def _generate_microservice(app: AppConfig, app_name: str) -> dict[str, Any]:
    """Ported from `generate_microservice_terraform` - a microservice app
    produces only a Secret (image/config for jobber to read and run
    dynamically), never its own workload."""
    return config_gen.build_microservice_secret(app, app_name)


def _generate_job(app: AppConfig, app_name: str) -> dict[str, Any]:
    """`app_type: job` was a confirmed stub in the old generator (emitted a
    comment and nothing else) - this is the real implementation, reusing
    Secret/RBAC the same way `_generate_service` does, plus the new
    `workloads.build_job`."""
    return merge_fragments(
        config_gen.build_secret(app, app_name),
        _rbac_fragment(app, app_name),
        workloads.build_job(app, app_name),
    )


def generate_terraform(app: AppConfig) -> dict[str, Any]:
    """Returns the full `.tf.json` document (a dict with a top-level
    `resource` key) for one app, or `{}` if the app is disabled or produces
    no resources - matching the old `generate_terraform_file`'s dispatch and
    each `generate_*_terraform`'s own `"" if not any(...)"` empty check."""
    if not app.enabled:
        return {}

    app_name = app.name
    if app.app_type == AppType.microservice:
        return _generate_microservice(app, app_name)
    if app.app_type == AppType.job:
        return _generate_job(app, app_name)
    if app.app_type == AppType.infrastructure:
        return _generate_infrastructure(app, app_name)
    return _generate_service(app, app_name)


def render(app: AppConfig, *, indent: int = 2) -> str:
    """Serializes `generate_terraform(app)` to `.tf.json` text - the ONLY
    place in this whole package that calls `json.dumps`. Every app.yml
    string value reaching this point is already a plain dict leaf, so this
    call is what turns the vulnerability class the old string-templating
    generator had into a structural non-issue: json.dumps() always escapes
    `"`, `\\`, control characters, etc. in string values, and a JSON string
    value can never be reinterpreted as a new key or a new resource block."""
    return json.dumps(generate_terraform(app), indent=indent)
