"""`pkg pr` — manage pull requests against packages on the hub.

Subcommands:
  create  <owner>/<name> <path> --title T --description D [--target-version V]
  list    [<owner>/<name>] [--status <s>] [--to-review]
  show    <owner>/<name>#<number>
  approve <owner>/<name>#<number> <body>
  reject  <owner>/<name>#<number> --body <text>
  comment <owner>/<name>#<number> --body <text>
  merge   <owner>/<name>#<number> [--target-version <v>]
  close   <owner>/<name>#<number>

The subcommand group is exposed as ``pr_app`` and registered into the main
``pkg`` Typer app from ``cli.py`` via ``app.add_typer(pr_app, name='pr')``
(mod-24 wiring).

Auth: requires a logged-in session (token in ~/.pkg/config.yaml). Without
authentication the command fails fast — no HTTP call is attempted.

Error handling: HTTP failures (404 / 4xx / 5xx / network) are translated
into a human-readable message on stderr and a non-zero exit code. The CLI
never prints raw stack traces or raw JSON envelopes.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import requests
import typer

from .identity_helpers import parse_canonical_id


pr_app = typer.Typer(help="Manage pull requests against packages on the hub.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> tuple[dict, dict]:
    """Return ``(config, auth_headers)`` or exit(1) if not logged in.

    Lazy-imported from ``cli`` to avoid a circular import: ``cli.py``
    imports this module to register the subcommand group (mod-24).
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


def _parse_owner_name(ref: str) -> tuple[str, str]:
    """Parse a plain ``owner/name`` package reference (create / list).

    Delegates the actual validation to ``cli._parse_package_ref`` so the
    error message and rules stay consistent with the rest of the CLI. Exits
    before any HTTP call on malformed input.
    """
    from .cli import _parse_package_ref

    canonical_id, _fs_slug, _version = _parse_package_ref(ref)
    owner, name = parse_canonical_id(canonical_id)
    return owner, name


def _parse_pr_ref(ref: str) -> tuple[str, str, int]:
    """Parse a PR reference ``owner/name#number`` used by show/approve/reject/
    comment/merge/close.

    Exits with a clear message on malformed input BEFORE any HTTP call.
    """
    raw = (ref or "").strip()
    if "#" not in raw:
        typer.echo(
            f"Error: invalid PR reference {raw!r}. Expected 'owner/name#number'.",
            err=True,
        )
        raise typer.Exit(1)
    head, _, num_str = raw.partition("#")
    try:
        owner, name = parse_canonical_id(head)
    except ValueError as e:
        typer.echo(
            f"Error: invalid PR reference {raw!r}. Expected 'owner/name#number'. {e}",
            err=True,
        )
        raise typer.Exit(1)
    if not num_str.isdigit():
        typer.echo(
            f"Error: invalid PR number in {raw!r}. Expected 'owner/name#number'.",
            err=True,
        )
        raise typer.Exit(1)
    return owner, name, int(num_str)


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


