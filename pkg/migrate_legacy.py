"""`pkg migrate-legacy` — Rename legacy single-segment package folders.

Walks a `packages/` directory and migrates each entry that uses the legacy
single-segment slug format (`telegram/`) to the new scoped `fs_slug` format
(`hugo--telegram/`). Already-migrated entries (those whose name contains the
`--` separator) are skipped silently.

For each legacy folder:
  1. Read `meta.yaml` if present, else fall back to the folder name for `name`.
  2. Resolve owner from `--owner` flag, OR interactively prompt the user.
  3. Validate both `owner` and `name` via `validate_name_format`.
  4. Move `packages/<name>/` -> `packages/<owner>--<name>/`.
  5. Update `meta.yaml` (write `owner`, `name`).

Safety:
  - If the target already exists, skip (do NOT overwrite, do NOT merge).
  - `--dry-run` prints the planned renames without touching the filesystem.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer
import yaml

from .identity_helpers import (
    NAME_FORMAT_DESCRIPTION,
    make_fs_slug,
    validate_name_format,
)


def _is_already_migrated(folder_name: str) -> bool:
    """A folder whose name contains '--' is considered already-migrated.

    The double-dash is reserved as the scoped fs_slug separator and cannot
    appear inside a valid `name` (the format regex forbids it).
    """
    return "--" in folder_name


def _load_meta(meta_path: Path) -> dict:
    """Read meta.yaml if it exists, otherwise return an empty dict."""
    if not meta_path.exists():
        return {}
    try:
        data = yaml.safe_load(meta_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise typer.Exit(f"Invalid YAML in {meta_path}: {e}")
    if not isinstance(data, dict):
        return {}
    return data


def _write_meta(meta_path: Path, meta: dict) -> None:
    """Persist the meta dict back to `meta.yaml`."""
    meta_path.write_text(yaml.safe_dump(meta, sort_keys=False))


def migrate_one(
    folder: Path,
    *,
    owner: Optional[str],
    dry_run: bool,
    target_parent: Path,
    interactive: bool = True,
) -> Optional[Path]:
    """Migrate a single folder. Returns the new path on success, None on skip.

    Pure-ish helper that the typer command wraps; broken out so tests can
    call it directly without spinning up Click's runner.
    """
    if not folder.is_dir():
        return None

    name_in_fs = folder.name

    # Already-migrated folders are silently skipped.
    if _is_already_migrated(name_in_fs):
        typer.echo(f"[skip] {name_in_fs} (already migrated)")
        return None

    meta_path = folder / "meta.yaml"
    meta = _load_meta(meta_path)

    # Pull `name` from meta.yaml if present, otherwise fall back to FS name.
    name = meta.get("name") or name_in_fs

    # Resolve owner: CLI flag wins; else prompt (or fail in non-interactive mode).
    owner_val = owner or meta.get("owner")
    if not owner_val:
        if not interactive:
            typer.echo(
                f"[error] {name_in_fs}: no owner provided (use --owner or "
                f"add `owner:` to meta.yaml)",
                err=True,
            )
            return None
        owner_val = typer.prompt(f"Owner for {name_in_fs}")

    # Validate format strictly before touching anything.
    try:
        validate_name_format(owner_val)
        validate_name_format(name)
    except ValueError as e:
        typer.echo(f"[error] {name_in_fs}: {e}", err=True)
        return None

    target_name = make_fs_slug(owner_val, name)
    target = target_parent / target_name

    if target.exists():
        typer.echo(
            f"[skip] {name_in_fs}: destination {target_name} already exists"
        )
        return None

    if dry_run:
        typer.echo(f"[dry-run] would rename {name_in_fs} -> {target_name}")
        return target

    # Do the rename, then enforce the canonical fields in meta.yaml.
    shutil.move(str(folder), str(target))
    new_meta_path = target / "meta.yaml"
    new_meta = _load_meta(new_meta_path)
    new_meta["owner"] = owner_val
    new_meta["name"] = name
    new_meta.setdefault("version", "0.0.0")
    _write_meta(new_meta_path, new_meta)

    typer.echo(f"[ok] renamed {name_in_fs} -> {target_name}")
    return target


def migrate_legacy_cmd(
    path: Path = typer.Argument(
        Path("packages"),
        help="Directory containing legacy package folders to migrate.",
    ),
    owner: Optional[str] = typer.Option(
        None,
        "--owner",
        help=(
            "Owner username to assign to every legacy folder. If omitted, "
            "the command prompts interactively for each folder."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print planned renames without modifying the filesystem.",
    ),
) -> None:
    """Rename legacy single-segment package folders to the scoped fs_slug format.

    Example:
        pkg migrate-legacy packages/ --owner hugo --dry-run
    """
    if not path.exists() or not path.is_dir():
        typer.echo(f"[error] {path} is not a directory", err=True)
        raise typer.Exit(1)

    migrated = 0
    skipped = 0
    for child in sorted(path.iterdir()):
        if not child.is_dir():
            continue
        # Hidden / dot folders are never considered packages.
        if child.name.startswith("."):
            continue
        result = migrate_one(
            child,
            owner=owner,
            dry_run=dry_run,
            target_parent=path,
            interactive=(owner is None),
        )
        if result is None:
            skipped += 1
        else:
            migrated += 1

    typer.echo(
        f"\n{migrated} folder(s) {'planned' if dry_run else 'migrated'}, "
        f"{skipped} skipped"
    )
