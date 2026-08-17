"""cube-manifest's CLI: list/validate/generate/build/plan/apply/ship. `apply`
is the only command that can mutate the real cluster, and only with `--yes`
after a real plan (with existing resources imported first) has been shown.
`build` is the only command that touches a container registry - it builds
the app's real image and (by default) pushes `<registry>/<app>:latest` (and
`:previous`, retagged from whatever was already there, if anything was).
`apply` still assumes that tag already exists at the registry by the time
it runs - `build` and `apply` are separate primitives, each independently
useful (e.g. `build --no-push` to just check a Dockerfile compiles, or
`apply` alone after a config-only change with no new image). `ship` is the
composed convenience command for the common case of actually shipping a
real code change: build, apply, and a rollout restart (needed because
reapplying an unchanged `:latest` tag never forces already-running pods to
repull it) - all in one command, gated on the same `--yes`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax

from cube_manifest import build as build_mod
from cube_manifest import deploy as deploy_mod
from cube_manifest import vps_routing
from cube_manifest.config import load_cluster_config, set_cluster_config
from cube_manifest.generators.dockerfile import generate_dockerfile
from cube_manifest.generators.terraform.builder import generate_terraform
from cube_manifest.schema.errors import ConfigError
from cube_manifest.schema.loader import discover_apps, load_app_config
from cube_manifest.schema.models import AppConfig

app = typer.Typer(no_args_is_help=True, help="cube-manifest: one app.yml -> Dockerfile + Terraform + deploy.")
generate_app = typer.Typer(no_args_is_help=True, help="Generate build/deploy artifacts. Never touches a cluster.")
app.add_typer(generate_app, name="generate")

console = Console()
err_console = Console(stderr=True)


def _parse_build_secrets(values: list[str]) -> dict[str, str]:
    """`["gh_pat=/path/to/token", ...]` -> `{"gh_pat": "/path/to/token"}` -
    forwarded to `build_and_push(build_secrets=...)`, which turns each entry
    into one `docker build --secret id=<id>,src=<path>` flag. Rejected early
    (rather than passed through to a confusing docker-cli error) if an entry
    isn't `id=path` shaped, or `path` doesn't exist - both point at a caller
    mistake worth catching before a build is even attempted."""
    secrets: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            err_console.print(f"[red]--build-secret {raw!r}: expected id=path[/red]")
            raise typer.Exit(1)
        sid, _, spath = raw.partition("=")
        if not Path(spath).is_file():
            err_console.print(f"[red]--build-secret {raw!r}: no such file: {spath}[/red]")
            raise typer.Exit(1)
        secrets[sid] = spath
    return secrets


def _apps_dir(apps_dir: Path | None) -> Path:
    d = apps_dir or (Path.cwd() / "apps")
    if not d.is_dir():
        err_console.print(f"[red]No such apps directory: {d}[/red]")
        raise typer.Exit(1)
    # Every command resolves apps_dir through this one function, so this is
    # the single chokepoint to load this deployment's own `.cube-manifest.yaml`
    # (searched starting at apps_dir's parent - i.e. the repo root a real
    # `apps/` directory lives under) and make it "the current cluster
    # config" for every generator call this command goes on to make.
    set_cluster_config(load_cluster_config(d.parent))
    return d


def _resolve_one(apps_dir: Path, app_name: str) -> Path:
    discovered = discover_apps(apps_dir)
    path = discovered.get(app_name)
    if path is None:
        available = ", ".join(sorted(discovered)) or "(none found)"
        err_console.print(f"[red]No app.yml found for {app_name!r} under {apps_dir}.[/red] Available: {available}")
        raise typer.Exit(1)
    return path


@app.command("list")
def list_apps(
    apps_dir: Path | None = typer.Option(None, "--apps-dir", help="Directory containing <app>/app.yml folders."),
) -> None:
    """List every app under apps_dir, with its type and validity."""
    d = _apps_dir(apps_dir)
    discovered = discover_apps(d)
    if not discovered:
        console.print(f"[yellow]No apps found under {d}[/yellow]")
        raise typer.Exit(1)
    for name, path in discovered.items():
        try:
            cfg = load_app_config(path)
            state = "[green]enabled[/green]" if cfg.enabled else "[dim]disabled[/dim]"
            console.print(f"  {name:32} {cfg.app_type.value:15} {state}")
        except ConfigError as exc:
            console.print(f"  {name:32} [red]INVALID[/red]  {exc.message}")


@app.command("validate")
def validate(
    apps: list[str] | None = typer.Argument(None, help="App names to validate. Omit to validate all."),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
) -> None:
    """Validate one, several, or all apps' app.yml against the schema."""
    d = _apps_dir(apps_dir)
    discovered = discover_apps(d)
    targets = apps or sorted(discovered)
    failures = 0
    for name in targets:
        path = discovered.get(name)
        if path is None:
            console.print(f"[red]FAIL[/red] {name}: no app.yml found under {d}")
            failures += 1
            continue
        try:
            load_app_config(path)
            console.print(f"[green]OK[/green]   {name}")
        except ConfigError as exc:
            console.print(f"[red]FAIL[/red] {name}: {exc.message}")
            failures += 1
    if failures:
        err_console.print(f"\n{failures}/{len(targets)} failed")
        raise typer.Exit(1)
    console.print(f"\n{len(targets)}/{len(targets)} valid")


