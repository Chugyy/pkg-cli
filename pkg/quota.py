"""`pkg quota` — show the current user's package and storage quota usage.

Single command (no arguments): `pkg quota`.

The subcommand group is exposed as ``quota_app`` and registered into the main
``pkg`` Typer app from ``cli.py`` via ``app.add_typer(quota_app, name='quota')``
(mod-26 wiring). Since ``quota_app`` has no further subcommands, the callback
below (``invoke_without_command=True``) runs directly on `pkg quota` — no
extra subcommand name is needed.

Auth: requires a logged-in session (token in ~/.pkg/config.yaml). Without
authentication the command fails fast — no HTTP call is attempted.

Error handling: HTTP failures (401 / 5xx / network) are translated into a
human-readable message on stderr and a non-zero exit code. The CLI never
prints raw stack traces or raw JSON envelopes.
"""

from __future__ import annotations

import requests
import typer


quota_app = typer.Typer(help="Show your package and storage quota usage.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> tuple[dict, dict]:
    """Return ``(config, auth_headers)`` or exit(1) if not logged in.

    Lazy-imported from ``cli`` to avoid a circular import: ``cli.py``
    imports this module to register the subcommand group (mod-26).
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


def _format_mb(num_bytes: int) -> str:
    """Render a byte count as a human-readable MB string (e.g. '5.0 MB')."""
    mb = (num_bytes or 0) / 1048576
    return f"{mb:.1f} MB"


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@quota_app.callback(invoke_without_command=True)
def quota() -> None:
    """Show current usage against your package and storage quota limits."""
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/users/me/quota"

    try:
        r = requests.get(url, headers=headers, timeout=10)
    except (requests.ConnectionError, requests.Timeout) as e:
        typer.echo(f"Error: hub unreachable ({e})", err=True)
        raise typer.Exit(1)
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

    packages_used = data.get("packages_used", 0)
    packages_limit = data.get("packages_limit", 0)
    storage_used_bytes = data.get("storage_used_bytes", 0)
    storage_limit_bytes = data.get("storage_limit_bytes", 0)

    typer.echo(f"Packages: {packages_used} / {packages_limit}")
    typer.echo(
        f"Storage:  {_format_mb(storage_used_bytes)} / {_format_mb(storage_limit_bytes)}"
    )
