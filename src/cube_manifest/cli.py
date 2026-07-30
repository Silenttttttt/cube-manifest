"""cube-manifest's CLI: list/validate/generate/plan/apply. `apply` is the
only command that can mutate the real cluster, and only with `--yes` after
a real plan (with existing resources imported first) has been shown.
`docker build`/push isn't wired in here yet - `apply` currently assumes an
image already exists at the tag the generator references."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax

from cube_manifest import deploy as deploy_mod
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


def _apps_dir(apps_dir: Path | None) -> Path:
    d = apps_dir or (Path.cwd() / "apps")
    if not d.is_dir():
        err_console.print(f"[red]No such apps directory: {d}[/red]")
        raise typer.Exit(1)
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
    if not keep:
        shutil.rmtree(result.workdir, ignore_errors=True)


if __name__ == "__main__":
    app()