@generate_app.command("dockerfile")
def generate_dockerfile_cmd(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    out: Path | None = typer.Option(None, "--out", help="Write to this file instead of stdout."),
) -> None:
    """Generate the Dockerfile for one app."""
    d = _apps_dir(apps_dir)
    path = _resolve_one(d, app_name)
    try:
        cfg = load_app_config(path)
        dockerfile = generate_dockerfile(cfg, app_dir=path.parent)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if out:
        out.write_text(dockerfile)
        console.print(f"[green]Wrote {out}[/green]")
    else:
        console.print(Syntax(dockerfile, "docker", theme="ansi_dark", line_numbers=False))


@generate_app.command("terraform")
def generate_terraform_cmd(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    out: Path | None = typer.Option(None, "--out", help="Write to this file instead of stdout."),
) -> None:
    """Generate the .tf.json for one app."""
    d = _apps_dir(apps_dir)
    path = _resolve_one(d, app_name)
    try:
        cfg = load_app_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    text = json.dumps(generate_terraform(cfg), indent=2)
    if out:
        out.write_text(text)
        console.print(f"[green]Wrote {out}[/green]")
    else:
        console.print(Syntax(text, "json", theme="ansi_dark", line_numbers=False))


@app.command("build")
def build_cmd(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    force: bool = typer.Option(
        False,
        "--force",
        help="No-op for now - there's no build-skip/cache optimization yet, every build is fresh regardless.",
    ),
    push: bool = typer.Option(
        True,
        "--push/--no-push",
        help="Push the built image(s) to the registry. --no-push also skips rollback-tagging (which "
        "itself pushes :previous) - a local-only build shouldn't mutate the registry at all.",
    ),
    prewarm: bool = typer.Option(
        True,
        "--prewarm/--no-prewarm",
        help="After a successful push, force every schedulable node to pull the new image right now "
        "(via a disposable per-node Pod) instead of leaving the first pull to whenever this app next "
        "cold-starts. --no-prewarm skips this - the image just sits unpulled until something needs it.",
    ),
    build_secret: list[str] = typer.Option(
        [],
        "--build-secret",
        help="id=path, repeatable. Forwarded to `docker build --secret id=<id>,src=<path>` for a "
        "Dockerfile's own `RUN --mount=type=secret,id=<id>` steps - e.g. a git credential for a private "
        "git+https dependency cloned mid-build. Never written to any image layer.",
    ),
) -> None:
    """Build the real Docker image for one app (cloning `external_repo` first
    if it's set - the source doesn't live under this app's own directory in
    that case) and, by default, push it to the registry as `<registry>/<app>
    :latest`. If that tag already exists in the registry, it's retagged
    `:previous` and pushed FIRST, before the new build starts - so
    `:previous` always means "whatever was actually live before this build,"
    never the image this same command just built (the old system's real,
    documented bug)."""
    d = _apps_dir(apps_dir)
    path = _resolve_one(d, app_name)
    cfg = _load_or_exit(path)
    secrets = _parse_build_secrets(build_secret)

    if force:
        console.print("[dim]--force: no effect yet (no build-skip optimization exists to bypass).[/dim]")

    console.print(f"[bold]Building {app_name} -> {build_mod.registry_tag(app_name, 'latest')}[/bold]")
    if cfg.external_repo is not None:
        console.print(
            f"[dim]external_repo: {cfg.external_repo.url} @ {cfg.external_repo.branch} "
            f"(path={cfg.external_repo.path or '.'})[/dim]"
        )

    try:
        result = build_mod.build_and_push(cfg, path.parent, push=push, prewarm=prewarm, build_secrets=secrets)
    except build_mod.BuildError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if result.rolled_back:
        console.print(f"[green]{result.rollback_message}[/green]")
    else:
        console.print(f"[dim]{result.rollback_message}[/dim]")

    if not result.build_ok:
        err_console.print(result.build_output)
        err_console.print(f"[red]Build failed: {result.latest_tag}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Built {result.latest_tag}[/green]")

    if not push:
        console.print("[yellow]--no-push: built locally only.[/yellow]")
        return

    if not result.pushed_latest:
        err_console.print(result.push_output)
        err_console.print(f"[red]Push failed: {result.latest_tag}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Pushed {result.latest_tag}[/green]")

    if not prewarm:
        console.print("[yellow]--no-prewarm: image left unpulled until something needs it.[/yellow]")
        return
    if not result.prewarm_results:
        console.print("[yellow]No Ready/schedulable nodes found to prewarm.[/yellow]")
        return
    any_failed = False
    for node, ok, message in result.prewarm_results:
        if ok:
            console.print(f"[green]Prewarmed {node}:[/green] {message}")
        else:
            any_failed = True
            err_console.print(f"[red]Prewarm failed on {node}:[/red] {message}")
    if any_failed:
        raise typer.Exit(1)


