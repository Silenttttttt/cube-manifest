"""Golden-file-style tests for kdeploy.generators.dockerfile.

Covers the HIGH-severity finding driving this generator's existence: the old
dockerfile_generator.py only gave rust/go/java a real multi-stage build,
which meant a git deploy SSH key injected via ARG/ENV for python/node/generic
apps ended up baked into the FINAL pushed image, extractable by anyone who
could pull it. Every assertion here is really checking one of:

  - every language gets >= 2 real FROM stages (no single-stage escape hatch)
  - a secret ever needed (external_repo + a git@/ssh:// URL) is wired in via
    `--mount=type=secret`, never ARG/ENV
  - the runtime stage's COPY --from=builder lines never reference the
    secret's mount path (which BuildKit never writes to a layer anyway, but
    a broken COPY that tried would be a real regression to catch)
  - a real Docker HEALTHCHECK is emitted iff docker_config.health_check is
    set and enabled
"""

from __future__ import annotations

import re

import pytest

from kdeploy.generators.dockerfile import generate_dockerfile
from kdeploy.generators.dockerfile.common import GIT_SSH_KEY_MOUNT_TARGET
from kdeploy.schema.models import (
    AppConfig,
    CacheOptimization,
    DockerConfig,
    DockerHealthCheck,
    ExternalRepo,
    UserConfig,
)

FROM_RE = re.compile(r"^FROM\s+\S+", re.MULTILINE)
COPY_FROM_RE = re.compile(r"^COPY\s+--from=\S+.*$", re.MULTILINE)


def _from_lines(dockerfile: str) -> list[str]:
    return FROM_RE.findall(dockerfile)


def _copy_from_lines(dockerfile: str) -> list[str]:
    return COPY_FROM_RE.findall(dockerfile)


def make_app(**overrides) -> AppConfig:
    docker_config = overrides.pop("docker_config", None) or DockerConfig()
    defaults = dict(name="testapp", enabled=True)
    defaults.update(overrides)
    return AppConfig(docker_config=docker_config, **defaults)


# --------------------------------------------------------------------------
# Per-language fixtures
# --------------------------------------------------------------------------


def python_app(**dc_overrides) -> AppConfig:
    base = dict(
        language="python",
        base_image="python:3.12-slim",
        entry_point=["python", "app.py"],
        exposed_ports=[8080],
        environment_vars={"PYTHONPATH": "/app"},
        user_config=UserConfig(user="appuser", uid=1002, gid=1002),
        health_check=DockerHealthCheck(enabled=True, command="curl -f http://localhost:8080/health || exit 1"),
        cache_optimization=CacheOptimization(enabled=True, dependency_cache_mount="/root/.cache/pip"),
    )
    base.update(dc_overrides)
    return make_app(name="python-svc", docker_config=DockerConfig(**base))


def node_app(**dc_overrides) -> AppConfig:
    base = dict(
        language="node",
        base_image="node:20-alpine",
        build_commands=["npm run build"],
        entry_point=["node", "server.js"],
        exposed_ports=[3000],
        user_config=UserConfig(user="nodeuser", uid=1001, gid=1001),
        cache_optimization=CacheOptimization(enabled=True),
    )
    base.update(dc_overrides)
    return make_app(name="node-svc", docker_config=DockerConfig(**base))


def rust_app(**dc_overrides) -> AppConfig:
    base = dict(
        language="rust",
        exposed_ports=[9000],
        cache_optimization=CacheOptimization(enabled=True),
    )
    base.update(dc_overrides)
    return make_app(name="rust-svc", docker_config=DockerConfig(**base))


def go_app(**dc_overrides) -> AppConfig:
    base = dict(
        language="go",
        exposed_ports=[8081],
        cache_optimization=CacheOptimization(enabled=True),
    )
    base.update(dc_overrides)
    return make_app(name="go-svc", docker_config=DockerConfig(**base))


def java_app(**dc_overrides) -> AppConfig:
    base = dict(
        language="java",
        exposed_ports=[8082],
        cache_optimization=CacheOptimization(enabled=True),
    )
    base.update(dc_overrides)
    return make_app(name="java-svc", docker_config=DockerConfig(**base))


LANGUAGE_FIXTURES = {
    "python": python_app,
    "node": node_app,
    "rust": rust_app,
    "go": go_app,
    "java": java_app,
}


# --------------------------------------------------------------------------
# Two-stage-ness, for every language
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(LANGUAGE_FIXTURES))
def test_at_least_two_stages(language):
    app = LANGUAGE_FIXTURES[language]()
    dockerfile = generate_dockerfile(app)
    from_lines = _from_lines(dockerfile)
    assert len(from_lines) >= 2, f"{language}: expected >=2 FROM stages, got {from_lines!r}"
    assert "AS builder" in dockerfile


