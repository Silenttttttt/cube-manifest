"""The single, validated app.yml schema.

Every field the old app-generator/simple-deploy system reads (across both
tools) lives here as a typed Pydantic model, including fields the old
config_parser.py never validated at all (scheduling, scaling, storage,
ingress, image, security_context, health_check, init_containers) - a
malformed entry now fails at load time with a clear field path instead of
a raw KeyError deep inside a generator.

This module defines shapes only. Normalizing the legacy/competing input
shapes (three health-check schemas, three security-context schemas, two
node_port shapes, the "deployment" app_type alias) into these canonical
models happens in compat.py, before an AppConfig is ever constructed -
these models only ever see the one canonical shape.

Some fields below (app_name, cleanup.*, container, container_security_context,
expose_service, monitoring, version, image_pull_policy, image_pull_timeout,
docker_config.dockerfile/context) are confirmed DEAD CODE in the current
terraform_generator.py - never read by any generator function - but they are
still real, user-authored data in the 26 production app.yml files, so they're
modeled properly here rather than rejected or silently dropped. A future
generator is free to start consuming them.
"""

from __future__ import annotations

import re
import warnings
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _validate_dns_label(value: str, field_name: str) -> str:
    if not DNS_LABEL_RE.match(value):
        raise ValueError(f"{field_name} must be a valid DNS-1123 label (lowercase alphanumeric/'-'), got {value!r}")
    return value


class AppType(str, Enum):
    service = "service"
    microservice = "microservice"
    job = "job"
    infrastructure = "infrastructure"
    # A real physical/external device or host that can never run as a pod in
    # this cluster (e.g. an ESP32 on the LAN) - gets a selector-less Service +
    # a hand-specified Endpoints resource instead of a Deployment, via the
    # standard k8s "Service without selector" pattern. See ExternalEndpoint.
    external = "external"


class ServiceType(str, Enum):
    cluster_ip = "ClusterIP"
    node_port = "NodePort"
    load_balancer = "LoadBalancer"
    none = "none"