def _require_terraform() -> None:
    if shutil.which("terraform") is None:
        err_console.print("[red]terraform is not on PATH.[/red]")
        raise typer.Exit(1)


def _load_or_exit(path: Path) -> AppConfig:
    try:
        return load_app_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _print_plan_warnings(result: deploy_mod.PlanResult) -> None:
    if result.import_failures:
        console.print("[yellow]Could not import (will show as a real apply-time conflict instead):[/yellow]")
        for f in result.import_failures:
            console.print(f"  [yellow]{f}[/yellow]")
    if result.unknown_kinds:
        console.print(
            "[bold red]Unknown resource kind - existence was never checked, "
            "'will create' below may be wrong if this already exists live:[/bold red]"
        )
        for k in result.unknown_kinds:
            console.print(f"  [bold red]{k}[/bold red]")


@app.command("plan")
def plan(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    kubeconfig: Path = typer.Option(Path.home() / ".kube" / "config", "--kubeconfig"),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary working directory instead of deleting it."),
) -> None:
    """Generate Terraform for one app and run a REAL `terraform plan` against
    the real cluster (importing any already-existing live resources into
    the plan's local state first, so the diff is real rather than false
    "will create" noise). Strictly read-only - never runs `terraform apply`."""
    _require_terraform()
    d = _apps_dir(apps_dir)
    cfg = _load_or_exit(_resolve_one(d, app_name))

    result = deploy_mod.prepare_and_plan(cfg, kubeconfig, label="plan")
    console.print(f"[dim]{result.workdir}[/dim]")
    _print_plan_warnings(result)
    console.print(result.output)
    if not keep:
        shutil.rmtree(result.workdir, ignore_errors=True)
    if not result.ok:
        raise typer.Exit(1)


