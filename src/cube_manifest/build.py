"""Real `docker build` + `docker push` orchestration - the build-side
counterpart to `deploy.py`'s real terraform orchestration. `apply` currently
assumes the image it references already exists at the registry; this module
is what actually gets it there.

Builds the image the generated Dockerfile describes, tags it
`<registry>/<app>:latest`, and (by default) pushes it - but FIRST, if that
tag already exists in the registry, retags it `:previous` and pushes that,
before the new build ever starts. That ordering is the entire point: the old
app-generator/simple-deploy system's real, documented bug was tagging
"previous" AFTER the new build had already overwritten the local `:latest`,
which made "previous" meaningless (it was retagging the image that was
about to become - or already was - the new one, not the one that used to be
live). Doing the retag-and-push first means `:previous` always means
"whatever was actually live in the registry before this build ran."

`docker build` here is run with `DOCKER_BUILDKIT=1` explicitly, rather than
assuming a `buildx` CLI plugin is installed/selected as the default builder
- every generated Dockerfile has `# syntax=docker/dockerfile:1` plus
BuildKit-only `RUN --mount=type=cache`/`type=secret` instructions, and
`DOCKER_BUILDKIT=1` is what makes plain `docker build` (not `docker buildx
build`) honor those regardless of which builder happens to be configured as
default - notably sidestepping a real gotcha found while building this: a
`docker-container`-driver buildx builder set as the CLI default does NOT
automatically share the daemon's own `insecure-registries` config
(`/etc/docker/daemon.json`), so a plain HTTP-only local registry push can
fail under that builder even though `docker push` on its own works fine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from cube_manifest.config import get_cluster_config
from cube_manifest.generators.dockerfile import generate_dockerfile
from cube_manifest.schema.models import AppConfig

_BUILDKIT_ENV = {**os.environ, "DOCKER_BUILDKIT": "1"}


class BuildError(RuntimeError):
    """A real external command (git/docker) failed, or the registry
    couldn't be reached at all to answer the rollback-tagging check. The
    message is that command/request's own output, not a generic wrapper."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=_BUILDKIT_ENV)


def registry_tag(app_name: str, tag: str) -> str:
    """<current cluster's registry_url>/<app_name>:<tag> - reads the SAME
    module-level config `_common.py`'s generators use (see config.py),
    never a hardcoded literal."""
    return f"{get_cluster_config().registry_url}/{app_name}:{tag}"


