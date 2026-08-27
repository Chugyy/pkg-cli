"""Lockfile management for pkg CLI.

File: .pkg-lock.yaml in the project root (cwd).
Format:
    entries:
      canonical/id:
        version: "1.0.0"
        resolved_at: "2026-06-30T10:00:00Z"  # optional — managed by callers
        deps: []                               # optional
"""
from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

import yaml

_LOCKFILE_NAME = ".pkg-lock.yaml"


def read_lockfile(cwd: Path) -> dict:
    """Return lockfile contents as a dict.

    Returns ``{"entries": {}}`` when ``.pkg-lock.yaml`` is absent.
    Never raises — a missing lockfile is a normal starting state.
    """
    lockfile = cwd / _LOCKFILE_NAME
    if not lockfile.exists():
        return {"entries": {}}
    with lockfile.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        return {"entries": {}}
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    return data


def write_lockfile(cwd: Path, data: dict) -> None:
    """Atomically write *data* to ``.pkg-lock.yaml`` inside *cwd*.

    Strategy: write to a temp file in the same directory, fsync, then
    ``os.replace`` into the final path. The temp file is cleaned up on
    failure when possible.

    ``PermissionError`` / ``OSError`` from the filesystem are propagated
    unchanged — callers are responsible for handling them.
    """
    lockfile = cwd / _LOCKFILE_NAME
    fd, tmp_str = tempfile.mkstemp(dir=cwd, prefix=".pkg-lock-", suffix=".tmp")
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.dump(
                data,
                fh,
                default_flow_style=False,
                sort_keys=True,
                allow_unicode=True,
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, lockfile)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upsert_entry(
    data: dict,
    canonical_id: str,
    version: str,
    deps: list | None = None,
) -> dict:
    """Return a new dict with *canonical_id* entry updated or inserted.

    Idempotent: calling twice with identical arguments produces identical
    output (the same logical entry is written, no mutable fields like
    timestamps are injected by this function).

    Entry shape written: ``{version: str, deps?: list}``.
    ``resolved_at`` is optional and is the caller's responsibility to set.
    If *deps* is ``None`` the ``deps`` key is omitted from the entry.
    """
    result = copy.deepcopy(data)
    if not isinstance(result.get("entries"), dict):
        result["entries"] = {}

    entry: dict = {"version": version}
    if deps is not None:
        entry["deps"] = deps

    result["entries"][canonical_id] = entry
    return result
