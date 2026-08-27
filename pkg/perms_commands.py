"""`pkg perms` — describe the package permission catalog (iter #4 — mod-13).

Subcommands:
  list           -> show the 8 atomic package permissions
  system-roles   -> show the 3 system roles and their permission sets

Both commands are 100% OFFLINE — they read the static catalog from
``app.core.permissions`` (mod-2) and never call the hub. This is the
single source of truth for the CLI; if a 9th permission lands without
updating mod-2, the CLI list automatically reflects it (no drift).

NOTE — drift canary: tests/test_cli/test_perms_commands.py pins the
8-permission list. If mod-2 PACKAGE_PERMISSIONS adds a 9th item without
updating the test, CI breaks loudly (cf cross-fm-6 of fmea-report).
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table


perms_app = typer.Typer(help="Describe the package permission catalog.")

_console = Console(width=240, no_color=True, soft_wrap=True)


# ---------------------------------------------------------------------------
# Catalog loader — read from app.core.permissions when importable,
# else fall back to a hardcoded copy that MUST stay in sync (drift canary).
# ---------------------------------------------------------------------------


def _load_catalog() -> tuple[list[str], dict[str, list[str]]]:
    """Return (permissions_sorted, system_roles).

    Tries app.core.permissions first (single source of truth). When the
    backend package is not importable (e.g. the CLI is shipped standalone
    via pip without the backend code), we fall back to a hardcoded mirror
    that is exercised by tests/test_cli/test_perms_commands.py.
    """
    try:
        from app.core.permissions import (  # type: ignore
            PACKAGE_PERMISSIONS,
            SYSTEM_ROLES,
        )

        return sorted(PACKAGE_PERMISSIONS), {
            role: list(perms) for role, perms in SYSTEM_ROLES.items()
        }
    except Exception:
        permissions = [
            "package:delete",
            "package:manage_acl",
            "package:manage_roles",
            "package:publish",
            "package:read",
            "pr:auto_merge",
            "pr:create",
            "pr:review",
        ]
        system_roles = {
            "owner": sorted(permissions),
            "maintainer": [
                "package:read",
                "package:publish",
                "package:manage_acl",
                "package:manage_roles",
                "pr:create",
                "pr:review",
            ],
            "contributor": [
                "package:read",
                "pr:create",
            ],
        }
        return permissions, system_roles


# ---------------------------------------------------------------------------
# Permission descriptions — best-effort human gloss. Not the source of truth.
# ---------------------------------------------------------------------------

_PERM_DESCRIPTIONS: dict[str, str] = {
    "package:read":          "Read metadata and download archives.",
    "package:publish":       "Publish new versions of the package.",
    "package:delete":        "Delete the package (owner-only by default).",
    "package:manage_acl":    "Grant or revoke access for other users.",
    "package:manage_roles":  "Create, update, or delete custom roles.",
    "pr:create":             "Open pull requests against the package.",
    "pr:review":             "Review and approve pull requests.",
    "pr:auto_merge":         "Auto-merge approved pull requests.",
}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@perms_app.command("list")
def perms_list() -> None:
    """Show the 8 atomic package permissions (OFFLINE)."""
    permissions, _ = _load_catalog()
    table = Table(title="Package permissions", show_lines=False)
    table.add_column("Permission", style="bold")
    table.add_column("Description")
    for perm in permissions:
        table.add_row(perm, _PERM_DESCRIPTIONS.get(perm, ""))
    _console.print(table)


@perms_app.command("system-roles")
def perms_system_roles() -> None:
    """Show the 3 system roles and their permission sets (OFFLINE)."""
    _, system_roles = _load_catalog()
    table = Table(title="System roles", show_lines=False)
    table.add_column("Role", style="bold")
    table.add_column("Permissions")
    # Stable display order: owner > maintainer > contributor.
    order = ["owner", "maintainer", "contributor"]
    seen: set[str] = set()
    for role in order:
        if role in system_roles:
            seen.add(role)
            table.add_row(role, ", ".join(system_roles[role]))
    # Any extras (forward-compat) appended after the canonical three.
    for role, perms in system_roles.items():
        if role not in seen:
            table.add_row(role, ", ".join(perms))
    _console.print(table)
