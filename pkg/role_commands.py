"""`pkg role` — manage per-package custom roles (iter #4 — mod-12).

Subcommands:
  list   <owner>/<name>                         -> list roles (system + custom)
  create <owner>/<name> <name> --perms p1,p2    -> create a custom role
  update <owner>/<name> <name> --perms p1,p2    -> replace permissions
  delete <owner>/<name> <name> [--force]        -> delete a custom role

CLI surface uses role NAME (human-friendly); the backend addresses roles by
UUID. The `update` and `delete` commands therefore GET the roles list first
to resolve name -> id, then call the mutation endpoint.

Auth: same gate as `pkg acl …` — requires a logged-in session. Without auth,
exits 1 BEFORE any HTTP call (cf. mod-12.auth.fm-2).
"""

from __future__ import annotations

import requests
import typer
from rich.console import Console
from rich.table import Table


role_app = typer.Typer(help="Manage per-package custom roles.")

# Wide console to prevent column truncation under CliRunner (which has no TTY).
_console = Console(width=240, no_color=True, soft_wrap=True)


# ---------------------------------------------------------------------------
# Helpers — duplicated from acl_commands to keep both modules self-contained.
# ---------------------------------------------------------------------------


def _parse_canonical(canonical: str) -> tuple[str, str]:
    """Parse 'owner/name'. Exit(1) on bad input — BEFORE any HTTP call."""
    if not canonical or "/" not in canonical or "@" in canonical:
        typer.echo(
            f"Error: invalid package id {canonical!r}. "
            f"Expected 'owner/name'.",
            err=True,
        )
        raise typer.Exit(1)
    owner, _, name = canonical.partition("/")
    if not owner or not name or "/" in name:
        typer.echo(
            f"Error: invalid package id {canonical!r}. "
            f"Expected 'owner/name'.",
            err=True,
        )
        raise typer.Exit(1)
    return owner, name


def _ctx() -> tuple[dict, dict]:
    """Return ``(config, auth_headers)`` or exit(1) if not logged in."""
    from .cli import cfg, client_headers

    c = cfg()
    headers = client_headers()
    if not headers:
        typer.echo(
            "Not logged in (run `pkg login` or `pkg auth <token>`).",
            err=True,
        )
        raise typer.Exit(1)
    return c, headers


def _extract_detail(response: requests.Response) -> str:
    """Best-effort extraction of FastAPI's ``detail`` for error display.

    Handles both plain string detail and the structured dict shape used by the
    backend for richer errors (e.g. ``{"error": "role_assigned", "count": 3,
    "message": "Cannot delete role assigned to 3 users"}``).
    """
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail")
        if detail:
            if isinstance(detail, dict):
                msg = detail.get("message") or detail.get("error")
                return str(msg) if msg else str(detail)
            return str(detail)
    return response.text or f"HTTP {response.status_code}"


def _parse_perms(perms_str: str) -> list[str]:
    """Parse 'a,b , c' -> ['a', 'b', 'c'] (strip + drop empties)."""
    if not perms_str:
        return []
    return [p.strip() for p in perms_str.split(",") if p.strip()]


def _fetch_roles(c: dict, headers: dict, owner: str, name: str) -> dict:
    """GET /api/packages/{owner}/{name}/roles or exit(1) on HTTP error."""
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/roles"
    try:
        r = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    if r.status_code == 404:
        typer.echo(
            "Error: package not found or you don't have permission.",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)
    try:
        return r.json() or {}
    except ValueError:
        typer.echo("Error: invalid response from hub.", err=True)
        raise typer.Exit(1)


