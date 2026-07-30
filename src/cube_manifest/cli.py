"""cube-manifest's CLI. Deliberately narrow for now: list/validate/generate,
plus a real (but strictly read-only) `plan` that runs `terraform plan`
against the live cluster without ever applying anything. Building/pushing
images and a real `apply` are follow-on work - not built yet, and this CLI
has no command that mutates a cluster.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax

from cube_manifest.generators.dockerfile import generate_dockerfile
from cube_manifest.generators.terraform.builder import generate_terraform
from cube_manifest.schema.errors import ConfigError
from cube_manifest.schema.loader import discover_apps, load_app_config

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


def _provider_document(kubeconfig: Path, namespace: str) -> dict:
    """A minimal kubernetes-provider .tf.json fragment for planning ONE app in
    isolation. Generated resources reference `kubernetes_namespace.<ns>` (the
    real root module manages namespaces once, shared across every app - see
    e.g. Cubernetes' own infrastructure/terraform/main.tf), so a standalone
    per-app plan needs that same resource address declared here too, or
    Terraform can't resolve the reference at all.

    Caveat this deliberately doesn't hide: because this sandbox's state is
    always empty, the plan will always show the namespace itself as "to
    create" even though it already exists for real - that's an artifact of
    planning a single app in isolation from the shared root module, not a
    real pending change. A future real `apply` path needs to run against
    the actual shared root module (with the namespace already applied/
    imported there), not this throwaway per-app sandbox."""
    return {
        "terraform": {
            "required_providers": {
                "kubernetes": {"source": "hashicorp/kubernetes", "version": "~> 2.20"},
            }
        },
        "provider": {"kubernetes": [{"config_path": str(kubeconfig)}]},
        "resource": {
            "kubernetes_namespace": {
                namespace: {"metadata": [{"name": namespace}]},
            }
        },
    }


@app.command("plan")
def plan(
    app_name: str = typer.Argument(..., metavar="APP"),
    apps_dir: Path | None = typer.Option(None, "--apps-dir"),
    kubeconfig: Path = typer.Option(Path.home() / ".kube" / "config", "--kubeconfig"),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary working directory instead of deleting it."),
) -> None:
    """Generate Terraform for one app and run a REAL `terraform plan` against
    the real cluster. Strictly read-only - this never runs `terraform apply`
    and never mutates anything. Requires `terraform` on PATH."""
    if shutil.which("terraform") is None:
        err_console.print("[red]terraform is not on PATH - install it first (this command only ever plans, never applies).[/red]")
        raise typer.Exit(1)

    d = _apps_dir(apps_dir)
    path = _resolve_one(d, app_name)
    try:
        cfg = load_app_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    workdir = Path(tempfile.mkdtemp(prefix=f"cube-manifest-plan-{app_name}-"))
    try:
        (workdir / "provider.tf.json").write_text(json.dumps(_provider_document(kubeconfig, cfg.namespace), indent=2))
        (workdir / f"{app_name}.tf.json").write_text(json.dumps(generate_terraform(cfg), indent=2))

        console.print(f"[dim]{workdir}[/dim]")
        init = subprocess.run(["terraform", "init", "-input=false"], cwd=workdir, capture_output=True, text=True, check=False)
        if init.returncode != 0:
            err_console.print(init.stdout)
            err_console.print(init.stderr)
            raise typer.Exit(1)

        result = subprocess.run(
            ["terraform", "plan", "-input=false", "-no-color"], cwd=workdir, capture_output=True, text=True, check=False
        )
        console.print(result.stdout)
        if result.returncode not in (0, 2):  # 2 == "plan has changes", still a clean run
            err_console.print(result.stderr)
            raise typer.Exit(1)
    finally:
        if keep:
            console.print(f"[yellow]Kept working directory: {workdir}[/yellow]")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    app()