@app.command("apply")
def apply_cmd(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    kubeconfig: Path = typer.Option(Path.home() / ".kube" / "config", "--kubeconfig"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Actually apply. Without this, only shows the plan."),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary working directory instead of deleting it."),
) -> None:
    """Generate Terraform for one app, import any already-existing live
    resources, and show a real `terraform plan`. Only applies it if --yes
    is passed - otherwise this is exactly as read-only as `plan`. Never
    passes -auto-approve to Terraform; the plan file is always computed
    and shown before anything is ever applied."""
    _require_terraform()
    d = _apps_dir(apps_dir)
    cfg = _load_or_exit(_resolve_one(d, app_name))

    result = deploy_mod.prepare_and_plan(cfg, kubeconfig, label="apply")
    console.print(f"[dim]{result.workdir}[/dim]")
    _print_plan_warnings(result)
    console.print(result.output)

    if not result.ok:
        err_console.print("[red]Plan failed - not applying.[/red]")
        if not keep:
            shutil.rmtree(result.workdir, ignore_errors=True)
        raise typer.Exit(1)

    if not yes:
        console.print("\n[yellow]Dry run only - re-run with --yes to actually apply this plan.[/yellow]")
        if not keep:
            shutil.rmtree(result.workdir, ignore_errors=True)
        return

    console.print("\n[bold]Applying...[/bold]")
    applied = deploy_mod.real_apply(result.workdir)
    console.print(applied.stdout)
    if applied.returncode != 0:
        err_console.print(applied.stderr)
        err_console.print(f"[red]Apply failed. Working directory kept for inspection: {result.workdir}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Applied {app_name}.[/green]")

    if cfg.vps_route is not None:
        vps_config = vps_routing.VpsRoutingConfig.load()
        if vps_config is None:
            console.print(
                "[yellow]vps_route is set on this app but VPS routing isn't configured "
                "(no CUBE_MANIFEST_VPS_* env vars or ~/.config/cube-manifest/vps-routing.yaml) - "
                "skipping the public route sync.[/yellow]"
            )
        else:
            ok, message = vps_routing.sync_route(cfg, vps_config)
            (console if ok else err_console).print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")

    if not keep:
        shutil.rmtree(result.workdir, ignore_errors=True)


