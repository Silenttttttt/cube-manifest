"""Unit tests for deploy.restart_and_wait - the step `cube ship` (and,
manually, anyone redeploying an already-running app) needs after a real
apply, since reapplying an unchanged `:latest` tag never forces existing
pods to repull it. Every `kubectl` call is mocked - no cluster needed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cube_manifest import deploy
from cube_manifest.schema.models import AppConfig, AppType


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    class _Result:
        pass

    r = _Result()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestRestartAndWait:
    def test_service_app_rolls_a_deployment(self, monkeypatch):
        app = AppConfig(name="web-app", enabled=True, app_type=AppType.service, namespace="dev")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(0)

        with patch("subprocess.run", side_effect=fake_run):
            result = deploy.restart_and_wait(app, Path("/fake/kubeconfig"))

        assert result.kind == "deployment"
        assert result.ok is True
        assert len(calls) == 2
        assert calls[0][-2:] == ["rollout", "restart"] or "restart" in calls[0]
        assert "deployment/web-app" in calls[0]
        assert "-n" in calls[0] and "dev" in calls[0]
        assert "deployment/web-app" in calls[1]
        assert any("--timeout=90s" in c for c in calls[1])

    def test_job_app_has_nothing_to_roll(self, monkeypatch):
        app = AppConfig(name="batch-job", enabled=True, app_type=AppType.job, namespace="dev")

        with patch("subprocess.run") as mock_run:
            result = deploy.restart_and_wait(app, Path("/fake/kubeconfig"))
            mock_run.assert_not_called()

        assert result.kind is None
        assert result.ok is True

    def test_restart_failure_short_circuits_before_status_check(self):
        app = AppConfig(name="web-app", enabled=True, app_type=AppType.service, namespace="dev")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(1, stderr="deployments.apps \"web-app\" not found")

        with patch("subprocess.run", side_effect=fake_run):
            result = deploy.restart_and_wait(app, Path("/fake/kubeconfig"))

        assert result.kind == "deployment"
        assert result.ok is False
        assert len(calls) == 1  # never even attempted `rollout status`

    def test_status_timeout_reports_as_failure(self):
        app = AppConfig(name="web-app", enabled=True, app_type=AppType.service, namespace="dev")
        responses = [_completed(0), _completed(1, stderr="timed out waiting for the condition")]

        def fake_run(cmd, **kwargs):
            return responses.pop(0)

        with patch("subprocess.run", side_effect=fake_run):
            result = deploy.restart_and_wait(app, Path("/fake/kubeconfig"))

        assert result.kind == "deployment"
        assert result.ok is False
