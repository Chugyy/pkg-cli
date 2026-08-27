"""`pkg notifications` — manage the current user's notification feed.

Subcommands:
  list                        -> list notifications (optionally --unread-only)
  read <id>                   -> mark one notification as read
  read-all                    -> mark all notifications as read

The subcommand group is exposed as ``notifications_app`` and registered into
the main ``pkg`` Typer app from ``cli.py`` via
``app.add_typer(notifications_app, name='notifications')``.

Auth: requires a logged-in session (token in ~/.pkg/config.yaml). Without
authentication the command fails fast — no HTTP call is attempted.

Error handling: HTTP failures (404 / 4xx / network) are translated into a
human-readable message on stderr and a non-zero exit code. The CLI never
prints raw stack traces.
"""

from __future__ import annotations

import requests
import typer


notifications_app = typer.Typer(help="Manage your notification feed.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


@notifications_app.command("list")
def notifications_list(
    unread_only: bool = typer.Option(
        False,
        "--unread-only",
        help="Only show unread notifications.",
    ),
) -> None:
    """List notifications for the current user."""
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/notifications"
    params: dict = {"limit": 50, "offset": 0}
    if unread_only:
        params["unread_only"] = "true"

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
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
        typer.echo("No notifications.")
        return

    for item in items:
        notif_id = item.get("id", "")
        kind = item.get("kind", "")
        created_at = item.get("createdAt") or item.get("created_at") or ""
        typer.echo(f"{notif_id}  {kind}  {created_at}")


@notifications_app.command("read")
def notifications_read(
    notification_id: str = typer.Argument(..., help="Notification id."),
) -> None:
    """Mark a single notification as read."""
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/notifications/{notification_id}/read"

    try:
        r = requests.post(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code == 404:
        typer.echo("Error: notification not found.", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Notification {notification_id} marked read.")


@notifications_app.command("read-all")
def notifications_read_all() -> None:
    """Mark all notifications as read."""
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/notifications/mark-all-read"

    try:
        r = requests.post(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)

    try:
        data = r.json() or {}
    except ValueError:
        data = {}

    count = data.get("marked", 0)
    typer.echo(f"{count} notifications marked read.")