@app.command("ship")
def ship_cmd(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    kubeconfig: Path = typer.Option(Path.home() / ".kube" / "config", "--kubeconfig"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Actually apply and restart. Without this: still builds+pushes the real image (build has no "
        "confirmation gate of its own, same as running `build` directly) and shows the apply plan, but "
        "never touches the cluster - re-run with --yes to actually apply and roll it.",
    ),
    push: bool = typer.Option(True, "--push/--no-push", help="Forwarded to the build step."),
    prewarm: bool = typer.Option(True, "--prewarm/--no-prewarm", help="Forwarded to the build step."),
    restart: bool = typer.Option(
        True,
        "--restart/--no-restart",
        help="After a successful apply, force a rollout restart and wait for it to finish - reapplying "
        "an unchanged `:latest` tag never forces already-running pods to repull it. --no-restart if this "
        "app.yml change has no new image (e.g. a bare replica-count/config edit).",
    ),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary terraform working directory instead of deleting it."),
    rollout_timeout: str = typer.Option("90s", "--rollout-timeout", help="kubectl rollout status timeout, forwarded to the restart step."),
    build_secret: list[str] = typer.Option(
        [],
        "--build-secret",
        help="id=path, repeatable. Forwarded to the build step - see `build --help`.",
    ),
) -> None:
    """The full 'ship this change for real' sequence in one command: build
    the image and push it, apply the Terraform (only if --yes), then force a
    rollout restart and wait for it to finish. This doesn't replace
    `build`/`plan`/`apply` - it's composed from exactly those same
    primitives, for the common case of running all three back-to-back that
    every real code change to an already-deployed app needs (a bare `apply`
    on an unchanged image tag never forces existing pods to repull it)."""
    d = _apps_dir(apps_dir)
    path = _resolve_one(d, app_name)
    cfg = _load_or_exit(path)
    _require_terraform()
    secrets = _parse_build_secrets(build_secret)

    console.print(f"[bold]1/3 Building {app_name} -> {build_mod.registry_tag(app_name, 'latest')}[/bold]")
    if cfg.external_repo is not None:
        console.print(
            f"[dim]external_repo: {cfg.external_repo.url} @ {cfg.external_repo.branch} "
            f"(path={cfg.external_repo.path or '.'})[/dim]"
        )
    try:
        build_result = build_mod.build_and_push(cfg, path.parent, push=push, prewarm=prewarm, build_secrets=secrets)
    except build_mod.BuildError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if build_result.rolled_back:
        console.print(f"[green]{build_result.rollback_message}[/green]")
    if not build_result.build_ok:
        err_console.print(build_result.build_output)
        err_console.print(f"[red]Build failed: {build_result.latest_tag}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Built {build_result.latest_tag}[/green]")

    if push:
        if not build_result.pushed_latest:
            err_console.print(build_result.push_output)
            err_console.print(f"[red]Push failed: {build_result.latest_tag}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Pushed {build_result.latest_tag}[/green]")
    else:
        console.print("[yellow]--no-push: built locally only - apply below will still assume the tag exists at the registry.[/yellow]")

    console.print(f"\n[bold]2/3 Planning {app_name}[/bold]")
    plan_result = deploy_mod.prepare_and_plan(cfg, kubeconfig, label="ship")
    console.print(f"[dim]{plan_result.workdir}[/dim]")
    _print_plan_warnings(plan_result)
    console.print(plan_result.output)

    if not plan_result.ok:
        err_console.print("[red]Plan failed - not applying.[/red]")
        if not keep:
            shutil.rmtree(plan_result.workdir, ignore_errors=True)
        raise typer.Exit(1)

    if not yes:
        console.print("\n[yellow]Dry run only - re-run with --yes to actually apply and restart.[/yellow]")
        if not keep:
            shutil.rmtree(plan_result.workdir, ignore_errors=True)
        return

    console.print("\n[bold]Applying...[/bold]")
    applied = deploy_mod.real_apply(plan_result.workdir)
    console.print(applied.stdout)
    if applied.returncode != 0:
        err_console.print(applied.stderr)
        err_console.print(f"[red]Apply failed. Working directory kept for inspection: {plan_result.workdir}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Applied {app_name}.[/green]")

    if cfg.vps_route is not None:
        vps_config = vps_routing.VpsRoutingConfig.load()
        if vps_config is None:
            console.print(
                "[yellow]vps_route is set on this app but VPS routing isn't configured (no "
                "CUBE_MANIFEST_VPS_* env vars or ~/.config/cube-manifest/vps-routing.yaml) - skipping the "
                "public route sync.[/yellow]"
            )
        else:
            ok, message = vps_routing.sync_route(cfg, vps_config)
            (console if ok else err_console).print(f"[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]")

    if not keep:
        shutil.rmtree(plan_result.workdir, ignore_errors=True)

    if not restart:
        console.print(
            "[yellow]--no-restart: apply is live, but existing pods were not rolled - if you pushed a new "
            "image, they may still be running the old one.[/yellow]"
        )
        return

    console.print(f"\n[bold]3/3 Rolling {app_name}[/bold]")
    restart_result = deploy_mod.restart_and_wait(cfg, kubeconfig, timeout=rollout_timeout)
    if restart_result.kind is None:
        console.print(f"[dim]{app_name} is a {cfg.app_type.value} app with no restartable workload - nothing to roll.[/dim]")
        return
    console.print(restart_result.output)
    if not restart_result.ok:
        err_console.print(f"[red]Rollout restart/status failed for {restart_result.kind}/{app_name}.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{app_name} is live and rolled out.[/green]")


if __name__ == "__main__":
    app()
