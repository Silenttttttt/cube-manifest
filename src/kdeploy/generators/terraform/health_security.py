"""Translates the schema's already-unified `HealthCheck`/`Probe` and
`SecurityContext` models into Kubernetes probe / securityContext dict
fragments.

compat.py has already collapsed the old system's three competing
health-check shapes and three competing security-context shapes into these
one-true-shape models before an AppConfig ever exists, so unlike the old
terraform_generator.py's `_build_health_checks`/`_build_security_context`
(which each had to re-detect and branch on which legacy shape was present),
these functions only ever see the one canonical shape.

Every value handled here (probe commands/paths, security-context ints/bools)
flows straight from a typed Pydantic field into a plain dict value - no
string templating.
"""

from __future__ import annotations

from typing import Any

from kdeploy.schema.models import HealthCheck, Probe, SecurityContext


def build_probe(probe: Probe) -> dict[str, Any]:
    """One k8s probe block (liveness_probe / readiness_probe body). A probe
    is exec (command) XOR http/tcp (path/port) per the schema's own
    validator - command wins if somehow both were set upstream anyway."""
    body: dict[str, Any] = {
        "initial_delay_seconds": probe.initial_delay_seconds,
        "period_seconds": probe.period_seconds,
        "timeout_seconds": probe.timeout_seconds,
        "failure_threshold": probe.failure_threshold,
        "success_threshold": probe.success_threshold,
    }
    if probe.command:
        body["exec"] = {"command": list(probe.command)}
    elif probe.path is not None:
        http_get: dict[str, Any] = {"path": probe.path}
        if probe.port is not None:
            http_get["port"] = probe.port
        body["http_get"] = http_get
    elif probe.port is not None:
        body["tcp_socket"] = {"port": probe.port}
    return body


def build_probes(health_check: HealthCheck) -> dict[str, Any]:
    """Container-spec fragment with `liveness_probe`/`readiness_probe` keys
    (only the ones actually configured), or `{}` if health checks are
    disabled or neither probe is set - ported from `_build_health_checks`'s
    net effect now that compat.py has already resolved which legacy shape
    won."""
    result: dict[str, Any] = {}
    if not health_check.enabled:
        return result
    if health_check.liveness is not None:
        result["liveness_probe"] = build_probe(health_check.liveness)
    if health_check.readiness is not None:
        result["readiness_probe"] = build_probe(health_check.readiness)
    return result


def build_container_security_context(sc: SecurityContext) -> dict[str, Any]:
    """Container-level `security_context {}` fragment.

    The old generator's container-level `_build_security_context` (used by
    Deployment/StatefulSet) only ever emitted run_as_user/run_as_group,
    while its DaemonSet-only sibling `_build_infrastructure_security_context`
    supported the fuller set (run_as_non_root, read_only_root_filesystem,
    allow_privilege_escalation) - a real asymmetry from having three
    competing input shapes with different consumers. Since the schema now
    gives every workload type (Deployment/StatefulSet/DaemonSet/Job) the
    exact same canonical `SecurityContext` model, this emits the full field
    set uniformly for all of them: a superset of the old Deployment/
    StatefulSet behavior, but only produces output for fields the app.yml
    actually set (so it's a no-op difference for every one of the 26 real
    apps today, none of which set those extra fields outside a DaemonSet).
    """
    body: dict[str, Any] = {}
    if sc.run_as_user is not None:
        body["run_as_user"] = sc.run_as_user
    if sc.run_as_group is not None:
        body["run_as_group"] = sc.run_as_group
    if sc.run_as_non_root is not None:
        body["run_as_non_root"] = sc.run_as_non_root
    if sc.read_only_root_filesystem is not None:
        body["read_only_root_filesystem"] = sc.read_only_root_filesystem
    if sc.allow_privilege_escalation is not None:
        body["allow_privilege_escalation"] = sc.allow_privilege_escalation
    if sc.capabilities is not None:
        cap: dict[str, Any] = {}
        if sc.capabilities.drop:
            cap["drop"] = list(sc.capabilities.drop)
        if sc.capabilities.add:
            cap["add"] = list(sc.capabilities.add)
        if cap:
            body["capabilities"] = cap
    return body


def build_pod_security_context(sc: SecurityContext) -> dict[str, Any]:
    """Pod-level `security_context {}` fragment - fs_group/fs_group_change_policy
    only, matching the old `_build_pod_config`'s narrow scope (it read these
    two fields directly off the legacy `security:` block, a separate code
    path from the container-level security context builder)."""
    body: dict[str, Any] = {}
    if sc.fs_group is not None:
        body["fs_group"] = sc.fs_group
    if sc.fs_group_change_policy is not None:
        body["fs_group_change_policy"] = sc.fs_group_change_policy
    return body