def tag_exists_in_registry(registry_url: str, app_name: str, tag: str) -> bool:
    """A plain, unauthenticated HTTP HEAD against the registry's own v2 API
    - this is the most reliable check for "does this tag already exist"
    given a plain, unauthenticated local registry: it doesn't depend on
    which `docker`/`buildx` builder is currently selected, or on pulling the
    (potentially large) image just to find out it exists (`docker pull`
    would also work, per the task's own suggestion, but transfers the whole
    image only to answer a yes/no question; a manifest HEAD doesn't)."""
    # A plain "docker manifest v2" Accept header alone isn't enough: modern
    # `docker build` (via buildx) emits an OCI image INDEX (a manifest list,
    # for provenance/attestation, even for a single-platform build) - the
    # registry answers 404 MANIFEST_UNKNOWN for that same real, existing tag
    # if the request's Accept header doesn't also list the index/manifest-
    # list media types, confirmed empirically against the real registry this
    # builds against. Every media type a real `docker push` might have
    # created needs to be offered here, or an existing tag reads as absent.
    url = f"http://{registry_url}/v2/{app_name}/manifests/{tag}"
    accept = (
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.list.v2+json, "
        "application/vnd.oci.image.manifest.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json"
    )
    req = urllib.request.Request(url, method="HEAD", headers={"Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise BuildError(f"registry returned HTTP {exc.code} checking {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BuildError(f"could not reach registry at {url}: {exc.reason}") from exc


def clone_external_repo(url: str, branch: str, dest: Path) -> subprocess.CompletedProcess[str]:
    """Shallow clone only (`--depth 1`) - no persistent mirror/cache yet.
    That's a real, documented future improvement (re-cloning a large repo on
    every build is wasteful), not a correctness gap for this pass: every
    build still gets the real, current `branch` HEAD, just not reused
    across builds."""
    return _run(["git", "clone", "--depth", "1", "--branch", branch, url, str(dest)])


def resolve_source_root(app: AppConfig, app_dir: Path, clone_root: Path | None) -> Path:
    """Where this app's real build source lives. `external_repo` means the
    source is NOT under `apps/<name>/` at all - it's the given subpath of a
    different repo (already shallow-cloned to `clone_root` by the caller) -
    so that wins whenever it's set; otherwise the app's own directory is the
    source, same as `generate dockerfile`/`generate terraform` already
    assume."""
    if app.external_repo is not None:
        if clone_root is None:
            raise BuildError(f"{app.name}: external_repo is set but no clone_root was provided")
        return clone_root / (app.external_repo.path or ".")
    return app_dir


def build_context(app: AppConfig, source_root: Path, dockerfile_text: str, tmp_dir: Path) -> tuple[Path, Path]:
    """The real (context_dir, dockerfile_path) to hand to `docker build`.

    `docker_config.dockerfile` set (passthrough, e.g. local-storage-ui): the
    app brought its own handwritten Dockerfile - `generate_dockerfile`
    already validated it exists on disk relative to `source_root`/`context`
    (see `generators/dockerfile/__init__.py::_resolve_passthrough`), so
    build directly against that real file and its real context, no
    rewriting needed.

    Otherwise: `generate_dockerfile` only returned a STRING - there's no
    real file yet, so write one to `tmp_dir` and use it via `-f` (Docker
    supports a Dockerfile living outside its build context just fine). The
    context is `source_root` itself either way: every generated Dockerfile's
    fetch step is either a plain `COPY . .` (needs the whole source tree
    present as context) or its own in-Dockerfile `git clone` (external_repo
    set, no local dockerfile passthrough - context content isn't used by
    that path, but `docker build` still needs *some* valid directory)."""
    dc = app.docker_config
    if dc.dockerfile is not None:
        context_dir = source_root / (dc.context or ".")
        dockerfile_path = context_dir / dc.dockerfile
        return context_dir, dockerfile_path

    dockerfile_path = tmp_dir / "Dockerfile.generated"
    dockerfile_path.write_text(dockerfile_text)
    return source_root, dockerfile_path


def rollback_previous(app_name: str) -> tuple[bool, str]:
    """If `<registry>/<app_name>:latest` already exists in the registry,
    retag it `:previous` and push THAT, before anything about the new build
    has happened - see the module docstring for why the ordering matters."""
    registry_url = get_cluster_config().registry_url
    latest_tag = registry_tag(app_name, "latest")
    previous_tag = registry_tag(app_name, "previous")

    if not tag_exists_in_registry(registry_url, app_name, "latest"):
        return False, f"No existing {latest_tag} in the registry yet - nothing to roll back."

    pulled = _run(["docker", "pull", latest_tag])
    if pulled.returncode != 0:
        raise BuildError(f"docker pull {latest_tag} failed:\n{pulled.stdout}{pulled.stderr}")
    tagged = _run(["docker", "tag", latest_tag, previous_tag])
    if tagged.returncode != 0:
        raise BuildError(f"docker tag {latest_tag} -> {previous_tag} failed:\n{tagged.stdout}{tagged.stderr}")
    pushed = _run(["docker", "push", previous_tag])
    if pushed.returncode != 0:
        raise BuildError(f"docker push {previous_tag} failed:\n{pushed.stdout}{pushed.stderr}")
    return True, f"Retagged the existing {latest_tag} as {previous_tag} and pushed it."


def _candidate_nodes() -> list[str]:
    """Every currently Ready, schedulable node name - `kubectl get nodes`,
    not a node-local shortcut (SSH/`crictl` on a specific box), because
    prewarming has to work identically on any cluster this tool targets,
    not just ones where the caller happens to have host access to every
    node. Cordoned (`unschedulable`) nodes are skipped - the real scheduler
    would never place the app there either, so warming them would be
    pulling an image nothing can use yet."""
    result = _run(["kubectl", "get", "nodes", "-o", "json"])
    if result.returncode != 0:
        raise BuildError(f"kubectl get nodes failed:\n{result.stdout}{result.stderr}")
    nodes = json.loads(result.stdout)["items"]
    names = []
    for n in nodes:
        if n.get("spec", {}).get("unschedulable"):
            continue
        conditions = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        if conditions.get("Ready") == "True":
            names.append(n["metadata"]["name"])
    return names


def _prewarm_pod_name(app_name: str, node: str) -> str:
    # Kubernetes object names: lowercase alphanumeric + '-', max 253 chars.
    raw = f"cube-prewarm-{app_name}-{node}".lower().replace("_", "-").replace(".", "-")
    return raw[:253].rstrip("-")


def prewarm_node(app_name: str, image: str, node: str, *, timeout_seconds: int = 180) -> tuple[bool, str]:
    """Force `node` to actually pull `image` right now, via a disposable
    Pod pinned to it (`nodeName`, not `nodeSelector` - this has to land on
    THIS specific node, not just some node matching a label) with
    `imagePullPolicy: Always`. That's the real Kubernetes pull path a live
    Deployment would use - the same thing that made this necessary in the
    first place: `build_and_push` only ever talks to the registry via plain
    `docker push`, so no node's kubelet/containerd is ever involved, and a
    freshly built image can sit completely unpulled on every node
    indefinitely. For a `min_replicas: 0` app that gap is invisible until
    the next real cold start, which then pays a full first-time network
    pull as part of what's supposed to be a fast scale-up.

    A blanket "tolerate everything" toleration is required here on
    purpose: this pod is placed via `nodeName`, which skips the scheduler's
    own taint filtering, but the kubelet-side `TaintManager` can still evict
    a running pod that doesn't tolerate a node's taints - and this has to
    warm control-plane/critical-workload nodes too, whatever taints they
    carry, or the exact nodes real apps schedule onto (see
    `scheduling.node_preference` in app.yml) would be the ones left cold."""
    pod_name = _prewarm_pod_name(app_name, node)
    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "labels": {"cube-manifest.io/prewarm-for": app_name},
        },
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "tolerations": [{"operator": "Exists"}],
            "containers": [
                {
                    "name": "prewarm",
                    "image": image,
                    "imagePullPolicy": "Always",
                    "command": ["true"],
                }
            ],
        },
    }

    _run(["kubectl", "delete", "pod", pod_name, "--ignore-not-found", "--wait=true"])
    try:
        applied = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(pod_manifest),
            capture_output=True,
            text=True,
            check=False,
            env=_BUILDKIT_ENV,
        )
        if applied.returncode != 0:
            return False, f"kubectl apply (prewarm pod) failed:\n{applied.stdout}{applied.stderr}"

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            got = _run(["kubectl", "get", "pod", pod_name, "-o", "json"])
            if got.returncode != 0:
                return False, f"kubectl get pod {pod_name} failed:\n{got.stdout}{got.stderr}"
            pod = json.loads(got.stdout)
            phase = pod.get("status", {}).get("phase")
            if phase == "Succeeded":
                return True, f"{image} pulled and warm on {node}."
            if phase == "Failed":
                return False, f"prewarm pod on {node} reached Failed:\n{json.dumps(pod['status'], indent=2)}"
            statuses = pod.get("status", {}).get("containerStatuses", [])
            if statuses:
                waiting = statuses[0].get("state", {}).get("waiting", {})
                if waiting.get("reason") == "ErrImagePull" or waiting.get("reason") == "ImagePullBackOff":
                    return False, f"{node}: {waiting.get('reason')} - {waiting.get('message')}"
            time.sleep(1)
        return False, f"timed out after {timeout_seconds}s waiting for {node} to pull {image}"
    finally:
        _run(["kubectl", "delete", "pod", pod_name, "--ignore-not-found", "--wait=false"])


