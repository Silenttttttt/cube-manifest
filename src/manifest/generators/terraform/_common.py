"""Shared, low-level helpers used across every dict-builder in this package:
namespace references, image-reference resolution (ported from the old
terraform_generator.py's `_get_image_reference`/`_classify_and_format_image`/
`_is_standard_image`/`_generate_registry_image_reference`), and the
fragment-merging helper the orchestrator (builder.py) uses to stitch every
resource-type builder's output into one `.tf.json` document.

Every function here returns a plain Python value (str/dict/list) built from
typed AppConfig fields - never an f-string that concatenates an app.yml value
into a larger piece of HCL/JSON *text*. The one place an f-string embeds an
app.yml-controlled value at all (`namespace_ref`) only ever does so when that
value is exactly the fixed literal "dev" or "prod" (checked by set
membership first) - so the interpolated content is always one of those two
hardcoded constants, never arbitrary app.yml data.
"""

from __future__ import annotations

from typing import Any

from manifest.schema.models import AppConfig, AppType, DockerConfig

# Namespaces this tool's own root module declares as managed
# `kubernetes_namespace` resources - matches the old generator's
# MANAGED_NAMESPACES. Anything else is a pre-existing cluster namespace
# referenced as a literal string instead.
MANAGED_NAMESPACES = {"dev", "prod"}

# Docker Hub images the old generator recognized as "standard" (well-known,
# safe to reference directly rather than route through the local registry).
STANDARD_IMAGES = {
    "redis", "postgres", "mysql", "mongodb", "nginx", "alpine", "ubuntu", "debian",
    "rabbitmq", "elasticsearch", "memcached", "node", "python", "openjdk", "golang",
    "busybox", "registry", "grafana", "prometheus", "traefik", "jenkins", "docker",
}

_DEFAULT_DOCKER_CONFIG = DockerConfig()


def namespace_ref(namespace: str) -> str:
    """A Terraform expression referencing a namespace's real name (for
    namespaces this tool manages), or the plain literal namespace name
    otherwise. The returned string is used as-is as a JSON string value -
    for a MANAGED_NAMESPACES hit it happens to look like a Terraform
    interpolation expression on purpose (that's how you reference another
    resource's attribute in `.tf.json`), for anything else it's just the
    namespace name itself.
    """
    if namespace in MANAGED_NAMESPACES:
        return f"${{kubernetes_namespace.{namespace}.metadata[0].name}}"
    return namespace


def registry_image_reference(app_name: str) -> str:
    """Registry-based image reference for the local registry - always
    'latest', matching the old generator's `_generate_registry_image_reference`
    (2-tag system: 'latest' for deployments, 'previous' for rollback,
    managed outside Terraform)."""
    return f"192.168.1.105:30500/{app_name}:latest"


def is_standard_image(image_name: str) -> bool:
    base_name = image_name.split(":")[0].split("/")[0]
    return base_name in STANDARD_IMAGES


def is_locally_built(app: AppConfig) -> bool:
    """Proxy for the old raw-dict check `'docker_config' in config`. The
    schema always materializes `app.docker_config` as a DockerConfig
    instance (default_factory) whether or not app.yml set the block at all,
    so literal-presence can't be recovered - a non-default DockerConfig is
    the closest available signal that app.yml genuinely declared one (every
    real docker_config-using app sets at least one non-default field:
    language, entry_point, dockerfile, etc.)."""
    return app.docker_config != _DEFAULT_DOCKER_CONFIG


def classify_and_format_image(image_name: str, app_name: str, app: AppConfig) -> str:
    """Classify image as local vs external and format appropriately - ported
    from `_classify_and_format_image`."""
    if image_name.startswith("local/"):
        return registry_image_reference(app_name)

    if image_name.startswith(("docker-registry/", "docker-registry-service.")):
        return image_name

    if ":" in image_name and ("/" in image_name or is_standard_image(image_name)):
        return image_name

    if is_locally_built(app):
        return registry_image_reference(app_name)

    if ":" not in image_name and "/" not in image_name:
        if app.app_type == AppType.infrastructure:
            return f"{image_name}:latest"
        return registry_image_reference(app_name)

    return image_name


def get_image_reference(app_name: str, app: AppConfig) -> str:
    """Ported from `_get_image_reference` - image_config wins over the
    legacy `image:` block, both win over the "build it locally" fallback."""
    if app.image_config is not None:
        return classify_and_format_image(app.image_config.image, app_name, app)

    if app.image.name is not None:
        return classify_and_format_image(app.image.name, app_name, app)

    return registry_image_reference(app_name)


def default_pull_policy(image_ref: str) -> str:
    """Ported from the repeated inline logic in `generate_deployment`/
    `generate_statefulset`/`_validate_image_config`."""
    if image_ref.startswith(("docker-registry-service.", "192.168.1.105:30500/")):
        return "Always"
    return "Always" if (":" in image_ref and not image_ref.endswith(":latest")) else "Never"


def resolve_pull_policy(app: AppConfig, computed_default: str) -> str:
    """Ported from `config['image_config'].get('pull_policy', default_pull_policy)`
    / `config.get('image', {}).get('pull_policy', default_pull_policy)`.

    The schema always materializes SOME pull_policy value (ImageSpec defaults
    to "IfNotPresent", ImageConfig to "Never"), so "the key was omitted" can't
    be told apart from "the key was set to that same default" just by reading
    the field - `model_fields_set` (pydantic v2) recovers exactly that
    distinction, letting this reproduce the old get()-with-computed-default
    behavior faithfully instead of silently taking the model's own static
    default.
    """
    if app.image_config is not None:
        if "pull_policy" in app.image_config.model_fields_set:
            return app.image_config.pull_policy
        return computed_default
    if "pull_policy" in app.image.model_fields_set:
        return app.image.pull_policy
    return computed_default


def effective_replicas(app: AppConfig) -> int:
    """Ported from `config.get('scaling', {}).get('replicas', config.get('replicas', 1))`.
    Same `model_fields_set` technique as `resolve_pull_policy` - Scaling.replicas
    always has a materialized value (default 1), so without this an app that
    sets only the top-level `replicas:` (e.g. activator: replicas: 2, no
    `scaling:` block at all) would silently regress to 1."""
    if "replicas" in app.scaling.model_fields_set:
        return app.scaling.replicas
    return app.replicas


def merge_fragments(*fragments: dict[str, Any]) -> dict[str, Any]:
    """Deep-merges any number of `{"resource": {type: {name: body}}}`
    fragments (some builders return `{}` when they generate nothing, matching
    the old generator's "" empty-string convention) into one `.tf.json`
    document."""
    result: dict[str, Any] = {}
    for frag in fragments:
        for top_key, resources in frag.items():
            bucket = result.setdefault(top_key, {})
            for rtype, instances in resources.items():
                type_bucket = bucket.setdefault(rtype, {})
                type_bucket.update(instances)
    return result
