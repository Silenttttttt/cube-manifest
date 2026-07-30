"""Activator-contract annotations for scale-to-zero apps.

Generates the exact `activator.cubernetes.io/*` Deployment annotations that
the ALWAYS-ON, separately-deployed activator process
(cube-activator's src/cube_activator/scaler.py, ``AppConfig.from_deployment_annotations``)
parses back into its own runtime config. That module is the ground truth for
every key spelling and value encoding here - this file exists purely to keep
producing what it already expects, reproducing the *behavior* of the old
app-generator/terraform_generator.py::generate_deployment (lines ~415-494)
against the new, validated ``cube_manifest.schema.models.AppConfig`` shape.

Do not "clean up" a key spelling or encoding here without re-checking
scaler.py first - a silent drift is invisible until an idle app mysteriously
never wakes back up. tests/unit/test_activator_contract.py imports the real
scaler.py and round-trips this module's output through it to catch exactly
that class of bug.

Two related constants live here even though neither is *emitted* by
``build_activator_annotations``, purely so nothing downstream has to guess
the literal string:

- ``LAST_ACTIVE_ANNOTATION``: written only at runtime by the activator's own
  heartbeat (``Scaler.touch_last_active`` / ``ensure_scaled_up``), never at
  generate time. The Terraform generator needs this exact key to add a
  ``lifecycle.ignore_changes`` entry for it (mirroring
  terraform_generator.py's own ``activator_ignore_changes`` block) so
  Terraform never fights the activator over a value it doesn't own.
- ``MANAGED_LABEL_KEY``: this is a Deployment **label**, not an annotation -
  confirmed by scaler.py's own name for it (``MANAGED_LABEL``, defined
  right next to ``LAST_ACTIVE_ANNOTATION`` but never read inside
  ``from_deployment_annotations``) and by discovery.py, which finds managed
  Deployments via ``label_selector=f"{MANAGED_LABEL}=true"``. The old
  terraform_generator.py agrees: it stamps this into the Deployment's
  ``labels {}`` block, never ``annotations {}``. Callers of this module
  must put ``MANAGED_LABEL_KEY: "true"`` under ``metadata.labels`` (gated on
  the same ``is_scale_to_zero`` condition), NOT into the dict
  ``build_activator_annotations`` returns.
"""

from __future__ import annotations

from cube_manifest.schema.models import AppConfig

ANNOTATION_PREFIX = "activator.cubernetes.io"

# Runtime-only heartbeat annotation - never generated, only ever read/written
# by the activator itself. Exposed here so a Terraform generator's
# lifecycle.ignore_changes list can reference the real key instead of a
# second hardcoded string literal.
LAST_ACTIVE_ANNOTATION = f"{ANNOTATION_PREFIX}/last-active"

# A LABEL (see module docstring), not an annotation - do not add this key to
# metadata.annotations.
MANAGED_LABEL_KEY = f"{ANNOTATION_PREFIX}/managed"


def is_scale_to_zero(app: AppConfig) -> bool:
    """True iff the activator should manage this app at all.

    Mirrors terraform_generator.py's own gate exactly:
    ``scaling_config.get('min_replicas', -1) == 0`` - i.e. scale-to-zero
    triggers on ``min_replicas`` being EXACTLY 0, nothing else (not "falsy",
    not "unset"). An app with no ``scaling:`` block at all gets
    ``Scaling(min_replicas=1)`` by the new schema's own default, matching
    the old dict-based config's ``-1`` sentinel default in never equaling
    0 - both encodings agree an absent ``scaling:`` block means "not
    activator-managed".
    """
    return app.scaling.min_replicas == 0


def build_activator_annotations(app: AppConfig) -> dict[str, str]:
    """Returns the ``activator.cubernetes.io/*`` annotation dict for one app,
    exactly as the real activator's ``scaler.py`` expects to parse it back.

    Returns ``{}`` (no keys at all - not present-but-empty) whenever
    ``is_scale_to_zero(app)`` is False, matching terraform_generator.py: when
    its own ``activation_enabled`` is False, the entire
    ``activator_annotations`` HEREDOC fragment is skipped, so none of these
    keys ever reach ``metadata.annotations``. scaler.py's own
    ``.get(key, default)`` fallbacks are what would apply if such a
    Deployment were ever (incorrectly) treated as activator-managed.

    For a scale-to-zero app, all 11 keys ``scaler.py`` parses are ALWAYS
    present, even when the underlying value is the empty string (e.g.
    extra-backend-ports / queue-name / depends-on for an app that sets no
    extra ports / no queue / no dependencies) - this matches the old
    system, which unconditionally interpolates every key into its Terraform
    string with no per-key conditional omission.
    """
    if not is_scale_to_zero(app):
        return {}

    scaling = app.scaling
    activation = scaling.activation  # may be None - schema only warns, doesn't require it

    activation_type = activation.type.value if activation is not None else "http"

    # Backend port precedence - matches terraform_generator.py's
    # generate_deployment exactly: an explicit activation.port wins, then
    # docker_config's first exposed port, then service.ports[0].port, then
    # the top-level `port:` field last (both the old config_parser.py and
    # this schema default that field to 80 whether or not app.yml sets it,
    # so it can't be trusted as a signal of real intent and must be checked
    # last, same as generate_service()'s own legacy-port derivation).
    if activation is not None and activation.port is not None:
        backend_port = activation.port
    elif app.docker_config.exposed_ports:
        backend_port = app.docker_config.exposed_ports[0]
    elif app.service is not None and app.service.ports:
        backend_port = app.service.ports[0].port
    else:
        backend_port = app.port

    extra_ports = activation.extra_ports if activation is not None else []
    extra_ports_str = ",".join(str(p) for p in extra_ports)

    if activation is not None and activation.queue is not None:
        queue_name = activation.queue.name
        queue_host = activation.queue.host
    else:
        queue_name = ""
        queue_host = "rabbitmq-service"

    depends_on_str = ",".join(app.dependencies)

    write_protected = "true" if scaling.write_protected else "false"

    return {
        f"{ANNOTATION_PREFIX}/activation-type": activation_type,
        f"{ANNOTATION_PREFIX}/backend-service": f"{app.name}-backend-service",
        f"{ANNOTATION_PREFIX}/backend-port": str(backend_port),
        f"{ANNOTATION_PREFIX}/extra-backend-ports": extra_ports_str,
        f"{ANNOTATION_PREFIX}/hpa-max-replicas": str(scaling.max_replicas),
        f"{ANNOTATION_PREFIX}/hpa-target-cpu-percentage": str(scaling.target_cpu_utilization_percentage),
        f"{ANNOTATION_PREFIX}/idle-timeout-seconds": str(scaling.idle_timeout_seconds),
        f"{ANNOTATION_PREFIX}/queue-name": queue_name,
        f"{ANNOTATION_PREFIX}/queue-host": queue_host,
        f"{ANNOTATION_PREFIX}/write-protected": write_protected,
        f"{ANNOTATION_PREFIX}/depends-on": depends_on_str,
    }
