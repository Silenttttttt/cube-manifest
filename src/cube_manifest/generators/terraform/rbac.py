"""ServiceAccount, Role/ClusterRole, RoleBinding/ClusterRoleBinding
dict-builders.

The old terraform_generator.py had two nearly-identical function pairs for
this - `generate_role`/`generate_role_binding` (namespace-scoped) and
`generate_cluster_role`/`generate_cluster_role_binding` (cluster-scoped),
duplicating the same rule-building logic twice. `build_role_resources` below
is the one parameterized replacement (`cluster_scope: bool` picks the
resource type/address/metadata shape), ported from both.
"""

from __future__ import annotations

from typing import Any

from cube_manifest.schema.models import AppConfig, RbacRule

from ._common import merge_fragments, namespace_ref


def _build_rule(rule: RbacRule) -> dict[str, Any]:
    return {
        "api_groups": list(rule.api_groups),
        "resources": list(rule.resources),
        "verbs": list(rule.verbs),
    }


def build_service_account(app: AppConfig, app_name: str) -> dict[str, Any]:
    return {"resource": {"kubernetes_service_account": {f"{app_name}_service_account": {
        "metadata": {
            "name": f"{app_name}-service-account",
            "namespace": namespace_ref(app.namespace),
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        },
        "automount_service_account_token": True,
    }}}}


def build_role_resources(app: AppConfig, app_name: str, *, cluster_scope: bool) -> dict[str, Any]:
    """Role+RoleBinding (namespace-scoped) or ClusterRole+ClusterRoleBinding
    (cluster-scoped), picked by `cluster_scope` - one function replacing the
    old system's two near-duplicates."""
    rules = [_build_rule(r) for r in app.rbac.rules]
    namespace = namespace_ref(app.namespace)
    sa_ref = f"${{kubernetes_service_account.{app_name}_service_account.metadata[0].name}}"

    if cluster_scope:
        role_resource_type = "kubernetes_cluster_role"
        role_ref_kind = "ClusterRole"
        binding_resource_type = "kubernetes_cluster_role_binding"
        role_tf_name = f"{app_name}_cluster_role"
        binding_tf_name = f"{app_name}_cluster_role_binding"
        role_metadata: dict[str, Any] = {
            "name": f"{app_name}-cluster-role",
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        }
        binding_metadata: dict[str, Any] = {
            "name": f"{app_name}-cluster-role-binding",
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        }
    else:
        role_resource_type = "kubernetes_role"
        role_ref_kind = "Role"
        binding_resource_type = "kubernetes_role_binding"
        role_tf_name = f"{app_name}_role"
        binding_tf_name = f"{app_name}_role_binding"
        role_metadata = {
            "name": f"{app_name}-role",
            "namespace": namespace,
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        }
        binding_metadata = {
            "name": f"{app_name}-role-binding",
            "namespace": namespace,
            "labels": {"app": app_name, "managed-by": "cube_manifest"},
        }

    role_body = {"metadata": role_metadata, "rule": rules}
    binding_body = {
        "metadata": binding_metadata,
        "role_ref": {
            "api_group": "rbac.authorization.k8s.io",
            "kind": role_ref_kind,
            "name": f"${{{role_resource_type}.{role_tf_name}.metadata[0].name}}",
        },
        "subject": {"kind": "ServiceAccount", "name": sa_ref, "namespace": namespace},
    }

    return {"resource": {
        role_resource_type: {role_tf_name: role_body},
        binding_resource_type: {binding_tf_name: binding_body},
    }}


def build_rbac(app: AppConfig, app_name: str) -> dict[str, Any]:
    """ServiceAccount + (Role/RoleBinding or ClusterRole/ClusterRoleBinding),
    or `{}` if `rbac.enabled` is False. `rbac.scope: namespace` picks the
    namespace-scoped pair; the default `cluster` scope (matching the old
    generator's own default) picks the cluster-wide pair."""
    if not app.rbac.enabled:
        return {}
    return merge_fragments(
        build_service_account(app, app_name),
        build_role_resources(app, app_name, cluster_scope=(app.rbac.scope == "cluster")),
    )
