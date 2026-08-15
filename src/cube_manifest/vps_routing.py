"""Optional VPS public-routing support: registers a live Caddy path route on
a remote VPS for any app.yml declaring `vps_route: {path_prefix, host}`.

Entirely optional. If VPS connection details aren't configured (see
`VpsRoutingConfig.load()`), every call here is a no-op with a one-line log
message - never a hard failure, since most apps and most environments
(anyone without this specific VPS) have no use for it at all. Configuration
lives OUTSIDE this repo - environment variables, or an optional file at
`~/.config/cube-manifest/vps-routing.yaml` - never a hardcoded credential
in source. That's a deliberate fix, not a style choice: the previous
version of this mechanism (`simple-deploy/register_vps_route.py`, deleted
when app-generator/simple-deploy were replaced by cube-manifest) had a real
plaintext SSH password committed directly in that .py file.

Security note (why this is a rewrite, not a restore): that same old script
also built a REMOTE SHELL COMMAND STRING by f-string-interpolating a `path`
value straight into it before handing the whole thing to `ssh` - a genuine
command-injection vulnerability if that value ever contained shell
metacharacters (caught by a 2026-07-30 security audit, "SSH command
injection in the VPS routing plugin"). This version never builds a dynamic
remote command string at all. The remote-side script text
(`_REMOTE_SCRIPT`) is a FIXED constant, identical on every call; the only
things that vary per-call (HTTP method, path, JSON body) travel over
**stdin**, and the fixed script consumes them only via double-quoted POSIX
shell variable expansion (`"$path"`, `"$method"`) - which treats a
variable's *content* as inert data, never re-parsed as shell syntax, no
matter what characters it contains. `path_prefix` is also still restricted
to a safe slug pattern at the schema layer (`models.py`'s `PATH_PREFIX_RE`)
as defense-in-depth, not as the only safeguard - and this module applies
the same slug check to the app *name* too, since that also flows into the
constructed route id and isn't schema-constrained the same way.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .schema.models import AppConfig

# Matches VpsRoute.path_prefix's own schema-level validator exactly - applied
# again here to the app *name* (which also ends up in the constructed route
# id), since app names aren't regex-constrained by the schema the way
# path_prefix is.
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# The home cluster's own Tailscale IP - stable, non-secret infrastructure
# info already visible throughout the VPS's own Caddyfile history. Still
# overridable via env var for anyone running this against a different
# cluster.
_DEFAULT_HOME_TAILSCALE_IP = "100.127.98.1"

# Every dynamic value (method, path, body) travels over stdin - this text
# never changes based on any app.yml/user-controlled value, so there is
# nothing here for an attacker-controlled string to inject into.
#
# Deliberately uses ONLY double quotes internally (never a single quote) -
# this whole string gets wrapped in single quotes for `sh -c '...'` on the
# ssh command line, and a single quote embedded inside that would close the
# outer quoting early and mangle the command (a real bug caught by live
# testing against the actual VPS, not just unit tests - the injection-proof
# stdin design doesn't by itself guarantee valid shell syntax).
_REMOTE_SCRIPT = (
    "read -r method; "
    "read -r path_b64; "
    'path=$(printf "%s" "$path_b64" | base64 -d); '
    'curl -s -w "\\nHTTP:%{http_code}" -X "$method" '
    '-H "Content-Type: application/json" --data-binary @- '
    '"http://127.0.0.1:2019$path"'
)


@dataclass
class VpsRoutingConfig:
    ssh_host: str
    ssh_port: int
    ssh_user: str
    caddy_container: str
    default_host: str
    home_tailscale_ip: str
    identity_file: str | None = None
    password: str | None = None

    @classmethod
    def load(cls) -> VpsRoutingConfig | None:
        """Env vars win over the optional local config file; either source
        being incomplete (missing host/user) means the feature is disabled
        for this run - callers must treat None as "skip silently", not an
        error."""
        env = os.environ
        file_cfg = _load_config_file()

        def get(key: str, default: str | None = None) -> str | None:
            return env.get(f"CUBE_MANIFEST_VPS_{key}") or file_cfg.get(key.lower()) or default

        ssh_host = get("SSH_HOST")
        ssh_user = get("SSH_USER")
        if not ssh_host or not ssh_user:
            return None

        identity_file = get("SSH_IDENTITY_FILE")
        password = get("SSH_PASSWORD")
        if not identity_file and not password:
            return None

        return cls(
            ssh_host=ssh_host,
            ssh_port=int(get("SSH_PORT", "22") or "22"),
            ssh_user=ssh_user,
            caddy_container=get("CADDY_CONTAINER", "advogadosx-caddy-1") or "advogadosx-caddy-1",
            default_host=get("DEFAULT_HOST", "cybertechnology.sh") or "cybertechnology.sh",
            home_tailscale_ip=get("HOME_TAILSCALE_IP", _DEFAULT_HOME_TAILSCALE_IP) or _DEFAULT_HOME_TAILSCALE_IP,
            identity_file=identity_file,
            password=password,
        )


def _config_file_path() -> Path:
    return Path.home() / ".config" / "cube-manifest" / "vps-routing.yaml"


def _load_config_file() -> dict:
    path = _config_file_path()
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


class VpsRoutingError(Exception):
    pass


def _slug_safe(value: str, *, what: str) -> str:
    if not _SAFE_SLUG_RE.match(value):
        raise VpsRoutingError(
            f"{what} {value!r} contains characters outside [a-zA-Z0-9_-] - refusing to use it in a VPS route."
        )
    return value


def _ssh_argv(config: VpsRoutingConfig) -> list[str]:
    """Builds the LOCAL argv list for the ssh client itself - every element
    here is a fixed literal or a config value the user themselves supplied
    (host/port/user/identity file path), never app.yml/attacker-influenced
    data. This is a real argv list (shell=False downstream), not a shell
    string - no quoting/injection concerns for these values either."""
    argv = ["ssh", "-o", "StrictHostKeyChecking=no", "-p", str(config.ssh_port)]
    if config.identity_file:
        argv += ["-i", config.identity_file]
    argv += [f"{config.ssh_user}@{config.ssh_host}"]
    return argv


def _run_remote(config: VpsRoutingConfig, method: str, path: str, body: dict | None) -> tuple[bool, str]:
    """Runs the fixed remote script over SSH, feeding method/path/body via
    stdin. `method` is always one of a small literal set this module itself
    chooses (never app.yml data); `path` has already been through
    _slug_safe/the schema's own PATH_PREFIX_RE by the time it reaches here."""
    path_b64 = base64.b64encode(path.encode()).decode()
    stdin_payload = f"{method}\n{path_b64}\n" + (json.dumps(body) if body is not None else "")

    remote_cmd = f"docker exec -i {config.caddy_container} sh -c '{_REMOTE_SCRIPT}'"
    # remote_cmd is fixed except for config.caddy_container, which is the
    # user's OWN config value (not app.yml data) - still, keep it slug-safe
    # so a copy-pasted config typo can't do anything unexpected either.
    _slug_safe(config.caddy_container, what="caddy_container")

    argv = _ssh_argv(config) + [remote_cmd]
    env = dict(os.environ)
    if config.password and not config.identity_file:
        argv = ["sshpass", "-p", config.password] + argv

    result = subprocess.run(
        argv,
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    output = result.stdout + result.stderr
    ok = result.returncode == 0 and "HTTP:200" in output
    return ok, output


def _route_id(app_name: str) -> str:
    return f"home-route-{_slug_safe(app_name, what='app name')}"


def _build_route(route_id: str, host: str, path_prefix: str, target: str) -> dict:
    prefix = "/" + path_prefix.strip("/")
    return {
        "@id": route_id,
        "match": [{"host": [host], "path": [prefix, prefix + "/*"]}],
        "handle": [
            {"handler": "rewrite", "strip_path_prefix": prefix},
            {"handler": "reverse_proxy", "upstreams": [{"dial": target}]},
        ],
        "terminal": True,
    }


def sync_route(app: AppConfig, config: VpsRoutingConfig) -> tuple[bool, str]:
    """Registers (or updates) app.vps_route as a live Caddy route on the
    configured VPS. Returns (ok, message) - callers should log this, not
    raise the process's exit code on failure, since a VPS routing hiccup
    should never block or fail an otherwise-successful `cube apply`."""
    if app.vps_route is None:
        return True, "no vps_route configured - nothing to sync"
    if app.node_port is None:
        return False, "vps_route is set but no node_port is configured - cannot derive a target"

    host = app.vps_route.host or config.default_host
    path_prefix = app.vps_route.path_prefix  # already regex-validated by the schema
    route_id = _route_id(app.name)
    target = f"{config.home_tailscale_ip}:{app.node_port}"
    route = _build_route(route_id, host, path_prefix, target)

    exists_ok, _exists_out = _run_remote(config, "GET", f"/id/{route_id}", None)
    if exists_ok:
        ok, out = _run_remote(config, "PATCH", f"/id/{route_id}", route)
        action = "Updated"
    else:
        ok, out = _run_remote(config, "PUT", "/config/apps/http/servers/srv0/routes/0", route)
        action = "Inserted"

    if not ok:
        return False, f"FAILED to register VPS route for {app.name}: {out}"
    return True, f"{action} VPS route '{route_id}': {host}{path_prefix}/* -> {target}"


def unregister_route(app_name: str, config: VpsRoutingConfig) -> tuple[bool, str]:
    route_id = _route_id(app_name)
    ok, out = _run_remote(config, "DELETE", f"/id/{route_id}", None)
    if not ok:
        return False, f"FAILED to remove VPS route (may not exist): {out}"
    return True, f"Removed VPS route '{route_id}'"
