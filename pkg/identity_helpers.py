"""Canonical package identity helpers — CLI shim.

This is a copy of `app.core.utils.identity` kept inside the `pkg_cli` package
so that the CLI ships as a fully standalone Python distribution (no dependency
on the `app` import path). The two modules MUST stay in sync; both contain
pure-stdlib functions (no DB, no I/O).

A package is identified by an immutable couple `(owner, name)`. Two derived
strings exist on top of that couple:

- `canonical_id = f"{owner}/{name}"` — global, scoped, npm/Docker-style.
- `fs_slug      = f"{owner}--{name}"` — filesystem-safe flat slug (no slash).
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Constants — KEEP IN SYNC with app/core/utils/identity.py
# ---------------------------------------------------------------------------

NAME_FORMAT_RE = re.compile(
    r"^[a-z0-9][a-z0-9_\-]{0,38}[a-z0-9]$|^[a-z0-9]{1,3}$"
)

NAME_FORMAT_DESCRIPTION = (
    "must be 1-40 lowercase alphanumeric chars, optionally with `-` or `_`, "
    "starting and ending with alphanumeric"
)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def make_canonical_id(owner: str, name: str) -> str:
    """Build the canonical identifier `owner/name`."""
    return f"{owner}/{name}"


def make_fs_slug(owner: str, name: str) -> str:
    """Build the filesystem-safe slug `owner--name` (no slash)."""
    return f"{owner}--{name}"


def parse_canonical_id(canonical_id: str) -> tuple[str, str]:
    """Split a canonical id into `(owner, name)`.

    Raises:
        ValueError: if `canonical_id` does not contain exactly one slash, or
            if either half is empty.
    """
    if canonical_id is None:
        raise ValueError("canonical_id must not be None")
    if canonical_id.count("/") != 1:
        raise ValueError(
            f"Invalid canonical_id {canonical_id!r}: must be exactly "
            f"'owner/name' (one '/' separator)"
        )
    owner, name = canonical_id.split("/", 1)
    if not owner or not name:
        raise ValueError(
            f"Invalid canonical_id {canonical_id!r}: both owner and name "
            f"must be non-empty"
        )
    return owner, name


def validate_name_format(name: str) -> None:
    """Validate that `name` matches `NAME_FORMAT_RE`.

    Raises:
        ValueError: if `name` is empty, None, or does not match the regex.
    """
    if not name:
        raise ValueError(f"name must not be empty ({NAME_FORMAT_DESCRIPTION})")
    if not NAME_FORMAT_RE.match(name):
        raise ValueError(
            f"Invalid name {name!r}: {NAME_FORMAT_DESCRIPTION}"
        )