def _get(url: str, **kwargs) -> requests.Response:
    """``requests.get`` wrapped with clean connection-error handling."""
    try:
        return requests.get(url, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as e:
        typer.echo(f"Error: hub unreachable ({e})", err=True)
        raise typer.Exit(1)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


def _post(url: str, **kwargs) -> requests.Response:
    """``requests.post`` wrapped with clean connection-error handling."""
    try:
        return requests.post(url, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as e:
        typer.echo(f"Error: hub unreachable ({e})", err=True)
        raise typer.Exit(1)
    except requests.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


def _ensure_ok(r: requests.Response) -> dict:
    """Raise typer.Exit(1) with a human-readable message for non-2xx responses.

    Returns the parsed JSON body on success. Never echoes a raw JSON
    envelope or a Python stack trace to the user.
    """
    if r.status_code >= 400:
        typer.echo(f"Error: {_extract_detail(r)}", err=True)
        raise typer.Exit(1)
    try:
        return r.json() or {}
    except ValueError:
        typer.echo("Error: invalid response from hub.", err=True)
        raise typer.Exit(1)


def _build_tarball(path: Path) -> bytes:
    """Build a gzipped tarball of *path*, honoring the same exclusion rules
    used by ``pkg publish`` (DEFAULT_PUBLISH_IGNORES + package .gitignore).

    This guarantees secrets (.env, __pycache__, .venv, ...) never leak into
    a PR archive (mod-22.output.fm-10). The local user space (`user/`) is
    also excluded now, not just secrets — it never belongs in a distributed
    archive either.
    """
    from .cli import (
        DEFAULT_PUBLISH_IGNORES,
        _load_gitignore_patterns,
        _make_publish_filter,
    )

    patterns = DEFAULT_PUBLISH_IGNORES + _load_gitignore_patterns(path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(path, arcname=".", filter=_make_publish_filter(patterns))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@pr_app.command("create")
def pr_create(
    ref: str = typer.Argument(..., help="Package id 'owner/name'."),
    path: Path = typer.Argument(..., help="Path to the package directory to submit."),
    title: str = typer.Option(..., "--title", help="PR title."),
    description: str = typer.Option(..., "--description", help="PR description."),
    target_version: str = typer.Option(
        None, "--target-version", help="Target version to merge into (optional)."
    ),
) -> None:
    """Open a new PR against a package, uploading a fresh tarball of *path*."""
    owner, name = _parse_owner_name(ref)
    c, headers = _ctx()

    if not path.exists() or not path.is_dir():
        typer.echo(f"Error: {str(path)!r} is not a directory.", err=True)
        raise typer.Exit(1)

    tarball_bytes = _build_tarball(path)

    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs"
    data = {"title": title, "description": description}
    if target_version is not None:
        data["target_version"] = target_version

    r = _post(
        url,
        headers=headers,
        files={
            "file": ("archive.tar.gz", io.BytesIO(tarball_bytes), "application/gzip")
        },
        data=data,
        timeout=30,
    )
    body = _ensure_ok(r)
    number = body.get("number", "?")
    pr_status = body.get("status", "?")
    typer.echo(f"PR #{number} created (status: {pr_status})")


@pr_app.command("list")
def pr_list(
    ref: str = typer.Argument(
        None, help="Package id 'owner/name' (ignored with --to-review)."
    ),
    status: str = typer.Option(
        None, "--status", help="Filter by PR status (open, merged, closed, ...)."
    ),
    to_review: bool = typer.Option(
        False, "--to-review", help="List PRs awaiting your review."
    ),
) -> None:
    """List pull requests, either for a package or awaiting your review."""
    c, headers = _ctx()

    if to_review:
        url = f"{c['hub_url']}/api/me/prs/to-review"
        params: dict = {"page": 1, "limit": 20}
    else:
        if not ref:
            typer.echo(
                "Error: missing package reference 'owner/name' (or use --to-review).",
                err=True,
            )
            raise typer.Exit(1)
        owner, name = _parse_owner_name(ref)
        url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs"
        params = {"page": 1, "limit": 20}
        if status:
            params["status"] = status

    r = _get(url, headers=headers, params=params, timeout=10)
    data = _ensure_ok(r)

    items = data.get("items") or []
    if not items:
        typer.echo("No pull requests.")
        return

    typer.echo(f"{'#':<6} {'STATUS':<10} {'TITLE':<40} PROPOSER")
    for item in items:
        number = item.get("number", "")
        status_val = item.get("status", "")
        title = item.get("title", "")
        proposer = item.get("proposer") or {}
        username = (
            proposer.get("username", "") if isinstance(proposer, dict) else str(proposer)
        )
        typer.echo(f"{number!s:<6} {status_val:<10} {title:<40} {username}")


@pr_app.command("show")
def pr_show(
    ref: str = typer.Argument(..., help="PR reference 'owner/name#number'."),
) -> None:
    """Show the details of a single PR."""
    owner, name, number = _parse_pr_ref(ref)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs/{number}"
    r = _get(url, headers=headers, timeout=10)
    data = _ensure_ok(r)

    number_out = data.get("number", number)
    title = data.get("title", "")
    status_val = data.get("status", "")
    description = data.get("description", "")
    proposer = data.get("proposer") or {}
    username = (
        proposer.get("username", "") if isinstance(proposer, dict) else str(proposer)
    )

    typer.echo(f"PR #{number_out}: {title}")
    typer.echo(f"Status: {status_val}")
    if username:
        typer.echo(f"Proposer: {username}")
    if description:
        typer.echo(description)


@pr_app.command("approve")
def pr_approve(
    ref: str = typer.Argument(..., help="PR reference 'owner/name#number'."),
    body: str = typer.Argument(..., help="Review comment."),
) -> None:
    """Approve a PR with a required review comment."""
    owner, name, number = _parse_pr_ref(ref)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs/{number}/reviews"
    payload = {"verdict": "approve", "body": body}
    r = _post(url, json=payload, headers=headers, timeout=10)
    data = _ensure_ok(r)
    verdict = data.get("verdict", "approve")
    typer.echo(f"Review posted: {verdict}")


@pr_app.command("reject")
def pr_reject(
    ref: str = typer.Argument(..., help="PR reference 'owner/name#number'."),
    body: str = typer.Option(..., "--body", help="Reason for rejection (required)."),
) -> None:
    """Reject a PR. ``--body`` is required and validated before any HTTP call."""
    if not body or not body.strip():
        typer.echo("Error: --body is required for reject.", err=True)
        raise typer.Exit(2)
    owner, name, number = _parse_pr_ref(ref)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs/{number}/reviews"
    payload = {"verdict": "reject", "body": body}
    r = _post(url, json=payload, headers=headers, timeout=10)
    data = _ensure_ok(r)
    verdict = data.get("verdict", "reject")
    typer.echo(f"Review posted: {verdict}")


@pr_app.command("comment")
def pr_comment(
    ref: str = typer.Argument(..., help="PR reference 'owner/name#number'."),
    body: str = typer.Option(..., "--body", help="Comment text (required)."),
) -> None:
    """Leave a comment-only review on a PR (no approve/reject verdict)."""
    if not body or not body.strip():
        typer.echo("Error: --body is required for comment.", err=True)
        raise typer.Exit(2)
    owner, name, number = _parse_pr_ref(ref)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs/{number}/reviews"
    payload = {"verdict": "comment", "body": body}
    r = _post(url, json=payload, headers=headers, timeout=10)
    data = _ensure_ok(r)
    verdict = data.get("verdict", "comment")
    typer.echo(f"Review posted: {verdict}")


@pr_app.command("merge")
def pr_merge(
    ref: str = typer.Argument(..., help="PR reference 'owner/name#number'."),
    target_version: str = typer.Option(
        None, "--target-version", help="Target version to merge into (optional)."
    ),
) -> None:
    """Merge a PR, producing a new package version."""
    owner, name, number = _parse_pr_ref(ref)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs/{number}/merge"
    payload: dict = {}
    if target_version is not None:
        payload["target_version"] = target_version
    r = _post(url, json=payload, headers=headers, timeout=10)
    data = _ensure_ok(r)
    version = data.get("version") or data.get("versionId") or data.get("version_id") or "?"
    typer.echo(f"Merged into version {version}")


@pr_app.command("close")
def pr_close(
    ref: str = typer.Argument(..., help="PR reference 'owner/name#number'."),
) -> None:
    """Close a PR without merging."""
    owner, name, number = _parse_pr_ref(ref)
    c, headers = _ctx()
    url = f"{c['hub_url']}/api/packages/{owner}/{name}/prs/{number}/close"
    r = _post(url, headers=headers, timeout=10)
    _ensure_ok(r)
    typer.echo("PR closed")