def _resolve_role_id(
    c: dict, headers: dict, owner: str, name: str, role_name: str
) -> str:
    """Resolve a role NAME to its UUID. Exit(1) when:
       - the role does not exist for this package, OR
       - the role is a system role (immutable — the CLI refuses to mutate it).
    """
    data = _fetch_roles(c, headers, owner, name)
    items = data.get("items") or []
    for entry in items:
        if entry.get("name") == role_name:
            is_system = bool(
                entry.get("isSystem") or entry.get("is_system")
            )
            if is_system:
                typer.echo(
                    f"Error: role {role_name!r} is a system role and cannot "
                    f"be modified or deleted.",
                    err=True,
                )
                raise typer.Exit(1)
            role_id = entry.get("id")
            if not role_id:
                typer.echo(
                    "Error: backend response is missing the role id.",
                    err=True,
                )
                raise typer.Exit(1)
            return str(role_id)
    typer.echo(
        f"Error: role {role_name!r} not found in package {owner}/{name}.",
        err=True,
    )
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@role_app.command("list")
def role_list(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
) -> None:
    """List roles (system + custom) defined for a package."""
    owner, name = _parse_canonical(canonical)
    c, headers = _ctx()
    data = _fetch_roles(c, headers, owner, name)
    items = data.get("items") or []
    if not items:
        typer.echo("No roles.")
        return

    table = Table(title=f"Roles for {owner}/{name}", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("System")
    table.add_column("Permissions")
    for entry in items:
        is_system = bool(entry.get("isSystem") or entry.get("is_system"))
        perms = entry.get("permissions") or []
        perms_text = ", ".join(perms) if perms else "-"
        table.add_row(
            str(entry.get("name", "")),
            "yes" if is_system else "no",
            perms_text,
        )
    _console.print(table)


@role_app.command("create")
def role_create(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
    role_name: str = typer.Argument(..., help="Name of the new custom role."),
    perms: str = typer.Option(
        ...,
        "--perms",
        help="Comma-separated permissions (e.g. 'pr:create,pr:review').",
    ),
) -> None:
    """Create a custom role with the given permissions."""
    owner, name = _parse_canonical(canonical)
    permissions = _parse_perms(perms)
    if not permissions:
        typer.echo(
            "Error: --perms must contain at least one permission.", err=True
        )
        raise typer.Exit(1)

    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/roles"
    payload = {"name": role_name, "permissions": permissions}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code == 404:
        typer.echo(
            "Error: package not found or you don't have permission.",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    try:
        created = r.json() or {}
    except ValueError:
        created = {}
    perms_text = ", ".join(created.get("permissions") or permissions)
    typer.echo(
        f"Created role {role_name!r} on {owner}/{name} "
        f"with permissions: {perms_text}."
    )


@role_app.command("update")
def role_update(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
    role_name: str = typer.Argument(..., help="Name of the role to update."),
    perms: str = typer.Option(
        ...,
        "--perms",
        help="Comma-separated replacement permission list (non-empty).",
    ),
) -> None:
    """Replace the permissions of a custom role."""
    owner, name = _parse_canonical(canonical)
    permissions = _parse_perms(perms)
    if not permissions:
        typer.echo(
            "Error: --perms must contain at least one permission.", err=True
        )
        raise typer.Exit(1)

    c, headers = _ctx()
    role_id = _resolve_role_id(c, headers, owner, name, role_name)
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/roles/{role_id}"
    payload = {"permissions": permissions}
    try:
        r = requests.patch(url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code == 404:
        typer.echo(
            "Error: role not found or you don't have permission.", err=True
        )
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    typer.echo(
        f"Updated role {role_name!r} on {owner}/{name}: "
        f"{', '.join(permissions)}."
    )


@role_app.command("delete")
def role_delete(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
    role_name: str = typer.Argument(..., help="Name of the role to delete."),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Unassign all ACL entries pointing at this role before deletion. "
            "Without --force, a role still assigned to any user returns 400."
        ),
    ),
) -> None:
    """Delete a custom role.

    System roles cannot be deleted from the CLI — they are immutable.
    """
    owner, name = _parse_canonical(canonical)
    c, headers = _ctx()
    role_id = _resolve_role_id(c, headers, owner, name, role_name)
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/roles/{role_id}"
    params = {"force": "true"} if force else None
    try:
        r = requests.delete(url, headers=headers, params=params, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code == 404:
        typer.echo(
            "Error: role not found or you don't have permission.", err=True
        )
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Deleted role {role_name!r} from {owner}/{name}.")
