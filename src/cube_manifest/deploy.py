"""Real terraform plan/apply orchestration for one app at a time.

Replaces the old system's conflict-resolution approach (regex-scrape a
`terraform apply` error for `"X" already exists`, guess the resource KIND
from the name's SUFFIX, `terraform state rm` + `kubectl delete` on that
guess) with something structurally safer: before ever planning or applying,
check via a real, resource-kind-aware, read-only `kubectl get` whether each
generated resource already exists live, and if so `terraform import` it
into this run's local state first. `terraform import` only ever adds
tracking to local Terraform state - it cannot mutate or delete the real
object - so this whole discovery+import phase is exactly as safe as `plan`
itself. Only `real_apply()` can mutate the cluster, and only once a human
(or a caller that explicitly decided to) has seen the plan.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cube_manifest.generators.terraform.builder import generate_terraform
from cube_manifest.schema.models import AppConfig

# Terraform resource type -> (kubectl kind, is namespaced). Covers every
# resource type the current generator can emit (see
# generators/terraform/builder.py) - extend here if a new resource type is
# added to the generator.
KUBECTL_KIND: dict[str, tuple[str, bool]] = {
    "kubernetes_namespace": ("namespace", False),
    "kubernetes_deployment": ("deployment", True),
    "kubernetes_stateful_set": ("statefulset", True),
    "kubernetes_daemonset": ("daemonset", True),
    "kubernetes_service": ("service", True),
    "kubernetes_endpoints": ("endpoints", True),
    "kubernetes_config_map": ("configmap", True),
    "kubernetes_secret": ("secret", True),
    "kubernetes_service_account": ("serviceaccount", True),
    "kubernetes_role": ("role", True),
    "kubernetes_role_binding": ("rolebinding", True),
    "kubernetes_cluster_role": ("clusterrole", False),
    "kubernetes_cluster_role_binding": ("clusterrolebinding", False),
    "kubernetes_persistent_volume_claim": ("persistentvolumeclaim", True),
    "kubernetes_horizontal_pod_autoscaler_v2": ("horizontalpodautoscaler", True),
    "kubernetes_job": ("job", True),
    "kubernetes_cron_job_v1": ("cronjob", True),
    "kubernetes_ingress_v1": ("ingress", True),
}


@dataclass
class ResourceRef:
    tf_type: str
    tf_name: str
    k8s_name: str
    namespaced: bool


def _resource_refs(tf_doc: dict[str, Any]) -> list[ResourceRef]:
    refs = []
    for tf_type, instances in tf_doc.get("resource", {}).items():
        kind_info = KUBECTL_KIND.get(tf_type)
        for tf_name, body in instances.items():
            meta = body.get("metadata", {}) if isinstance(body, dict) else {}
            k8s_name = meta.get("name", tf_name)
            namespaced = kind_info[1] if kind_info else ("namespace" in meta)
            refs.append(ResourceRef(tf_type=tf_type, tf_name=tf_name, k8s_name=k8s_name, namespaced=namespaced))
    return refs


def resource_exists(tf_type: str, k8s_name: str, namespace: str, namespaced: bool) -> bool:
    """Read-only. Never mutates anything."""
    kind = KUBECTL_KIND.get(tf_type, (None, None))[0]
    if kind is None:
        return False
    cmd = ["kubectl", "get", kind, k8s_name]
    if namespaced:
        cmd += ["-n", namespace]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode == 0


# Matches infrastructure/terraform/main.tf's own namespace resources exactly
# (labels included) - the namespace here always gets imported (never
# created, see prepare_and_plan), but Terraform still diffs the FULL
# config against real state, so if this doesn't match main.tf's config the
# plan would show (and --yes would apply) an unintended, unreviewed change
# to a resource this tool doesn't actually own. Confirmed by testing: an
# earlier version of this function that omitted labels produced a real
# "will remove environment=development" diff on the shared dev namespace.
_KNOWN_NAMESPACE_LABELS: dict[str, dict[str, str]] = {
    "dev": {"environment": "development"},
    "prod": {"environment": "production"},
}


def provider_document(kubeconfig: Path, namespace: str) -> dict[str, Any]:
    """A minimal kubernetes-provider .tf.json fragment for handling ONE app
    in isolation from Cubernetes' shared root module (which normally owns
    the namespace resources - see infrastructure/terraform/main.tf). The
    namespace is always imported (see prepare_and_plan) rather than left to
    be "created," so this never conflicts with the real root module's own
    management of it - but Terraform still diffs this config against the
    real object, so it must match main.tf's own definition, not just the
    bare name."""
    labels = _KNOWN_NAMESPACE_LABELS.get(namespace, {})
    metadata: dict[str, Any] = {"name": namespace}
    if labels:
        metadata["labels"] = labels
    return {
        "terraform": {
            "required_providers": {
                "kubernetes": {"source": "hashicorp/kubernetes", "version": "~> 2.20"},
            }
        },
        "provider": {"kubernetes": [{"config_path": str(kubeconfig)}]},
        "resource": {
            "kubernetes_namespace": {
                namespace: {"metadata": [metadata]},
            }
        },
    }


@dataclass
class PlanResult:
    workdir: Path
    output: str
    returncode: int
    imported: list[str] = field(default_factory=list)
    import_failures: list[str] = field(default_factory=list)
    unknown_kinds: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode in (0, 2)  # 2 == "plan has changes", still a clean run


def prepare_and_plan(app: AppConfig, kubeconfig: Path, *, label: str = "plan") -> PlanResult:
    """Writes provider+app terraform into a fresh temp workdir, imports any
    already-existing live resources, then runs `terraform plan -out=tfplan`.

    Strictly read-only against the real cluster: `kubectl get` and
    `terraform import`/`terraform plan` never create, modify, or delete a
    live object. The returned workdir's tfplan file is what a subsequent
    `real_apply()` call actually applies - nothing here does that itself.
    """
    tf_doc = generate_terraform(app)
    workdir = Path(tempfile.mkdtemp(prefix=f"cube-manifest-{label}-{app.name}-"))
    (workdir / "provider.tf.json").write_text(json.dumps(provider_document(kubeconfig, app.namespace), indent=2))
    (workdir / f"{app.name}.tf.json").write_text(json.dumps(tf_doc, indent=2))

    init = subprocess.run(["terraform", "init", "-input=false"], cwd=workdir, capture_output=True, text=True, check=False)
    if init.returncode != 0:
        return PlanResult(workdir=workdir, output=init.stdout + init.stderr, returncode=init.returncode)

    imported: list[str] = []
    import_failures: list[str] = []

    def _import(address: str, import_id: str) -> None:
        res = subprocess.run(
            ["terraform", "import", "-input=false", address, import_id],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            imported.append(address)
        else:
            import_failures.append(f"{address} ({import_id}): {res.stderr.strip().splitlines()[-1] if res.stderr else 'unknown error'}")

    # The namespace always already exists for real - always import it so
    # the plan never proposes "creating" a namespace that's already there.
    _import(f"kubernetes_namespace.{app.namespace}", app.namespace)

    unknown_kinds: list[str] = []
    for ref in _resource_refs(tf_doc):
        if ref.tf_type not in KUBECTL_KIND:
            # We have no idea whether this actually exists live - resource_exists()
            # would silently return False and let Terraform plan a "create" for
            # something that might already be there (exactly the bug a mapping-table
            # gap caused for kubernetes_daemonset once). Surface it loudly instead
            # of guessing, so a "will be created" for something you know already
            # exists is a signal to check this table, not just apply blindly.
            unknown_kinds.append(f"{ref.tf_type}.{ref.tf_name}")
            continue
        if resource_exists(ref.tf_type, ref.k8s_name, app.namespace, ref.namespaced):
            import_id = f"{app.namespace}/{ref.k8s_name}" if ref.namespaced else ref.k8s_name
            _import(f"{ref.tf_type}.{ref.tf_name}", import_id)
        # If it doesn't exist live, we deliberately do NOT import - Terraform
        # will plan to create it for real, which is correct.

    plan_run = subprocess.run(
        ["terraform", "plan", "-input=false", "-no-color", "-out=tfplan"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return PlanResult(
        workdir=workdir,
        output=plan_run.stdout + plan_run.stderr,
        returncode=plan_run.returncode,
        imported=imported,
        import_failures=import_failures,
        unknown_kinds=unknown_kinds,
    )


def real_apply(workdir: Path) -> subprocess.CompletedProcess[str]:
    """Applies the plan file `prepare_and_plan` already wrote to `workdir`.
    This is the ONLY function in this whole package that can mutate the
    real cluster - it always applies a plan file that was already computed
    and (by CLI convention) shown to the caller, never `-auto-approve`
    against unreviewed config."""
    return subprocess.run(
        ["terraform", "apply", "-input=false", "-no-color", "tfplan"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
