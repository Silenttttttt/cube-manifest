"""One builder function per real language, each returning a complete
two-stage Dockerfile string. Every builder shares the same shape:

    builder stage  - full toolchain, installs deps, builds/compiles
    runtime stage  - minimal base image, COPY --from=builder only the
                     specific artifact(s) it needs (never the whole
                     builder filesystem, never a secret-mount path)

`generic` (and anything not in LANGUAGE_BUILDERS, e.g. the real
`language: docker` app in the 26 apps/*/app.yml, which just wraps an
already-built image) fall back to a plain apt/apk two-stage build that
still respects build_commands/entry_point - it just has no language-specific
dependency-file/build-artifact knowledge to apply.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from manifest.schema.models import AppConfig

from . import common

DEFAULT_WORKDIR = "/app"


def _workdir(app: AppConfig) -> str:
    return app.docker_config.working_dir or DEFAULT_WORKDIR


def _fetch_source_block(app: AppConfig, working_dir: str) -> str:
    """Either clone app.external_repo straight into working_dir (no local
    build context exists for this app - the repo itself IS the source), or
    COPY the local build context in, matching whichever the app actually
    has."""
    if app.external_repo is not None:
        return common.external_repo_clone_block(app, working_dir)
    return "COPY . ."


def build_python(app: AppConfig, app_dir: Path | None = None) -> str:
    dc = app.docker_config
    working_dir = _workdir(app)
    base_image = dc.base_image or "python:3.12-slim"

    # Real precedent from the old generator: requirements.txt pinning a
    # source (non-binary) psycopg2 build needs libpq-dev+gcc in the BUILDER
    # only - psycopg2-binary already ships a compiled wheel and needs none
    # of this. Best-effort: only checked when a real app_dir is available.
    extra_build_deps: list[str] = []
    if app_dir is not None:
        req_file = app_dir / "requirements.txt"
        if req_file.is_file():
            content = req_file.read_text()
            if "psycopg2==" in content and "psycopg2-binary" not in content:
                extra_build_deps = ["libpq-dev", "gcc"]

    builder_body = [
        f"WORKDIR {working_dir}",
        common.package_install_block(
            base_image, ["build-essential"] + dc.build_dependencies + extra_build_deps
        ),
        _fetch_source_block(app, working_dir),
        common.run_block(
            common.cache_mount_flags(dc.cache_optimization, "/root/.cache/pip"),
            [
                f"python -m venv {working_dir}/.venv",
                f". {working_dir}/.venv/bin/activate",
                "pip install --no-cache-dir --upgrade pip",
                "pip install --no-cache-dir -r requirements.txt",
            ],
        ),
        "\n".join(f"RUN {cmd}" for cmd in dc.build_commands) if dc.build_commands else "",
    ]

    runtime_body = [
        common.package_install_block(base_image, ["ca-certificates"] + dc.runtime_dependencies),
        common.env_block(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "PATH": f"{working_dir}/.venv/bin:$PATH",
                **dc.environment_vars,
            }
        ),
        f"WORKDIR {working_dir}",
        f"COPY --from=builder {working_dir}/.venv {working_dir}/.venv",
        f"COPY --from=builder {working_dir} {working_dir}",
        # Real precedent: apps/jobber/app.yml uses build_commands to install
        # kubectl into /usr/local/bin - a tool the running jobber.py process
        # itself invokes (its RBAC grants create/delete/watch on pods), not a
        # build-time-only dependency. Builder and runtime share the exact
        # same base_image here, so /usr/local/bin already exists in both -
        # this only ever adds what build_commands put there, never anything
        # from the discarded builder toolchain (apt packages install
        # elsewhere, not there) and never a secret-mount path.
        "COPY --from=builder /usr/local/bin /usr/local/bin" if dc.build_commands else "",
        common.user_block(dc.user_config, base_image),
        common.expose_block(dc.exposed_ports),
        common.healthcheck_block(dc.health_check),
        common.entrypoint_and_cmd(app),
    ]

    return common.render_two_stage(base_image, builder_body, base_image, runtime_body)


def build_node(app: AppConfig) -> str:
    dc = app.docker_config
    working_dir = _workdir(app)
    base_image = dc.base_image or "node:20-slim"
    serves_via_nginx = "nginx" in dc.system_dependencies

    builder_body = [
        f"WORKDIR {working_dir}",
        common.package_install_block(base_image, dc.build_dependencies),
        _fetch_source_block(app, working_dir),
        common.run_block(
            common.cache_mount_flags(dc.cache_optimization, "/root/.npm"),
            [
                " ".join(f"{k}={v}" for k, v in dc.install_env.items()) + " npm ci"
                if dc.install_env
                else "npm ci"
            ],
        ),
    ]
    if dc.build_commands:
        builder_body.append(
            common.run_block(common.cache_mount_flags(dc.cache_optimization, "/root/.npm"), dc.build_commands)
        )

    if serves_via_nginx:
        runtime_from = "nginx:alpine"
        runtime_body = [
            f"COPY --from=builder {working_dir}/dist /usr/share/nginx/html",
            common.expose_block(dc.exposed_ports or [80]),
            common.healthcheck_block(dc.health_check),
            common.entrypoint_and_cmd(app, default_cmd=["nginx", "-g", "daemon off;"]),
        ]
        # nginx base images already run as an unprivileged worker process
        # internally - docker_config.user_config's USER directive is skipped
        # here since it would break nginx's own privilege-drop handling for
        # binding ports / writing its pidfile, not because the config was
        # ignored.
    else:
        runtime_from = base_image
        runtime_body = [
            common.package_install_block(runtime_from, ["ca-certificates"] + dc.runtime_dependencies),
            common.env_block(dc.environment_vars),
            f"WORKDIR {working_dir}",
            f"COPY --from=builder {working_dir}/node_modules {working_dir}/node_modules",
            f"COPY --from=builder {working_dir} {working_dir}",
            common.user_block(dc.user_config, runtime_from),
            common.expose_block(dc.exposed_ports),
            common.healthcheck_block(dc.health_check),
            common.entrypoint_and_cmd(app),
        ]

    return common.render_two_stage(base_image, builder_body, runtime_from, runtime_body)


def build_rust(app: AppConfig) -> str:
    dc = app.docker_config
    working_dir = _workdir(app)
    builder_base = dc.base_image or "rust:1.75-slim"
    # Debian-based runtime regardless of the builder's own base image - a
    # glibc binary built in a musl/Alpine builder won't run on Debian and
    # vice versa, so this deliberately does NOT follow builder_base the way
    # python/node do; matches the old generator's own hardcoded choice here.
    runtime_base = "debian:bookworm-slim"
    # Real Cargo project-name knowledge doesn't exist in app.yml - assumed
    # to equal app.name (already a DNS-safe, filename-safe identifier); the
    # old generator's own fallback here was a bare `target/release/*` glob
    # copy into /usr/local/bin, which is no more principled than this and
    # additionally risks pulling in `.d` dep-info files/build-script
    # artifacts alongside the real binary.
    binary_name = app.name

    builder_body = [
        f"WORKDIR {working_dir}",
        common.package_install_block(
            builder_base, ["pkg-config", "libssl-dev"] + dc.build_dependencies
        ),
        _fetch_source_block(app, working_dir),
        common.run_block(
            common.cache_mount_flags(
                dc.cache_optimization, "/usr/local/cargo/registry", f"{working_dir}/target"
            ),
            (dc.build_commands or ["cargo build --release"]),
        ),
    ]

    runtime_body = [
        common.package_install_block(runtime_base, ["ca-certificates", "libssl3"] + dc.runtime_dependencies),
        common.env_block(dc.environment_vars),
        f"COPY --from=builder {working_dir}/target/release/{binary_name} /usr/local/bin/{binary_name}",
        common.user_block(dc.user_config, runtime_base),
        common.expose_block(dc.exposed_ports),
        common.healthcheck_block(dc.health_check),
        common.entrypoint_and_cmd(app, default_cmd=[f"/usr/local/bin/{binary_name}"]),
    ]

    return common.render_two_stage(builder_base, builder_body, runtime_base, runtime_body)


def build_go(app: AppConfig) -> str:
    dc = app.docker_config
    working_dir = _workdir(app)
    builder_base = dc.base_image or "golang:1.21"
    runtime_base = "alpine:3.18"
    binary_name = app.name
    binary_path = f"{working_dir}/bin/{binary_name}"

    builder_body = [
        f"WORKDIR {working_dir}",
        common.package_install_block(builder_base, ["git", "ca-certificates"] + dc.build_dependencies),
        _fetch_source_block(app, working_dir),
        common.run_block(common.cache_mount_flags(dc.cache_optimization, "/go/pkg/mod"), ["go mod download"]),
        common.run_block(
            common.cache_mount_flags(dc.cache_optimization, "/go/pkg/mod", "/root/.cache/go-build"),
            (
                dc.build_commands
                or [f"CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo -o {binary_path} ."]
            ),
        ),
    ]

    runtime_body = [
        common.package_install_block(runtime_base, ["ca-certificates"] + dc.runtime_dependencies),
        common.env_block(dc.environment_vars),
        f"COPY --from=builder {binary_path} /usr/local/bin/{binary_name}",
        common.user_block(dc.user_config, runtime_base),
        common.expose_block(dc.exposed_ports),
        common.healthcheck_block(dc.health_check),
        common.entrypoint_and_cmd(app, default_cmd=[f"/usr/local/bin/{binary_name}"]),
    ]

    return common.render_two_stage(builder_base, builder_body, runtime_base, runtime_body)


def build_java(app: AppConfig) -> str:
    dc = app.docker_config
    working_dir = _workdir(app)
    # The old generator's builder image (openjdk:21-jdk-slim) has no `mvn`
    # binary at all - `mvn clean package` in that stage only ever worked if
    # an app author overrode base_image to something with Maven preinstalled.
    # Defaulting to a real maven+JDK image fixes that real gap instead of
    # reproducing it.
    builder_base = dc.base_image or "maven:3.9-eclipse-temurin-21"
    # openjdk:* DockerHub images are EOL/unmaintained; eclipse-temurin is
    # the maintained successor and ships an explicit -jre variant.
    runtime_base = "eclipse-temurin:21-jre"

    builder_body = [
        f"WORKDIR {working_dir}",
        common.package_install_block(builder_base, dc.build_dependencies),
        _fetch_source_block(app, working_dir),
        common.run_block(common.cache_mount_flags(dc.cache_optimization, "/root/.m2"), ["mvn -B dependency:go-offline"]),
        common.run_block(
            common.cache_mount_flags(dc.cache_optimization, "/root/.m2"),
            (dc.build_commands or ["mvn -B clean package -DskipTests"]),
        ),
    ]

    runtime_body = [
        common.package_install_block(runtime_base, ["ca-certificates"] + dc.runtime_dependencies),
        common.env_block(dc.environment_vars),
        f"COPY --from=builder {working_dir}/target/*.jar /app/app.jar",
        common.user_block(dc.user_config, runtime_base),
        common.expose_block(dc.exposed_ports),
        common.healthcheck_block(dc.health_check),
        common.entrypoint_and_cmd(app, default_cmd=["java", "-jar", "/app/app.jar"]),
    ]

    return common.render_two_stage(builder_base, builder_body, runtime_base, runtime_body)


def build_generic(app: AppConfig) -> str:
    """Anything not in LANGUAGE_BUILDERS (an unrecognized docker_config.language
    value like the real `language: docker` github-runner app, or an explicit
    `generic`) - still a mandatory two-stage build, just with no
    language-specific dependency/build-artifact knowledge to apply beyond
    docker_config's own build_commands/entry_point."""
    dc = app.docker_config
    working_dir = _workdir(app)
    base_image = dc.base_image or "ubuntu:22.04"

    builder_body = [
        f"WORKDIR {working_dir}",
        common.package_install_block(base_image, dc.build_dependencies),
        _fetch_source_block(app, working_dir),
        "\n".join(f"RUN {cmd}" for cmd in dc.build_commands) if dc.build_commands else "",
    ]

    runtime_body = [
        common.package_install_block(base_image, ["ca-certificates"] + dc.runtime_dependencies),
        common.env_block(dc.environment_vars),
        f"WORKDIR {working_dir}",
        f"COPY --from=builder {working_dir} {working_dir}",
        # Same reasoning as python's builder: build_commands here may install
        # extra tools outside working_dir (builder and runtime share the same
        # base_image, so this only ever surfaces what build_commands put
        # there, nothing from the discarded toolchain).
        "COPY --from=builder /usr/local/bin /usr/local/bin" if dc.build_commands else "",
        common.user_block(dc.user_config, base_image),
        common.expose_block(dc.exposed_ports),
        common.healthcheck_block(dc.health_check),
        common.entrypoint_and_cmd(app),
    ]

    return common.render_two_stage(base_image, builder_body, base_image, runtime_body)


# Languages that take an optional app_dir (currently just python, for the
# psycopg2 sniff) get called with it; everything else ignores the extra arg.
LANGUAGE_BUILDERS: dict[str, Callable[..., str]] = {
    "python": build_python,
    "node": build_node,
    "rust": build_rust,
    "go": build_go,
    "java": build_java,
    "generic": build_generic,
}
