"""Shared Dockerfile-generation building blocks used by every per-language
builder in `languages.py`.

Kept deliberately dumb and string-based - each helper takes exactly the
AppConfig/DockerConfig fields it needs and returns a block of Dockerfile
text (or "" for "nothing to emit here"), so a language builder just calls
the handful it needs and assembles them with `render_two_stage`. None of
this reaches into per-language specifics - that stays in languages.py.

Security-critical pieces live here on purpose, not duplicated per language:
- `external_repo_clone_block` is the ONE place a git deploy key ever touches
  a generated Dockerfile, and it always uses a BuildKit `--mount=type=secret`
  (never ARG/ENV) - the mounted file exists only for the lifetime of that
  single RUN step's filesystem view and is never written to any image layer,
  builder or final. Compare the old system's `main.py::ensure_arg_git_ssh_key`,
  which injected `ARG GIT_SSH_KEY_B64` + a plaintext decode step into every
  Dockerfile it touched - fine in a builder stage that gets discarded, fatal
  for python/node/generic, which the old generator only ever gave a single
  stage (the ARG's value is recoverable from `docker history` regardless of
  which stage it's in, since ARG/ENV values are always recorded in image
  metadata - only a secret *mount* avoids this).
"""

from __future__ import annotations

import json
import shlex

from manifest.schema.models import AppConfig, CacheOptimization, DockerHealthCheck, UserConfig

DOCKERFILE_SYNTAX_HEADER = "# syntax=docker/dockerfile:1"

# Secret id every generated Dockerfile uses for the git deploy key. The
# build orchestrator is responsible for actually supplying it at build time
# via `docker buildx build --secret id=git_ssh_key,src=<path-to-key>` -
# nothing this module does can prevent that path from existing on the build
# host; what it *does* guarantee is that the key never ends up inside the
# image itself.
GIT_SSH_KEY_SECRET_ID = "git_ssh_key"
GIT_SSH_KEY_MOUNT_TARGET = "/root/.ssh/id_rsa"


def is_alpine(base_image: str) -> bool:
    return "alpine" in base_image.lower()


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(p for p in items if p))


def package_install_block(base_image: str, packages: list[str]) -> str:
    """A single `RUN apt-get install ...` / `RUN apk add ...` block, or ''
    if there's nothing to install. Package name translation only covers the
    handful of Debian<->Alpine spellings the old generator special-cased
    (build-essential/libpq-dev/libssl-dev) - anything else is passed through
    unchanged, same as the old system did for everything not in that map."""
    packages = dedupe(packages)
    if not packages:
        return ""
    if is_alpine(base_image):
        alpine_names = {
            "build-essential": "build-base",
            "libpq-dev": "postgresql-dev",
            "libssl-dev": "openssl-dev",
            "gcc": "gcc",
            "python3-dev": "python3-dev",
        }
        pkgs = " ".join(alpine_names.get(p, p) for p in packages)
        return f"RUN apk add --no-cache \\\n    {pkgs}"
    pkgs = " ".join(packages)
    return (
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        f"    {pkgs} \\\n"
        "    && rm -rf /var/lib/apt/lists/* && apt-get clean"
    )


def cache_mount_flags(
    cache: CacheOptimization, primary_default: str, secondary_default: str | None = None
) -> list[str]:
    """`--mount=type=cache,target=...` flags for a RUN line. `primary_default`
    is the dependency-manager cache (pip/npm/cargo-registry/go-mod/m2);
    `secondary_default`, when a language actually has one (rust's target/
    dir, go's build cache), is the build-artifact cache. The old generator
    hardcoded these paths and silently ignored
    docker_config.cache_optimization.dependency_cache_mount/build_cache_mount
    even when a real app.yml set them (e.g. activator's own
    dependency_cache_mount: "/root/.cache/pip" happened to match the
    hardcoded default by coincidence, but nothing would have used it had it
    been set to something else) - this actually honors them."""
    if not cache.enabled:
        return []
    flags = [f"--mount=type=cache,target={cache.dependency_cache_mount or primary_default}"]
    if secondary_default is not None or cache.build_cache_mount:
        flags.append(f"--mount=type=cache,target={cache.build_cache_mount or secondary_default}")
    return flags


def run_block(mounts: list[str], commands: list[str]) -> str:
    """RUN --mount=... \\n    --mount=... \\n    cmd1 && \\n    cmd2"""
    body = " && \\\n    ".join(commands)
    if not mounts:
        return f"RUN {body}"
    return "RUN " + " \\\n    ".join([*mounts, body])