@pytest.mark.parametrize("language", sorted(LANGUAGE_FIXTURES))
def test_syntax_header_present(language):
    app = LANGUAGE_FIXTURES[language]()
    dockerfile = generate_dockerfile(app)
    assert dockerfile.startswith("# syntax=docker/dockerfile:1")


def test_generic_language_also_two_stage():
    # Real precedent: apps/github-runner/app.yml sets docker_config.language:
    # docker, which isn't one of the 6 implemented languages - must still
    # fall back to a real two-stage build rather than crashing or silently
    # producing a single stage.
    app = make_app(name="unknown-lang", docker_config=DockerConfig(language="docker", base_image="ubuntu:22.04"))
    dockerfile = generate_dockerfile(app)
    assert len(_from_lines(dockerfile)) >= 2


# --------------------------------------------------------------------------
# Secret handling: external_repo + a git@/ssh:// URL must use
# --mount=type=secret, never ARG/ENV, and the runtime COPY --from=builder
# lines must never reference the secret's mount path.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(LANGUAGE_FIXTURES))
def test_external_repo_ssh_url_uses_secret_mount(language):
    fixture = LANGUAGE_FIXTURES[language]
    app = fixture()
    app = app.model_copy(
        update={
            "external_repo": ExternalRepo(
                url="git@github.com:example/private-repo.git",
                branch="main",
                ssh_key_secret="git-deploy-key",
            )
        }
    )
    dockerfile = generate_dockerfile(app)

    assert "--mount=type=secret" in dockerfile, f"{language}: expected a secret mount for the ssh clone"
    assert f"id={GIT_SSH_KEY_MOUNT_TARGET.rsplit('/', 1)[-1]}" not in dockerfile  # sanity: not matching on filename
    assert "git_ssh_key" in dockerfile

    # The actual finding: no ARG/ENV carrying key material.
    assert not re.search(r"^ARG\s+.*(SSH|KEY)", dockerfile, re.MULTILINE | re.IGNORECASE)
    assert not re.search(r"^ENV\s+.*(SSH_KEY|GIT_KEY)", dockerfile, re.MULTILINE | re.IGNORECASE)

    # The secret's mount target must never appear on a COPY --from= line -
    # BuildKit never writes a secret mount to a layer, so this also
    # double-checks the runtime stage isn't trying to copy from that path.
    for copy_line in _copy_from_lines(dockerfile):
        assert GIT_SSH_KEY_MOUNT_TARGET not in copy_line, f"{language}: {copy_line!r} references the secret path"


@pytest.mark.parametrize("language", sorted(LANGUAGE_FIXTURES))
def test_external_repo_https_url_needs_no_secret(language):
    fixture = LANGUAGE_FIXTURES[language]
    app = fixture()
    app = app.model_copy(
        update={
            "external_repo": ExternalRepo(url="https://github.com/example/public-repo.git", branch="main"),
        }
    )
    dockerfile = generate_dockerfile(app)
    # A plain https clone needs no deploy key at all - no secret machinery
    # should be wired in for it.
    assert "--mount=type=secret" not in dockerfile
    assert "git clone" in dockerfile


# --------------------------------------------------------------------------
# COPY --from=builder never carries the whole builder filesystem, and never
# the secret mount path, even without external_repo involved.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(LANGUAGE_FIXTURES))
def test_runtime_copy_never_references_secret_path(language):
    app = LANGUAGE_FIXTURES[language]()
    dockerfile = generate_dockerfile(app)
    for copy_line in _copy_from_lines(dockerfile):
        assert GIT_SSH_KEY_MOUNT_TARGET not in copy_line


# --------------------------------------------------------------------------
# HEALTHCHECK
# --------------------------------------------------------------------------


def test_healthcheck_emitted_when_enabled():
    app = python_app(health_check=DockerHealthCheck(enabled=True, command="curl -f http://localhost:8080/ || exit 1"))
    dockerfile = generate_dockerfile(app)
    assert "HEALTHCHECK" in dockerfile
    assert "curl -f http://localhost:8080/" in dockerfile


def test_healthcheck_omitted_when_disabled():
    app = python_app(health_check=DockerHealthCheck(enabled=False))
    dockerfile = generate_dockerfile(app)
    assert "HEALTHCHECK" not in dockerfile


def test_healthcheck_omitted_when_unset():
    dc = DockerConfig(language="python", base_image="python:3.12-slim", entry_point=["python", "app.py"])
    app = make_app(name="no-hc", docker_config=dc)
    dockerfile = generate_dockerfile(app)
    assert "HEALTHCHECK" not in dockerfile


