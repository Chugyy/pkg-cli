"""``pkg deps`` — dependency inspection subcommands.

Subcommands:
  check                 -> verify lockfile health in the current workspace
  show <canonical_id>   -> display dep tree of a package (queries the hub)

Registration: ``deps_app`` is registered into the main ``pkg`` Typer app
from ``cli.py`` via a defensive try/except import at the bottom of that file.

Error exit codes for ``check``:
  0 — lockfile present and valid YAML (empty is coherent)
  1 — lockfile present but corrupt (unparseable YAML)
  2 — lockfile absent (run ``pkg install`` first)
"""

from __future__ import annotations

from pathlib import Path

import requests
import typer
import yaml

deps_app = typer.Typer(help="Dependency inspection commands.")

_LOCKFILE_NAME = ".pkg-lock.yaml"


# ---------------------------------------------------------------------------
# pkg deps check
# ---------------------------------------------------------------------------


@deps_app.command("check")
def deps_check() -> None:
    """Check lockfile health in the current working directory.

    Exit 0  — lockfile present and valid YAML (even if empty).
    Exit 1  — lockfile present but corrupt (invalid YAML or wrong type).
    Exit 2  — lockfile absent; run ``pkg install`` first.
    """
    lockfile = Path(_LOCKFILE_NAME)
    if not lockfile.exists():
        typer.echo("No lockfile found. Run pkg install first.")
        raise typer.Exit(2)

    try:
        data = yaml.safe_load(lockfile.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        typer.echo("Corrupt lockfile.")
        raise typer.Exit(1)

    if not isinstance(data, dict):
        typer.echo("Corrupt lockfile.")
        raise typer.Exit(1)

    # Lockfile is present and parseable — coherent (drift detection is out of
    # scope for this iteration; an empty entries dict is trivially coherent).


# ---------------------------------------------------------------------------
# pkg deps show <canonical_id>
# ---------------------------------------------------------------------------


@deps_app.command("show")
def deps_show(
    canonical_id: str = typer.Argument(..., help="Package id 'owner/name'."),
) -> None:
    """Display the dependency tree of a package from the hub.

    On 404 the command echoes 'Package <id> not found' and exits 1.
    Tree rendering is minimal (flat list of direct deps).
    """
    from .cli import cfg

    if "/" not in canonical_id or "@" in canonical_id:
        typer.echo(
            f"Error: invalid package id {canonical_id!r}. "
            "Expected 'owner/name'.",
            err=True,
        )
        raise typer.Exit(1)
    owner, _, name = canonical_id.partition("/")
    if not owner or not name or "/" in name:
        typer.echo(
            f"Error: invalid package id {canonical_id!r}. "
            "Expected 'owner/name'.",
            err=True,
        )
        raise typer.Exit(1)

    c = cfg()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/deps"
    try:
        r = requests.get(url, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code == 404:
        typer.echo(f"Package {canonical_id} not found.")
        raise typer.Exit(1)

    if r.status_code >= 400:
        typer.echo(f"Error: HTTP {r.status_code}", err=True)
        raise typer.Exit(1)

    try:
        data = r.json() or {}
    except ValueError:
        typer.echo("Error: invalid response from hub.", err=True)
        raise typer.Exit(1)

    deps = data.get("deps") or data.get("dependencies") or []
    typer.echo(canonical_id)
    for dep in deps:
        typer.echo(f"  - {dep}")
