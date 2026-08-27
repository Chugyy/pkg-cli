"""Single-version dependency resolver for pkg CLI (Go-style).

Public API
----------
- ``DependencyCycleError`` — raised when a cycle is found in the transitive graph.
- ``resolve_install(canonical_id, version, *, hub_url, headers, cwd)`` — main entry point.
- ``_is_compatible(installed_version, constraint)`` — exposed for tests.

Security
--------
The *headers* dict (which contains the Bearer token) is NEVER stored in, logged,
printed, or repr'd inside any exception.  ``_HubError`` accepts only
``status_code``, ``url``, and a truncated response body.
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import requests
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    Timeout,
)

from pkg.lockfile import read_lockfile


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DependencyCycleError(Exception):
    """Raised when a cycle is detected in the transitive dependency graph.

    ``path`` is the full cycle (first node repeated at the end), e.g.
    ``["alice/a", "alice/b", "alice/c", "alice/a"]``.
    """

    def __init__(self, path: list[str]) -> None:
        self.path = path
        super().__init__("Dependency cycle detected: " + " -> ".join(path))


class _HubError(Exception):
    """HTTP error that deliberately excludes Authorization headers from its message."""

    def __init__(self, status_code: int, url: str, body: str) -> None:
        self.status_code = status_code
        # Truncate body to avoid very long messages; headers are never included.
        super().__init__(
            f"Hub returned HTTP {status_code} for {url!r}: {body[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Semver helpers
# ---------------------------------------------------------------------------


def _parse_semver(v: str) -> tuple[int, int, int]:
    """Parse 'x.y.z[-pre][+build]' into an integer 3-tuple.

    Pre-release and build metadata are stripped before comparison so that
    numeric ordering is strictly numeric (fixes the 1.10.0 > 1.9.0 case).
    """
    # Strip pre-release / build metadata
    v = v.strip().split("-")[0].split("+")[0]
    parts = v.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _is_compatible(installed_version: str, constraint: str) -> bool:
    """Return ``True`` if *installed_version* satisfies *constraint*.

    Supported operators (npm-style semver):
    - ``*`` or empty string — any version
    - ``x.y.z``             — exact match (no operator)
    - ``^x.y.z``            — compatible (locks major, or minor when major==0)
    - ``~x.y.z``            — patch-level (locks major+minor)
    - ``>=``, ``>``, ``<=``, ``<``, ``=`` — comparison operators
    """
    constraint = constraint.strip()

    if not constraint or constraint == "*":
        return True

    try:
        iv = _parse_semver(installed_version)
    except Exception:
        return False

    if constraint.startswith("^"):
        cv = _parse_semver(constraint[1:])
        if cv[0] > 0:
            # ^x.y.z  ->  >=x.y.z <(x+1).0.0
            return iv >= cv and iv < (cv[0] + 1, 0, 0)
        elif cv[1] > 0:
            # ^0.y.z  ->  >=0.y.z <0.(y+1).0
            return iv >= cv and iv < (0, cv[1] + 1, 0)
        else:
            # ^0.0.z  ->  >=0.0.z <0.0.(z+1)
            return iv >= cv and iv < (0, 0, cv[2] + 1)

    if constraint.startswith("~"):
        cv = _parse_semver(constraint[1:])
        # ~x.y.z  ->  >=x.y.z <x.(y+1).0
        return iv >= cv and iv < (cv[0], cv[1] + 1, 0)

    if constraint.startswith(">="):
        cv = _parse_semver(constraint[2:])
        return iv >= cv

    if constraint.startswith(">"):
        cv = _parse_semver(constraint[1:])
        return iv > cv

    if constraint.startswith("<="):
        cv = _parse_semver(constraint[2:])
        return iv <= cv

    if constraint.startswith("<"):
        cv = _parse_semver(constraint[1:])
        return iv < cv

    if constraint.startswith("="):
        cv = _parse_semver(constraint[1:])
        return iv == cv

    # No recognised operator: treat as exact match
    cv = _parse_semver(constraint)
    return iv == cv


# ---------------------------------------------------------------------------
# Cycle detection (DFS with path tracking)
# ---------------------------------------------------------------------------


def _detect_cycle(nodes: dict) -> list[str] | None:
    """Return the cycle path (first node repeated at end) if one exists, else ``None``.

    Uses recursive DFS with three-colour marking (white / gray / black).
    A gray node encountered during traversal means a back edge — cycle found.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in nodes}
    stack: list[str] = []

    def _dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for neighbor in nodes.get(node, {}).get("deps", []):
            if neighbor not in nodes:
                # Dep not part of the returned subgraph — skip
                continue
            state = color.get(neighbor, WHITE)
            if state == GRAY:
                # Back edge: extract the cycle segment from stack
                idx = stack.index(neighbor)
                return stack[idx:] + [neighbor]
            if state == WHITE:
                result = _dfs(neighbor)
                if result is not None:
                    return result
        stack.pop()
        color[node] = BLACK
        return None

    for node in nodes:
        if color[node] == WHITE:
            result = _dfs(node)
            if result is not None:
                return result
    return None


# ---------------------------------------------------------------------------
# HTTP fetch with retry + backoff
# ---------------------------------------------------------------------------

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1, 2, 4]  # sleep between attempt N and N+1