@pytest.mark.parametrize("language", sorted(LANGUAGE_FIXTURES))
def test_healthcheck_list_command_uses_exec_form(language):
    fixture = LANGUAGE_FIXTURES[language]
    app = fixture(health_check=DockerHealthCheck(enabled=True, command=["python", "-c", "import sys; sys.exit(0)"]))
    dockerfile = generate_dockerfile(app)
    assert 'HEALTHCHECK' in dockerfile
    assert '["python", "-c", "import sys; sys.exit(0)"]' in dockerfile


# --------------------------------------------------------------------------
# USER (non-root by default)
# --------------------------------------------------------------------------


def test_non_root_user_directive_present_by_default():
    app = python_app()
    dockerfile = generate_dockerfile(app)
    assert "USER appuser" in dockerfile


def test_root_when_create_user_false():
    app = python_app(user_config=UserConfig(create_user=False))
    dockerfile = generate_dockerfile(app)
    assert "USER " not in dockerfile
    assert "Running as root" in dockerfile


# --------------------------------------------------------------------------
# Generic app with its own handwritten Dockerfile (case 6: passthrough)
# --------------------------------------------------------------------------


def test_own_dockerfile_passthrough(tmp_path):
    app_dir = tmp_path / "local-storage"
    app_dir.mkdir()
    dockerfile_content = "FROM scratch\nCOPY . .\nCMD [\"/app/bin\"]\n"
    (app_dir / "Dockerfile").write_text(dockerfile_content)

    dc = DockerConfig(dockerfile="Dockerfile", context=".")
    app = make_app(name="local-storage", docker_config=dc)

    result = generate_dockerfile(app, app_dir=app_dir)
    assert result == dockerfile_content


def test_own_dockerfile_with_context_subdir(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "sub").mkdir(parents=True)
    dockerfile_content = "FROM alpine\n"
    (app_dir / "sub" / "Dockerfile.custom").write_text(dockerfile_content)

    dc = DockerConfig(dockerfile="Dockerfile.custom", context="sub")
    app = make_app(name="withctx", docker_config=dc)

    result = generate_dockerfile(app, app_dir=app_dir)
    assert result == dockerfile_content


def test_own_dockerfile_missing_file_raises(tmp_path):
    app_dir = tmp_path / "missing"
    app_dir.mkdir()
    dc = DockerConfig(dockerfile="Dockerfile", context=".")
    app = make_app(name="missing-df", docker_config=dc)

    with pytest.raises(FileNotFoundError):
        generate_dockerfile(app, app_dir=app_dir)


def test_own_dockerfile_no_app_dir_raises():
    dc = DockerConfig(dockerfile="Dockerfile", context=".")
    app = make_app(name="no-dir", docker_config=dc)
    with pytest.raises(ValueError):
        generate_dockerfile(app)


def test_own_dockerfile_does_not_get_generated_content(tmp_path):
    """An app opting out of generation must never see the generator's own
    two-stage scaffolding - only its own file's content."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "Dockerfile").write_text("FROM busybox\n")
    dc = DockerConfig(language="python", dockerfile="Dockerfile", context=".")
    app = make_app(name="opted-out", docker_config=dc)
    result = generate_dockerfile(app, app_dir=app_dir)
    assert result == "FROM busybox\n"
    assert "AS builder" not in result


# --------------------------------------------------------------------------
# Node's nginx-serving special case (real precedent: apps/paipai-ui/app.yml)
# --------------------------------------------------------------------------


def test_node_nginx_runtime_uses_nginx_base_image():
    dc = DockerConfig(
        language="node",
        base_image="node:18-alpine",
        system_dependencies=["nginx", "curl"],
        build_commands=["npm ci", "npm run build"],
        entry_point=["sh", "-c", "nginx -g 'daemon off;'"],
        exposed_ports=[3000],
    )
    app = make_app(name="paipai-ui", docker_config=dc)
    dockerfile = generate_dockerfile(app)
    assert "FROM nginx:alpine" in dockerfile
    assert "/usr/share/nginx/html" in dockerfile


# --------------------------------------------------------------------------
# Cache mounts actually honor cache_optimization overrides (old system
# silently ignored these fields beyond .enabled)
# --------------------------------------------------------------------------


def test_custom_cache_mount_path_is_honored():
    app = python_app(cache_optimization=CacheOptimization(enabled=True, dependency_cache_mount="/custom/pip-cache"))
    dockerfile = generate_dockerfile(app)
    assert "--mount=type=cache,target=/custom/pip-cache" in dockerfile


def test_cache_disabled_emits_no_cache_mounts():
    app = python_app(cache_optimization=CacheOptimization(enabled=False))
    dockerfile = generate_dockerfile(app)
    assert "--mount=type=cache" not in dockerfile