class ResourceProfile(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    extreme = "extreme"


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: str
    memory: str
    # The cluster's one real extended resource (see nvidia-device-plugin,
    # which advertises `nvidia.com/gpu` on the one node that has one) - a
    # plain string count (e.g. "1"), matching how k8s itself represents
    # extended resource quantities. Confirmed nothing in the 26 real app.yml
    # files predating this used a GPU resource at all - `resources.limits`/
    # `requests` only ever emitted cpu/memory (see workloads.py), so this is
    # new, not a rename of an existing (dead) field. Left unset (None) for
    # every app that doesn't request a GPU - the generator only emits the
    # `nvidia.com/gpu` key when this is actually set.
    gpu: str | None = None


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: ResourceSpec = Field(default_factory=lambda: ResourceSpec(cpu="100m", memory="128Mi"))
    limits: ResourceSpec = Field(default_factory=lambda: ResourceSpec(cpu="500m", memory="512Mi"))


class ImageConfig(BaseModel):
    """The legacy top-level `image:` block (`image: {name, pull_policy}` or
    the empty-dict `image: {}` idiom meaning "let the generator decide")."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    pull_policy: Literal["Always", "IfNotPresent", "Never"] = "Never"


class ImageSpec(BaseModel):
    """The newer `image_config:` block (`image_config: {image, pull_policy}`) -
    real precedence confirmed in terraform_generator.py::_get_image_reference
    and ::generate_deployment: image_config wins over the legacy `image:`
    block whenever both are present."""

    model_config = ConfigDict(extra="forbid")
    image: str
    pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"


class CacheOptimization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    dependency_cache_mount: str | None = None
    build_cache_mount: str | None = None


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create_user: bool = True
    user: str = "appuser"
    uid: int = 1000
    gid: int = 1000


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    port: int | None = None
    command: list[str] | None = None
    initial_delay_seconds: int = 5
    period_seconds: int = 10
    timeout_seconds: int = 3
    failure_threshold: int = 3
    success_threshold: int = 1

    @model_validator(mode="after")
    def one_probe_kind(self):
        if self.command and (self.path or self.port):
            raise ValueError("a probe must be either exec (command) or http/tcp (path/port), not both")
        return self


class HealthCheck(BaseModel):
    """The ONE canonical k8s health-check shape. compat.py normalizes the
    three legacy shapes (docker_config.health_check / health_check.readiness_probe
    +liveness_probe / health_checks.liveness+readiness) into this."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    liveness: Probe | None = None
    readiness: Probe | None = None


class DockerHealthCheck(BaseModel):
    """docker_config.health_check is a DIFFERENT, Docker-native HEALTHCHECK-
    instruction shape (interval/retries/timeout are Docker concepts, not k8s
    probe fields) - dual-purpose with the canonical k8s HealthCheck above.
    terraform_generator.py (lines ~1538-1577) reads only enabled/command from
    this to build k8s liveness/readiness probes with HARDCODED timing
    (liveness: initial_delay=30/period=30/timeout=5/failure_threshold=3;
    readiness: initial_delay=5/period=10/timeout=5/failure_threshold=3),
    ignoring interval/retries/timeout for that purpose - those three fields
    are for a future Dockerfile generator's real `HEALTHCHECK` instruction
    instead. compat.py's normalize_health_check READS this block (without
    removing it) to derive the canonical health_check.{liveness,readiness};
    this model lets DockerConfig also parse it normally, untouched."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    command: str | list[str] | None = None
    interval: str = "30s"
    retries: int = 3
    timeout: str = "10s"


class SecurityCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drop: list[str] = Field(default_factory=list)
    add: list[str] = Field(default_factory=list)


class SecurityContext(BaseModel):
    """The ONE canonical security-context shape, replacing the three
    competing legacy shapes (security_context / security / docker_config.user_config).
    Also reused as-is for container_security_context and
    infrastructure_config.security_context, which share this exact shape."""

    model_config = ConfigDict(extra="forbid")
    run_as_user: int | None = None
    run_as_group: int | None = None
    run_as_non_root: bool | None = None
    read_only_root_filesystem: bool | None = None
    fs_group: int | None = None
    fs_group_change_policy: str | None = None
    allow_privilege_escalation: bool | None = None
    capabilities: SecurityCapabilities | None = None


class DockerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = "generic"
    base_image: str | None = None
    working_dir: str = "/app"
    system_dependencies: list[str] = Field(default_factory=list)
    build_dependencies: list[str] = Field(default_factory=list)
    runtime_dependencies: list[str] = Field(default_factory=list)
    build_commands: list[str] = Field(default_factory=list)
    entry_point: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    environment_vars: dict[str, str] = Field(default_factory=dict)
    install_env: dict[str, str] = Field(default_factory=dict)
    exposed_ports: list[int] = Field(default_factory=list)
    cache_optimization: CacheOptimization = Field(default_factory=CacheOptimization)
    user_config: UserConfig | None = None
    health_check: DockerHealthCheck | None = None
    # Dockerfile-build shape (local-storage, shy-worm-*, video-generator,
    # voice-cloning, whatsapp-clone): a repo is fetched (external_repo) and
    # built from a real Dockerfile instead of the docker-less fields above -
    # never consumed by terraform_generator.py itself (that's a separate
    # build-pipeline concern), but real app.yml data.
    dockerfile: str | None = None
    context: str | None = None


class MicroserviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_profile: ResourceProfile = ResourceProfile.medium
    max_execution_time: int = 300
    cleanup_after_completion: bool = True
    retry_failed_jobs: bool = False
    queue_name: str | None = None  # defaulted to f"{app.name}-queue" by AppConfig's validator
    custom_resources: Resources | None = None


class SecretKeyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    key: str


class EnvVarValueFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret_key_ref: SecretKeyRef


class EnvVar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: str | None = None
    value_from: EnvVarValueFrom | None = None

    @model_validator(mode="after")
    def at_most_one_value_source(self):
        # Real precedent: polymarket-collector/app.yml has an environment
        # entry (MIN_LIQUIDITY_USD) with neither value nor value_from set -
        # terraform_generator.py's _build_environment_variables silently skips
        # any entry missing both keys (`if 'value' in env: ... elif
        # 'value_from' in env: ...`, no else) rather than erroring, so a
        # value-less entry is real, if probably accidental, production data -
        # only BOTH set at once (genuinely ambiguous) is rejected here.
        if self.value is not None and self.value_from is not None:
            raise ValueError(f"env var {self.name!r} must set at most one of value/value_from, not both")
        return self


class RbacRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_groups: list[str]
    resources: list[str]
    verbs: list[str]


class Rbac(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    scope: Literal["namespace", "cluster"] = "cluster"
    rules: list[RbacRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def rules_required_if_enabled(self):
        if self.enabled and not self.rules:
            raise ValueError("rbac.enabled is true but rbac.rules is empty")
        return self


class StorageEntry(BaseModel):
    """Unifies the old storage[] (PVC) and volumes[] (legacy hostPath) shapes
    conceptually - a StorageEntry with host_path set generates a hostPath
    volume, one with size set generates a PVC, one with empty_dir set
    generates an emptyDir. Exactly one of the three must be set (enforced
    below), instead of separate, silently-coexisting fields.

    empty_dir exists for ephemeral, node-agnostic data shared between an
    init container and the main container within the same pod (e.g. an
    init container writing a config file the main container reads at
    startup) - unlike host_path, it doesn't tie the pod to a specific
    node's filesystem, and unlike a PVC it needs no storage class or
    provisioner, matching what a real, portable OSS tool should default to
    for "just share a small file/dir within this pod, don't persist it."
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    mount_path: str
    size: str | None = None
    host_path: str | None = None
    empty_dir: bool = False
    # Real default confirmed against terraform_generator.py (every call site
    # reads it as `storage.get('get_or_create', False)`, e.g. line 226's
    # `has_get_or_create` workload-shape check and line 2209's PVC-vs-
    # volume_claim_template branch) - defaulting this to True instead would
    # silently flip an app.yml that never sets the key from the currently-
    # deployed StatefulSet+headless-Service+volume_claim_template shape to a
    # Deployment+standalone-PVC shape (confirmed live: docker-registry/app.yml
    # doesn't set get_or_create and is a real, currently-running StatefulSet).
    get_or_create: bool = False
    pvc_name: str | None = None
    existing_pvc: str | None = None
    prevent_destroy: bool = False
    reclaim_policy: Literal["Retain", "Delete"] | None = None
    storage_class: str | None = None  # None = use the cluster config's default, not a hardcoded literal
    access_mode: str = "ReadWriteOnce"
    read_only: bool = False

    @model_validator(mode="after")
    def exactly_one_backing(self):
        backings = [self.size is not None, self.host_path is not None, self.empty_dir]
        if sum(backings) != 1:
            raise ValueError(
                f"storage entry {self.name!r} must set exactly one of "
                "size (PVC) / host_path (hostPath) / empty_dir (emptyDir)"
            )
        return self


class IngressConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    host: str | None = None
    service_port: int = 80
    # Matches the old generate_ingress's real default (ingress_config.get('tls', True))
    # exactly - confirmed via a real live cutover attempt: local-storage-ui's app.yml
    # doesn't set `tls` at all, and defaulting to False here would have silently
    # stripped a real, currently-working internal-wildcard-tls cert on apply.
    tls: bool = True
    tls_secret: str | None = None

    @model_validator(mode="after")
    def host_required_if_enabled(self):
        if self.enabled and not self.host:
            raise ValueError("ingress.enabled is true but ingress.host is not set")
        return self


class Toleration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = None
    operator: Literal["Exists", "Equal"] | None = None
    value: str | None = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = None


class NodeAffinityTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    operator: str
    values: list[str] = Field(default_factory=list)
    weight: int | None = None  # only meaningful for `preferred` terms


class NodeAffinity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: list[NodeAffinityTerm] = Field(default_factory=list)
    preferred: list[NodeAffinityTerm] = Field(default_factory=list)


class Affinity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_affinity: NodeAffinity | None = None


class AntiAffinity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    type: str | None = None


class Rebalancing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    strategy: Literal["immediate", "gradual", "manual"] | None = None
    trigger: Literal["node_availability", "resource_pressure", "schedule", "manual"] | None = None
    min_age_minutes: int | None = None
    cooldown_minutes: int | None = None


class SchedulingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Real accepted presets confirmed in terraform_generator.py::_validate_scheduling_config
    # (valid_presets = {'critical', 'indifferent', 'workload'}) and
    # ::_apply_scheduling_presets - NOT indifferent/flexible/strict as
    # originally assumed. 'flexible' is a real but auto-corrected typo (the
    # old code prints an ERROR and rewrites it to 'indifferent' rather than
    # rejecting it) - compat.py normalizes that alias the same way.
    node_preference: Literal["critical", "indifferent", "workload"] = "indifferent"
    priority_class: str | None = None
    affinity: Affinity | None = None
    # A second, separate real shape: scheduling.node_affinity.required (no
    # `affinity` wrapper) used directly by rabbitmq/postgres, alongside
    # scheduling.affinity.node_affinity.{required,preferred} used by others -
    # both coexist as independent fields, not unified, matching the two
    # genuinely distinct shapes found across the real app.yml files.
    node_affinity: NodeAffinity | None = None
    anti_affinity: AntiAffinity | None = None
    tolerations: list[Toleration] = Field(default_factory=list)
    rebalancing: Rebalancing | None = None


class HostPathSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    type: str = "DirectoryOrCreate"


class InfraVolume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    host_path: HostPathSpec


class VolumeMountSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    mount_path: str
    read_only: bool = False


class InfrastructureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daemonset: bool = False
    tolerations: list[Toleration] = Field(default_factory=list)
    node_selector: dict[str, str] = Field(default_factory=dict)
    runtime_class_name: str | None = None
    volumes: list[InfraVolume] = Field(default_factory=list)
    volume_mounts: list[VolumeMountSpec] = Field(default_factory=list)
    security_context: SecurityContext | None = None


class ActivationType(str, Enum):
    http = "http"
    tcp = "tcp"
    queue_depth = "queue_depth"


class QueueRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    host: str


class Activation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ActivationType = ActivationType.http
    port: int | None = None
    extra_ports: list[int] = Field(default_factory=list)
    queue: QueueRef | None = None

    @model_validator(mode="after")
    def queue_required_for_queue_depth(self):
        if self.type == ActivationType.queue_depth and self.queue is None:
            raise ValueError("scaling.activation.type is queue_depth but scaling.activation.queue is not set")
        return self


class Scaling(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replicas: int = 1
    max_replicas: int = 1
    target_cpu_utilization_percentage: int = Field(default=70, ge=1, le=100)
    min_replicas: int = 1
    idle_timeout_seconds: int = 300
    write_protected: bool = False
    activation: Activation | None = None

    @model_validator(mode="after")
    def activation_recommended_for_scale_to_zero(self):
        # Not fatal (fixes the old system's silent-fallback-to-port-80 behavior
        # by at least making the gap loud instead of invisible) - a future
        # cluster-config "strict mode" can upgrade this to a hard error.
        if self.min_replicas == 0 and self.activation is None:
            warnings.warn(
                "scaling.min_replicas is 0 but scaling.activation is not set - "
                "defaulting to activation.type=http, which assumes the app's real "
                "port is discoverable elsewhere in the config. Set scaling.activation "
                "explicitly to avoid the activator proxying to the wrong port.",
                stacklevel=2,
            )
        return self


class InitContainer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    image: str
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    env: list[EnvVar] = Field(default_factory=list)
    volume_mounts: list[VolumeMountSpec] = Field(default_factory=list)
    security_context: SecurityContext | None = None


PATH_PREFIX_RE = re.compile(r"^/[a-zA-Z0-9/_-]*$")


class VpsRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path_prefix: str
    host: str | None = None

    @field_validator("path_prefix")
    @classmethod
    def slug_safe(cls, v: str) -> str:
        # Defense-in-depth: this value ends up embedded in a remote command
        # built by the vps-routing plugin - reject anything that isn't a
        # plain path slug at the schema boundary, before it ever reaches
        # any command-construction code.
        if not PATH_PREFIX_RE.match(v):
            raise ValueError(f"vps_route.path_prefix must match {PATH_PREFIX_RE.pattern!r}, got {v!r}")
        return v


class ExternalRepo(BaseModel):
    """Real field names confirmed against every external_repo: block in the 26
    apps (local-storage, local-storage-ui, shy-worm-*, video-generator,
    voice-cloning, whatsapp-clone): url/branch/path/ssh_key_secret - NOT
    repo_url/subdirectory, which never appear anywhere in real data."""

    model_config = ConfigDict(extra="forbid")
    url: str
    branch: str = "main"
    path: str | None = None
    ssh_key_secret: str | None = None


class RollingUpdateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_surge: int = 1
    max_unavailable: int = 1


class DeploymentStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["RollingUpdate", "Recreate"] = "RollingUpdate"
    rolling_update: RollingUpdateSpec | None = None


class CleanupConfig(BaseModel):
    """_get_cleanup_config in terraform_generator.py is confirmed DEAD CODE -
    never called by any generator - but `cleanup:` is still real,
    user-authored data in several app.yml files, so it validates cleanly here
    even though nothing consumes it yet."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    auto_cleanup: bool = False
    preserve_data: bool = True
    preserve_pvcs: bool = False
    preserve_secrets: bool = False
    force_recreate: bool = False
    cleanup_orphaned_services: bool = False
    cleanup_duplicate_deployments: bool = False


class MonitoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    metrics_port: int | None = None
    health_endpoint: str | None = None


class ContainerSpec(BaseModel):
    """The top-level `container:` block (redis's shape: command/args only) -
    a THIRD, distinct container shape alongside docker_config and
    container_config. Confirmed dead in terraform_generator.py (no generator
    function reads config['container']), but real data."""

    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)


class ContainerPort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "http"
    container_port: int = 8080
    protocol: str = "TCP"


class ContainerConfig(BaseModel):
    """The `container_config:` block consumed by generate_daemonset (and its
    helpers) around terraform_generator.py lines 2382-2638 - the real
    container spec for daemonset-style apps (nvidia-device-plugin), used
    instead of docker_config since these apps deploy a pre-built image with
    no local build step."""

    model_config = ConfigDict(extra="forbid")
    image: str
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    ports: list[ContainerPort] = Field(default_factory=list)
    env: list[EnvVar] = Field(default_factory=list)


class ServicePort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    port: int
    target_port: int | None = None
    protocol: str = "TCP"
    node_port: int | None = None


class ServiceSpec(BaseModel):
    """The `service:` block (rabbitmq, postgres, adguard-home, redis,
    rabbitmq-sender's shape) - genuinely read by terraform_generator.py's
    generate_service() for `.type` and the full multi-port `.ports[]` list
    (name/port/target_port/protocol/node_port), independent of the top-level
    `service_type`/`port`/`node_port` fields (service_config wins whenever
    present - see generate_service's own "new format vs legacy format"
    branch). Modeled properly rather than dropped after node_port extraction,
    since later golden-file Terraform-diff testing needs this to genuinely
    match what's currently deployed."""

    model_config = ConfigDict(extra="forbid")
    type: ServiceType = ServiceType.cluster_ip
    ports: list[ServicePort] = Field(default_factory=list)
    node_port: int | None = None  # bare service.node_port shape - not seen in the 26 real apps, but compat.py's normalize_node_port defends against it


class RegistryStorageDriverHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    interval: str = "10s"
    threshold: int = 3


class RegistryHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storagedriver: RegistryStorageDriverHealth | None = None


class RegistryHttp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    addr: str = ":5000"
    headers: dict[str, list[str]] = Field(default_factory=dict)
    timeout: str | None = None
    read_timeout: str | None = None
    write_timeout: str | None = None
    drain_timeout: str | None = None
    max_connections: int | None = None
    max_idle_connections: int | None = None
    max_idle_connections_per_host: int | None = None


class RegistryLogFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str | None = None


class RegistryLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str = "info"
    fields: RegistryLogFields | None = None


class RegistryFilesystemStorage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rootdirectory: str
    maxthreads: int | None = None


class RegistryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False


class RegistryStorage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filesystem: RegistryFilesystemStorage | None = None
    delete: RegistryDelete | None = None


class RegistryConfig(BaseModel):
    """The Docker Registry v2 config schema (registry_config: block) -
    currently only docker-registry/app.yml uses this, but it's a real,
    general Docker Registry v2 config shape, not homelab-specific."""

    model_config = ConfigDict(extra="forbid")
    version: float = 0.1
    log: RegistryLog | None = None
    storage: RegistryStorage | None = None
    http: RegistryHttp | None = None
    health: RegistryHealth | None = None


class ExternalEndpointPort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    port: int
    protocol: str = "TCP"  # always TCP here - no UDP external endpoint has ever been needed


class ExternalEndpoint(BaseModel):
    """`app_type: external`'s only real config: a bare LAN host (an ESP32, a
    NAS, any device that can never run as a pod in this cluster) plus the
    ports it really listens on. The generator turns this into the standard
    k8s "Service without selector" pattern - a selector-less Service plus a
    hand-specified Endpoints resource pointing straight at `host` - so the
    device gets a clean in-cluster DNS name without ever being a workload."""

    model_config = ConfigDict(extra="forbid")
    host: str
    ports: list[ExternalEndpointPort]


class AppConfig(BaseModel):
    """The root schema. `extra='forbid'` deliberately - an unrecognized field
    in app.yml (typo, stale key from a schema migration) fails loudly at
    `cube_manifest validate` instead of being silently ignored the way the old
    config_parser.py's plain dict-based parsing did."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # A real, but confirmed-dead, duplicate of `name` - only rabbitmq/app.yml
    # sets it (app_name: rabbitmq, identical to name: rabbitmq) and nothing in
    # terraform_generator.py or config_parser.py ever reads config['app_name']
    # (every `app_name` in that codebase is a local variable/function
    # parameter derived from config['name'], never a raw-dict lookup).
    app_name: str | None = None
    enabled: bool
    app_type: AppType = AppType.service
    replicas: int = 1
    namespace: str = "dev"
    service_type: ServiceType = ServiceType.cluster_ip
    port: int = 80
    node_port: int | None = None
    dependencies: list[str] = Field(default_factory=list)
    image: ImageConfig = Field(default_factory=ImageConfig)
    image_config: ImageSpec | None = None
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = None
    image_pull_timeout: int | None = None
    resources: Resources = Field(default_factory=Resources)
    resource_profile: ResourceProfile | None = None
    docker_config: DockerConfig = Field(default_factory=DockerConfig)
    microservice_config: MicroserviceConfig | None = None
    environment: list[EnvVar] = Field(default_factory=list)
    secrets: dict[str, str] = Field(default_factory=dict)
    secrets_as_env_vars: bool = False
    rbac: Rbac = Field(default_factory=Rbac)
    storage: list[StorageEntry] = Field(default_factory=list)
    ingress: IngressConfig = Field(default_factory=IngressConfig)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    infrastructure_config: InfrastructureConfig = Field(default_factory=InfrastructureConfig)
    scaling: Scaling = Field(default_factory=Scaling)
    health_check: HealthCheck = Field(default_factory=HealthCheck)
    security_context: SecurityContext = Field(default_factory=SecurityContext)
    init_containers: list[InitContainer] = Field(default_factory=list)
    vps_route: VpsRoute | None = None
    external_repo: ExternalRepo | None = None
    deployment_strategy: DeploymentStrategy | None = None
    description: str | None = None
    version: str | None = None
    expose_service: bool | None = None
    host_network: bool | None = None
    restart_policy: Literal["Always", "OnFailure", "Never"] | None = None
    termination_grace_period: int | None = None
    cleanup: CleanupConfig | None = None
    configmap: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    monitoring: MonitoringConfig | None = None
    container: ContainerSpec | None = None
    container_config: ContainerConfig | None = None
    container_security_context: SecurityContext | None = None
    service: ServiceSpec | None = None
    registry_config: RegistryConfig | None = None
    external_endpoint: ExternalEndpoint | None = None

    @field_validator("name")
    @classmethod
    def name_dns_label(cls, v: str) -> str:
        return _validate_dns_label(v, "name")

    @field_validator("dependencies")
    @classmethod
    def dependencies_non_empty_strings(cls, v: list[str]) -> list[str]:
        for dep in v:
            if not dep or not isinstance(dep, str):
                raise ValueError(f"dependencies entries must be non-empty strings, got {dep!r}")
        return v

    @model_validator(mode="after")
    def microservice_config_required_for_microservice(self) -> AppConfig:
        if self.app_type == AppType.microservice and self.microservice_config is None:
            self.microservice_config = MicroserviceConfig()
        return self

    @model_validator(mode="after")
    def external_endpoint_required_for_external(self) -> AppConfig:
        # Unlike microservice_config above, there's no sane default to
        # auto-fill here (host has no meaningful default) - app.yml must
        # supply it explicitly.
        if self.app_type == AppType.external and self.external_endpoint is None:
            raise ValueError("app_type is 'external' but external_endpoint is not set")
        # Deliberately NOT rejecting docker_config/scaling/storage/resources
        # etc. when app_type is external - matching this codebase's existing
        # style for the other non-pod-backed app_type (microservice, whose
        # generator also never reads docker_config/storage/scaling/resources
        # even though the schema still accepts them). The generator simply
        # never calls the pod/Deployment/PVC/HPA builders for this app_type,
        # the same way it already doesn't for microservice/job.
        return self

    @model_validator(mode="after")
    def default_queue_name(self) -> AppConfig:
        if self.microservice_config is not None and self.microservice_config.queue_name is None:
            self.microservice_config.queue_name = f"{self.name}-queue"
        return self