def prewarm_image(app_name: str, image: str, *, timeout_seconds: int = 180) -> list[tuple[str, bool, str]]:
    """Warm `image` on every real, schedulable node - see `prewarm_node`
    for why this exists. Sequential, one node at a time: a homelab-scale
    node count (single digits) makes the extra parallelism not worth the
    added complexity, and sequential output is easier to read when a pull
    is genuinely slow."""
    nodes = _candidate_nodes()
    return [(node, *prewarm_node(app_name, image, node, timeout_seconds=timeout_seconds)) for node in nodes]


@dataclass
class BuildResult:
    app_name: str
    latest_tag: str
    previous_tag: str
    rolled_back: bool
    rollback_message: str
    build_ok: bool
    build_output: str
    pushed_latest: bool
    push_output: str
    prewarm_results: list[tuple[str, bool, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _private_git_dependency_warnings(source_root: Path, build_secrets: dict[str, str] | None) -> list[str]:
    """Real incident, not a hypothetical: a `pip install git+https://...`
    dependency with no `@<ref>` pin means the layer that installs it never
    invalidates (Docker's cache is keyed on instruction/file text, not what
    the remote actually holds), so a stale install can ship forever with no
    error. And if that layer's credential (a `--build-secret`) is missing or
    broken, nothing notices until something else finally forces the layer to
    rebuild - at which point it fails with a plain git auth error days or
    weeks later, nowhere near whatever actually broke the credential.

    Deliberately narrow: only `requirements.txt` right next to the resolved
    build source, only lines containing `git+`. Not a hard failure and not
    exhaustive (pyproject.toml, npm/cargo/go git deps aren't covered) - this
    exists to make the silent failure mode noisy on the very first build
    instead of six weeks later, not to be a general dependency linter."""
    requirements = source_root / "requirements.txt"
    if not requirements.is_file():
        return []

    warnings: list[str] = []
    for line in requirements.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "git+" not in line:
            continue
        if not build_secrets:
            warnings.append(
                f"requirements.txt has a git+ dependency ({line!r}) but no --build-secret was "
                "passed. If the pip-install layer is cached this build will report success while "
                "installing nothing; if it isn't cached, the clone will fail outright."
            )
        if "@" not in line.split("git+", 1)[1]:
            warnings.append(
                f"requirements.txt has an unpinned git+ dependency ({line!r}) - no @<ref>. "
                "Docker's build cache is keyed on this file's text, not the remote's real content, "
                "so this layer can silently keep installing an old version forever. Pin it to a "
                "commit SHA."
            )
    return warnings


def build_and_push(
    app: AppConfig,
    app_dir: Path,
    *,
    push: bool = True,
    prewarm: bool = True,
    build_secrets: dict[str, str] | None = None,
) -> BuildResult:
    """The real, end-to-end build for one app: rollback-tag (if pushing),
    resolve the real source (cloning `external_repo` first if set), generate
    the Dockerfile, `docker build`, then `docker push` if `push` is True,
    then prewarm every schedulable node's containerd cache with the image
    that was just pushed if `prewarm` is True.

    `push=False` also skips the rollback-tagging step - it mutates the
    registry (pulls the old `:latest`, retags it, pushes `:previous`), which
    a caller asking for a local-only build clearly doesn't want either.
    `prewarm` only ever runs after a successful push - there's nothing in
    the registry yet to warm a node with otherwise.

    `BuildResult.warnings`: non-fatal, never blocks the build - see
    `_private_git_dependency_warnings` for what it currently catches (an
    unpinned or unauthenticated private `git+` dependency in
    `requirements.txt`, a real incident where this looked like success
    while silently installing nothing).

    `build_secrets`: {secret_id: path_on_this_host} - forwarded to `docker
    build` as one `--secret id=<id>,src=<path>` per entry, for a generated or
    handwritten Dockerfile's own `RUN --mount=type=secret,id=<id>` steps
    (e.g. a git credential needed to `pip install`/`npm install` a private
    git+https dependency inside the build). `path` is read by the local
    `docker` CLI process itself and streamed to the BuildKit daemon - it does
    NOT need to already exist inside any build context, and the secret is
    never written to an image layer (contrast `--build-arg`, whose resolved
    value is always recoverable from `docker history` since ARG substitution
    happens before that layer's command is recorded). Empty/None forwards no
    `--secret` flags at all, same as before this parameter existed."""
    app_name = app.name
    latest_tag = registry_tag(app_name, "latest")
    previous_tag = registry_tag(app_name, "previous")

    if app.external_repo is not None and shutil.which("git") is None:
        raise BuildError("git is not on PATH (required: external_repo is set)")
    if shutil.which("docker") is None:
        raise BuildError("docker is not on PATH")

    if push:
        rolled_back, rollback_message = rollback_previous(app_name)
    else:
        rolled_back, rollback_message = False, "Skipped rollback tagging - build is --no-push (local only)."

    tmp_root = Path(tempfile.mkdtemp(prefix=f"cube-manifest-build-{app_name}-"))
    try:
        clone_root: Path | None = None
        if app.external_repo is not None:
            clone_root = tmp_root / "repo"
            cloned = clone_external_repo(app.external_repo.url, app.external_repo.branch, clone_root)
            if cloned.returncode != 0:
                raise BuildError(
                    f"git clone {app.external_repo.url} (branch {app.external_repo.branch}) failed:\n"
                    f"{cloned.stdout}{cloned.stderr}"
                )

        source_root = resolve_source_root(app, app_dir, clone_root)
        dockerfile_text = generate_dockerfile(app, app_dir=source_root)
        context_dir, dockerfile_path = build_context(app, source_root, dockerfile_text, tmp_root)
        warnings = _private_git_dependency_warnings(source_root, build_secrets)

        secret_flags = [f"--secret=id={sid},src={spath}" for sid, spath in (build_secrets or {}).items()]
        built = _run(
            ["docker", "build", *secret_flags, "-f", str(dockerfile_path), "-t", latest_tag, str(context_dir)]
        )
        build_ok = built.returncode == 0
        build_output = built.stdout + built.stderr

        pushed_latest = False
        push_output = ""
        if build_ok and push:
            pushed = _run(["docker", "push", latest_tag])
            pushed_latest = pushed.returncode == 0
            push_output = pushed.stdout + pushed.stderr

        prewarm_results: list[tuple[str, bool, str]] = []
        if pushed_latest and prewarm:
            prewarm_results = prewarm_image(app_name, latest_tag)

        return BuildResult(
            app_name=app_name,
            latest_tag=latest_tag,
            previous_tag=previous_tag,
            rolled_back=rolled_back,
            rollback_message=rollback_message,
            build_ok=build_ok,
            build_output=build_output,
            pushed_latest=pushed_latest,
            prewarm_results=prewarm_results,
            push_output=push_output,
            warnings=warnings,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