def external_repo_clone_block(app: AppConfig, dest: str) -> str:
    """RUN block that clones app.external_repo into `dest` inside the
    builder stage. SSH-style URLs (git@.../ssh://...) get the deploy key
    mounted in as a BuildKit secret for exactly this one RUN step; plain
    https:// URLs need no key at all, so no secret is wired in for those -
    matching the old ExternalRepoHandler.clone_repository's own
    needs_ssh_key = url.startswith("git@") or url.startswith("ssh://") test."""
    repo = app.external_repo
    assert repo is not None
    clone_cmd = (
        f"git clone --depth 1 --branch {shlex.quote(repo.branch)} "
        f"{shlex.quote(repo.url)} {shlex.quote(dest)}"
    )
    needs_key = repo.url.startswith("git@") or repo.url.startswith("ssh://")
    if not needs_key:
        return f"# Cloning {repo.url} (no deploy key needed for a plain https:// URL)\nRUN {clone_cmd}"
    return (
        f"# Cloning {repo.url} - deploy key mounted in-memory for this RUN step only,\n"
        "# never written to any image layer (contrast the old ARG/ENV-based injection,\n"
        "# whose value is always recoverable from `docker history` regardless of stage).\n"
        f"RUN --mount=type=secret,id={GIT_SSH_KEY_SECRET_ID},target={GIT_SSH_KEY_MOUNT_TARGET},required=true \\\n"
        "    mkdir -p -m 0700 /root/.ssh && \\\n"
        "    ssh-keyscan -H github.com >> /root/.ssh/known_hosts 2>/dev/null && \\\n"
        f"    GIT_SSH_COMMAND=\"ssh -i {GIT_SSH_KEY_MOUNT_TARGET} -o StrictHostKeyChecking=yes\" {clone_cmd}"
    )


def user_block(user_config: UserConfig | None, base_image: str) -> str:
    """USER directive for the runtime stage. A missing user_config block
    still gets the schema's own UserConfig() default (create a real non-root
    user) rather than silently running as root - the secure-by-default
    behavior the schema's default already implies, made real here."""
    cfg = user_config or UserConfig()
    if not cfg.create_user:
        return "# Running as root (docker_config.user_config.create_user is false)"
    if is_alpine(base_image):
        create = (
            f"RUN addgroup -g {cfg.gid} {cfg.user} && \\\n"
            f"    adduser -u {cfg.uid} -G {cfg.user} -D -s /bin/sh {cfg.user}"
        )
    else:
        create = (
            f"RUN addgroup --gid {cfg.gid} {cfg.user} && \\\n"
            f'    adduser --uid {cfg.uid} --gid {cfg.gid} --disabled-password --gecos "" {cfg.user}'
        )
    return f"{create}\nUSER {cfg.user}"


def healthcheck_block(health_check: DockerHealthCheck | None) -> str:
    """A real Docker HEALTHCHECK instruction - the old generator only ever
    read this block to derive k8s liveness/readiness probe *timing* and
    never emitted a Docker-level HEALTHCHECK at all, so `docker run` (or any
    orchestrator reading container health directly) never saw a health
    signal for these apps."""
    if health_check is None or not health_check.enabled:
        return ""
    cmd = health_check.command
    if cmd is None:
        cmd_str = "CMD exit 0"
    elif isinstance(cmd, list):
        cmd_str = "CMD " + json.dumps(cmd)
    else:
        cmd_str = f"CMD {cmd}"
    return (
        f"HEALTHCHECK --interval={health_check.interval} --timeout={health_check.timeout} "
        f"--retries={health_check.retries} \\\n    {cmd_str}"
    )


def entrypoint_and_cmd(app: AppConfig, default_cmd: list[str] | None = None) -> str:
    """docker_config.command (+ args) maps to ENTRYPOINT+CMD; entry_point
    alone (the only one of the three any real app.yml actually sets today)
    maps to CMD, exactly matching the old generator's real, proven behavior
    for the 26 apps currently in production. `default_cmd` lets a compiled
    language (rust/go/java) fall back to launching the exact artifact this
    same builder just produced, when the app author didn't set either."""
    dc = app.docker_config
    if dc.command:
        lines = ["ENTRYPOINT " + json.dumps(dc.command)]
        if dc.args:
            lines.append("CMD " + json.dumps(dc.args))
        return "\n".join(lines)
    if dc.entry_point:
        return "CMD " + json.dumps(dc.entry_point)
    if default_cmd:
        return "CMD " + json.dumps(default_cmd)
    return ""


def expose_block(ports: list[int]) -> str:
    return "\n".join(f"EXPOSE {p}" for p in dict.fromkeys(ports))


def env_block(env_vars: dict[str, str]) -> str:
    if not env_vars:
        return ""
    lines = []
    for key, value in env_vars.items():
        if any(c in value for c in (" ", '"', "'", "\\", "$")):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'ENV {key}="{escaped}"')
        else:
            lines.append(f"ENV {key}={value}")
    return "\n".join(lines)


def sections(*parts: str) -> str:
    """Join non-empty blocks with a blank line between them."""
    return "\n\n".join(p for p in parts if p and p.strip())


def render_two_stage(
    builder_from: str,
    builder_body: list[str],
    runtime_from: str,
    runtime_body: list[str],
) -> str:
    builder_stage = sections(f"FROM {builder_from} AS builder", *builder_body)
    runtime_stage = sections(f"FROM {runtime_from}", *runtime_body)
    return sections(DOCKERFILE_SYNTAX_HEADER, builder_stage, runtime_stage) + "\n"
