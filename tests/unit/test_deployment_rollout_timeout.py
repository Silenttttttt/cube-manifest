"""build_deployment's terraform apply timeout now derives from a single real
value (DeploymentStrategy.progress_deadline_seconds) instead of an
independently hardcoded constant - see workloads.py's own comment for the
real incident (chat-server's CI deploy step failing on every push because
terraform gave up at a hardcoded 5m while Kubernetes' own default
progressDeadlineSeconds is 600s/10m, with nothing keeping the two in sync)."""

from __future__ import annotations

from cube_manifest.generators.terraform.workloads import build_deployment
from cube_manifest.schema.models import AppConfig, DeploymentStrategy


def _deployment_body(app: AppConfig, app_name: str = "test-app") -> dict:
    result = build_deployment(app, app_name)
    return result["resource"]["kubernetes_deployment"][app_name]


def test_default_timeout_derives_from_k8s_own_progress_deadline_default():
    app = AppConfig(name="test-app", enabled=True)
    body = _deployment_body(app)
    # 600s (k8s's own real default) + 120s buffer = 720s = exactly 12m.
    assert body["timeouts"] == {"create": "12m", "update": "12m", "delete": "2m"}


def test_default_case_does_not_set_progress_deadline_seconds_explicitly():
    """Only ever set the field on the real spec when an app.yml explicitly
    asks for a non-default value - leaving it unset for every other app
    means Kubernetes' own default (600s) governs, exactly as it always has,
    rather than every generated Deployment suddenly pinning a value that
    used to be implicit."""
    app = AppConfig(name="test-app", enabled=True)
    body = _deployment_body(app)
    assert "progress_deadline_seconds" not in body["spec"]


def test_explicit_progress_deadline_seconds_is_set_on_the_real_spec():
    app = AppConfig(name="slow-app", enabled=True, deployment_strategy=DeploymentStrategy(progress_deadline_seconds=1800))
    body = _deployment_body(app, "slow-app")
    assert body["spec"]["progress_deadline_seconds"] == 1800


def test_terraform_timeout_scales_with_an_explicit_progress_deadline_seconds():
    app = AppConfig(name="slow-app", enabled=True, deployment_strategy=DeploymentStrategy(progress_deadline_seconds=1800))
    body = _deployment_body(app, "slow-app")
    # 1800s + 120s buffer = 1920s = exactly 32m.
    assert body["timeouts"]["create"] == "32m"
    assert body["timeouts"]["update"] == "32m"


def test_delete_timeout_is_never_derived_from_rollout_patience():
    """A delete doesn't wait on any rollout at all - tying it to
    progress_deadline_seconds would just make deletes slower for no reason,
    regardless of how patient an app's create/update timeout is."""
    app = AppConfig(name="slow-app", enabled=True, deployment_strategy=DeploymentStrategy(progress_deadline_seconds=3600))
    body = _deployment_body(app, "slow-app")
    assert body["timeouts"]["delete"] == "2m"


def test_rounds_up_to_a_whole_minute_rather_than_truncating():
    # 601s + 120s = 721s -> must round UP to 13m, not truncate down to 12m
    # (truncating would silently give BACK less patience than configured).
    app = AppConfig(name="odd-app", enabled=True, deployment_strategy=DeploymentStrategy(progress_deadline_seconds=601))
    body = _deployment_body(app, "odd-app")
    assert body["timeouts"]["create"] == "13m"