def _fetch_transitive(
    hub_url: str,
    canonical_id: str,
    version: str,
    headers: dict,
) -> dict:
    """GET transitive deps from hub.

    Retries on 5xx and network errors (Timeout / ConnectionError) using
    exponential backoff (1 s / 2 s / 4 s — up to 3 total attempts).
    Never retries on 4xx — raises immediately.

    The *headers* dict is forwarded to ``requests.get`` but is NEVER
    included in any exception message or logged.
    """
    owner, name = canonical_id.split("/", 1)
    url = (
        f"{hub_url}/api/packages/{owner}/{name}"
        f"/versions/{version}/deps/transitive"
    )

    last_error: Exception | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, timeout=(5, 30))

            if 500 <= resp.status_code < 600:
                # Transient server error: record and retry with backoff
                last_error = _HubError(resp.status_code, url, resp.text)
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                continue

            if resp.status_code >= 400:
                # Client error (404, 401, 403 …): fail immediately, no retry
                raise _HubError(resp.status_code, url, resp.text)

            resp.raise_for_status()
            return resp.json()

        except (Timeout, RequestsConnectionError) as exc:
            # Network-level failure: retry with backoff, without leaking details
            last_error = RuntimeError(
                f"Network error fetching {canonical_id}@{version} "
                f"(attempt {attempt + 1}/{_MAX_ATTEMPTS}): {type(exc).__name__}"
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS[attempt])
            continue

    # All attempts exhausted
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"Failed to fetch deps for {canonical_id}@{version} "
        f"after {_MAX_ATTEMPTS} attempts"
    )


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


def resolve_install(
    canonical_id: str,
    version: str,
    *,
    hub_url: str,
    headers: dict,
    cwd: Path,
) -> dict:
    """Resolve transitive dependencies for *canonical_id*@*version*.

    Algorithm (Go-style single-version resolver)
    --------------------------------------------
    1. ``GET {hub_url}/api/packages/{owner}/{name}/versions/{version}/deps/transitive``
       Retry + backoff on 5xx / network errors (1 s / 2 s / 4 s, 3 attempts max).
       4xx → raise immediately.
    2. Read the local lockfile via ``pkg.lockfile.read_lockfile(cwd)``.
    3. For each node in the transitive graph:
       - Not in lockfile → schedule for install.
       - In lockfile at same version → skip (idempotent).
       - In lockfile at incompatible version → record as conflict.
    4. Topological sort via Kahn's algorithm.  A cycle → ``DependencyCycleError``.
    5. Return ``{"plan": [...], "conflicts": [...]}``.

    Security
    --------
    *headers* is NEVER logged, repr'd, or embedded in any exception message.

    Returns
    -------
    dict with keys:
      ``plan``      – list of ``{dep_id, version, action}`` dicts in install order.
      ``conflicts`` – list of ``{dep_id, requested_version, installed_version,
                      requested_by}`` dicts for incompatible already-installed pkgs.

    Raises
    ------
    DependencyCycleError
        If the dependency graph contains a cycle.
    _HubError
        If the hub returns an HTTP error after all retries.
    """
    # Step 1: fetch transitive graph from hub
    graph = _fetch_transitive(hub_url, canonical_id, version, headers)

    nodes: dict = graph.get("nodes") or {}

    # Merge version constraints from top-level "constraints" map and per-node
    # "version_constraint" field (top-level wins on conflict).
    constraints: dict[str, str] = {}
    for node_id, node_data in nodes.items():
        vc = node_data.get("version_constraint")
        if vc:
            constraints[node_id] = vc
    # Top-level "constraints" overrides node-level values
    top_constraints: dict = graph.get("constraints") or {}
    constraints.update(top_constraints)

    # Pre-flight: validate constraint syntax before touching lockfile/sort
    for _nid, _con in constraints.items():
        _s = _con.strip()
        _c = next((_s[len(o):] for o in (">=", "<=", "^", "~", ">", "<", "=") if _s.startswith(o)), _s)
        if _c and _c != "*" and not _c[0].isdigit():
            raise ValueError(f"Invalid semver constraint {_con!r} for package {_nid!r}")

    # Step 2: read local lockfile (read-only — this function never mutates it)
    lockfile_data = read_lockfile(cwd)
    entries: dict = lockfile_data.get("entries") or {}

    # Step 3: cycle detection (before attempting Kahn's sort)
    cycle_path = _detect_cycle(nodes)
    if cycle_path is not None:
        raise DependencyCycleError(cycle_path)

    # Step 4: topological sort via Kahn's algorithm
    # Edge direction: node depends_on dep  →  dep must be installed before node.
    # reversed_adj[dep] = list of nodes that directly depend on dep.
    reversed_adj: dict[str, list[str]] = {n: [] for n in nodes}
    in_degree: dict[str, int] = {n: 0 for n in nodes}

    for node_id, node_data in nodes.items():
        for dep in node_data.get("deps", []):
            if dep in nodes:
                reversed_adj[dep].append(node_id)
                in_degree[node_id] += 1

    queue: deque[str] = deque(n for n in nodes if in_degree[n] == 0)
    sorted_nodes: list[str] = []

    while queue:
        node = queue.popleft()
        sorted_nodes.append(node)
        for dependent in reversed_adj[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Step 5: classify each node as install / skip / conflict
    plan: list[dict] = []
    conflicts: list[dict] = []

    for node_id in sorted_nodes:
        installed = entries.get(node_id)
        constraint = constraints.get(node_id)

        if installed is None:
            # Package not present locally → schedule install
            plan.append({
                "dep_id": node_id,
                "version": constraint or "latest",
                "action": "install",
            })
        else:
            installed_version: str = installed.get("version", "")
            if constraint and not _is_compatible(installed_version, constraint):
                # Incompatible version already installed → conflict
                requesters = [
                    nid
                    for nid, nd in nodes.items()
                    if node_id in nd.get("deps", [])
                ]
                conflicts.append({
                    "dep_id": node_id,
                    "requested_version": constraint,
                    "installed_version": installed_version,
                    "requested_by": requesters[0] if requesters else canonical_id,
                })
            # else: installed at a compatible version → idempotent skip

    return {"plan": plan, "conflicts": conflicts}
