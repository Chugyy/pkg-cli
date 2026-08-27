"""`pkg acl` — manage per-package access control lists.

Subcommands:
  list   <owner>/<name>                            -> list ACL entries
  add    <owner>/<name> <username> [--role <role>] -> grant a role
  remove <owner>/<name> <username>                 -> revoke access

The subcommand group is exposed as ``acl_app`` and registered into the main
``pkg`` Typer app from ``cli.py`` via ``app.add_typer(acl_app, name='acl')``.

Auth: requires a logged-in session (token in ~/.pkg/config.yaml). Without
authentication the command fails fast — no HTTP call is attempted.

Error handling: HTTP failures (404 / 4xx / network) are translated into a
human-readable message on stderr and a non-zero exit code. The CLI never
prints raw stack traces.
"""

from __future__ import annotations

import requests
import typer


# mod-14: VALID_ROLES has been DROPPED. The backend (mod-9 routes/acl.py) is
# now the single source of truth for role validation — it accepts both system
# roles (owner / maintainer / contributor) and custom roles defined per-package
# via `pkg role create`. The CLI must NOT pre-filter role names, otherwise it
# would silently break custom-role workflows (cf. cli-acl-1, cli-acl-2).


acl_app = typer.Typer(help="Manage package access control lists (ACL).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_canonical(canonical: str) -> tuple[str, str]:
    """Parse ``owner/name`` and exit(1) with a clear message on bad input.

    The parsing is intentionally strict and local: the ACL endpoints address
    packages by ``{owner}/{name}`` with no version suffix, so we reject any
    input that contains ``@``, a missing segment, or extra slashes BEFORE
    any HTTP call (cf. test_acl_canonical_invalid_format).
    """
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
    """Return ``(config, auth_headers)`` or exit(1) if not logged in.

    Lazy-imported from ``cli`` to avoid a circular import: ``cli.py``
    imports this module to register the subcommand group.
    """
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
    """Best-effort extraction of FastAPI's ``detail`` field for error display."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail")
        if detail:
            return str(detail)
    return response.text or f"HTTP {response.status_code}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@acl_app.command("list")
def acl_list(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
) -> None:
    """List ACL entries for a package."""
    owner, name = _parse_canonical(canonical)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/acl"
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
        data = r.json() or {}
    except ValueError:
        typer.echo("Error: invalid response from hub.", err=True)
        raise typer.Exit(1)

    items = data.get("items") or []
    if not items:
        typer.echo("No ACL entries.")
        return

    typer.echo(f"{'USERNAME':<20} {'ROLE':<20} GRANTED_AT")
    for entry in items:
        username = entry.get("username", "")
        # mod-14 / cli-acl-3: post mod-7 the API returns `role` as a nested
        # RoleSummary dict ({id, name, permissions, isSystem}). We extract
        # `role.name` for display, but stay forward-compatible with the
        # legacy plain-string shape used by older releases.
        raw_role = entry.get("role", "")
        if isinstance(raw_role, dict):
            role = str(raw_role.get("name", "") or "")
        else:
            role = str(raw_role)
        # BaseAPIModel emits camelCase aliases; also accept snake_case for
        # forward compatibility in case the API changes serialization.
        granted_at = entry.get("grantedAt") or entry.get("granted_at") or ""
        typer.echo(f"{username:<20} {role:<20} {granted_at}")


@acl_app.command("add")
def acl_add(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
    username: str = typer.Argument(..., help="Target username."),
    role: str = typer.Option(
        "contributor",
        "--role",
        help=(
            "Role to grant. Accepts any system role (owner, maintainer, "
            "contributor) OR a custom role defined for this package via "
            "`pkg role create`. The backend validates the name."
        ),
    ),
) -> None:
    """Add a user to the package ACL with the given role.

    mod-14: client-side ``VALID_ROLES`` filtering was REMOVED. Any non-empty
    string is now forwarded to the backend, which is the single source of
    truth for role validation. This unlocks custom roles (cli-acl-1/2).
    """
    owner, name = _parse_canonical(canonical)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/acl"
    payload = {"username": username, "role": role}

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
    if r.status_code in (400, 409, 422):
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Added {username} as {role} to {canonical}.")


@acl_app.command("remove")
def acl_remove(
    canonical: str = typer.Argument(..., help="Package id 'owner/name'."),
    username: str = typer.Argument(..., help="Username to remove from ACL."),
) -> None:
    """Remove a user from the package ACL."""
    owner, name = _parse_canonical(canonical)
    c, headers = _ctx()

    # The ACL DELETE endpoint expects ``user_id`` (UUID), so we first
    # resolve username -> id via the public profile route.
    user_url = f"{c['hub_url']}/api/users/{username}"
    try:
        ur = requests.get(user_url, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error looking up user: {e}", err=True)
        raise typer.Exit(1)

    if ur.status_code == 404:
        typer.echo(f"Error: user {username!r} not found.", err=True)
        raise typer.Exit(1)
    if ur.status_code >= 400:
        typer.echo(
            f"Error looking up user: {_extract_detail(ur)}", err=True
        )
        raise typer.Exit(1)

    try:
        user_id = (ur.json() or {})["id"]
    except (ValueError, KeyError):
        typer.echo(
            "Error: invalid user response from hub (missing 'id').",
            err=True,
        )
        raise typer.Exit(1)

    url = f"{c['hub_url']}/api/packages/{owner}/{name}/acl/{user_id}"
    try:
        r = requests.delete(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code == 404:
        typer.echo(
            "Error: ACL entry not found or no permission.",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Removed {username} from {canonical}.")
