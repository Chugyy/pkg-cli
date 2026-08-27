from __future__ import annotations
import fnmatch, hashlib, json, logging, os, shutil, subprocess, sys, tarfile, tempfile, threading
from pathlib import Path
from typing import Literal
import typer, httpx, requests, yaml

from .identity_helpers import (
    NAME_FORMAT_DESCRIPTION,
    make_canonical_id,
    make_fs_slug,
    parse_canonical_id,
    validate_name_format,
)
from .migrate_legacy import migrate_legacy_cmd

# Patterns toujours exclus, en plus du .gitignore du package, sur les 3 canaux
# distribues qui delegue a `_make_publish_filter` / `_make_copytree_ignore`
# (`pkg publish`, `pkg pr create`, `pkg fork`). Couvre deux categories
# distinctes : les artefacts de dev jetables/secrets (venv, caches, backups,
# fichiers macOS, `.env`) qui n'ont jamais leur place dans une distribution, ET
# `/user` — pas un artefact jetable, mais l'espace de donnee UTILISATEUR locale
# (runtime state, config personnelle) : jamais distribuable, mais preserve sur
# disque a l'install/update (cf. `safe_extract`), pas supprime.
DEFAULT_PUBLISH_IGNORES = [
    '.venv', '.runs', '.executions', '__pycache__', '*.egg-info',
    '*.bak', '_legacy', 'tmp', '.env', '._*', '.DS_Store',
    'node_modules', '.next', '.git', '/user',
]


def _load_gitignore_patterns(path: Path) -> list[str]:
    """Load ignore patterns from both .gitignore AND .pkg-ignore.

    .pkg-ignore takes precedence when patterns overlap; both are additive.
    The hub rejects tarballs with secrets / symlinks / nested meta.yaml, so
    a package can carry a stricter .pkg-ignore than its .gitignore (e.g. to
    exclude local `.env.local.backup-*` files that git tracks intentionally
    but must never ship).
    """
    patterns: list[str] = []
    for fname in ('.gitignore', '.pkg-ignore'):
        f = path / fname
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            patterns.append(line)
    return patterns


def _path_is_ignored(rel: str, patterns: list[str]) -> bool:
    """Fonction pure de matching gitignore-like, partagee par les 3 canaux
    distribues (tarfile publish/pr, copytree fork).

    True = exclure. False = conserver.

    Le test est applique sur :
    - chaque segment du chemin relatif (pour matcher des noms de dir/fichier comme `.venv`)
    - le chemin relatif complet (pour matcher des globs comme `**/foo.bak`)

    Handles gitignore root-anchor syntax: a leading `/` in a pattern means
    "match only at the top level" — we translate this to matching the full
    relative path (parts[0]) rather than any segment.

    CONTRAT D'ENTREE (verrouille par test-cli-01) : `rel` doit etre un chemin
    relatif '/'-separe DEJA normalise — ni prefixe './', ni chemin absolu, ni
    separateur OS. Un `rel` non normalise fait echouer SILENCIEUSEMENT les
    patterns ancres racine (aucune erreur, juste un fichier qui echappe au
    filtre). La normalisation reste la responsabilite de l'appelant
    (`_make_publish_filter` pour tarfile, `_make_copytree_ignore` pour
    shutil.copytree).
    """
    parts = rel.split('/')
    for pat in patterns:
        # Strip trailing slash dans les patterns gitignore type "node_modules/"
        clean = pat.rstrip('/')
        if not clean:
            continue
        # gitignore root-anchor: '/foo' means "match top-level foo only".
        # fnmatch doesn't understand this, so translate to a top-level check.
        if clean.startswith('/'):
            anchored = clean[1:]
            if not anchored:
                continue
            if parts and fnmatch.fnmatch(parts[0], anchored):
                return True
            # Also allow root-anchored globs like `/tmp/*` to match nested paths.
            if fnmatch.fnmatch(rel, anchored):
                return True
            continue
        if any(fnmatch.fnmatch(p, clean) for p in parts):
            return True
        if fnmatch.fnmatch(rel, clean):
            return True
    return False


def _normalize_rel(name: str) -> str:
    """Normalise un nom de membre brut (tarfile `info.name`, ou `dir/name` de
    copytree) vers le contrat d'entree exige par `_path_is_ignored` : chemin
    relatif '/'-separe, sans prefixe `./`, sans separateur OS.

    Partagee par les 3 call sites qui dupliquaient cette logique (DRY) :
    `_make_publish_filter`, `_make_copytree_ignore`, `safe_extract`.

    Strippe AUSSI tout `/` de tete (chemin absolu). C'est deliberement plus
    strict que le seul retrait de `./` : un membre d'archive nomme
    `/user/secret.db` a `parts[0] == ''` une fois splitte sur `/`, ce qui fait
    ECHOUER SILENCIEUSEMENT l'ancrage racine `/user` de `_path_is_ignored`
    (aucun des deux tests, ni `parts[0]` ni le chemin complet, ne matche un nom
    commencant par `/`) — alors que `tarfile.TarFile.extractall` normalise en
    interne un nom absolu en le rendant relatif (simple `lstrip` du `/` de
    tete), donc SANS ce strip ici le membre echappe au filtre mais atterrit
    quand meme sous `dest/user/`, ecrasant l'espace utilisateur local.
    """
    rel = name.replace(os.sep, '/')
    if rel.startswith('./'):
        rel = rel[2:]
    return rel.lstrip('/')


def _make_publish_filter(patterns: list[str]):
    """Construit un filter callable pour tarfile.add qui exclut les patterns donnes.

    Wrapper fin autour de `_path_is_ignored` : ne garde que ce qui ne peut pas
    descendre dans la fonction pure de matching —

    - skip integral des symlinks / hardlinks : le hub rejette tout tarball qui
      en contient, et un symlink extrait peut ecrire hors du dossier cible.
    - court-circuit de '.' et '' (la racine de l'archive, arcname='.').
    - normalisation de `info.name` via `_normalize_rel` AVANT delegation
      (ATTENTION : `lstrip('./')` traite les caracteres individuellement et
      mangerait aussi les dots de '.venv', '.runs' — d'ou le helper partage
      plutot qu'un strip naif).
    """
    def _filter(info: tarfile.TarInfo):
        # Skip symlinks entirely — the hub rejects them, and pkg-cli users
        # commonly have dev symlinks (e.g. .profiles, packages) they never
        # meant to publish. Better to filter than surface a cryptic 400.
        if info.issym() or info.islnk():
            return None
        if info.name == '.':
            return info
        rel = _normalize_rel(info.name)
        if not rel:
            return info
        if _path_is_ignored(rel, patterns):
            return None
        return info
    return _filter


def _make_copytree_ignore(patterns: list[str], source_root):
    """Construit un callable compatible `shutil.copytree(ignore=...)`.

    CPython appelle `ignore(os.fspath(src), names)` : `dir_path` est donc un
    **str**, jamais un Path — pas de `dir_path.relative_to(...)` possible ici.

    Utilise `os.path.relpath(dir_path, source_root)` (et NON `Path.relative_to`,
    non robuste au melange relatif/absolu — `fork()` passe un `source_dir`
    relatif de la forme `packages/<slug>`).

    ATTENTION au piege racine : `os.path.relpath(source_root, source_root)`
    vaut '.'  — le rel d'une entree RACINE doit donc etre `name` seul, jamais
    `'./name'`, sinon l'ancrage racine (`/user`) ne matche jamais a l'endroit
    ou il doit matcher.

    Le set RETOURNE contient des BASENAMES (contrat `shutil.copytree`), pas
    des chemins relatifs complets — retourner des chemins relatifs n'exclurait
    rien (no-op silencieux).

    Delegue a `_path_is_ignored` (memes semantiques exactes que le moteur
    tarfile de publish/pr, ancrage racine inclus). La normalisation finale
    (separateurs OS, prefixe `./`, `/` de tete) passe par `_normalize_rel`,
    partagee avec `_make_publish_filter` et `safe_extract`.
    """
    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        rel_dir = os.path.relpath(dir_path, source_root)
        excluded: set[str] = set()
        for name in names:
            raw = name if rel_dir == '.' else f"{rel_dir}/{name}"
            rel = _normalize_rel(raw)
            if _path_is_ignored(rel, patterns):
                excluded.add(name)
        return excluded
    return _ignore

app = typer.Typer(help='Package Hub CLI')
CFG = Path.home()/'.pkg/config.yaml'
LOCK = Path('.pkg-lock.yaml')
PKG_DIR = Path('packages')

# Visibility values accepted by the hub. Matches the canonical_visibility enum
# enforced server-side (mod-9 schema + mod-10 publish_package). Keeping this
# in sync prevents a CLI publish from being rejected by the API for a typo.
VALID_VISIBILITIES = ('public', 'private', 'shared')

# Distribution channels recognized by the hub (version discipline). Mirrors
# app.core.jobs.package.VALID_CHANNELS — 'dev' accumulates merged features,
# 'stable' is only reached by promoting an existing validated dev version.
VALID_CHANNELS = ('dev', 'stable')

def cfg():
    return yaml.safe_load(CFG.read_text()) if CFG.exists() else {'hub_url': os.getenv('PACKAGE_HUB_URL','http://localhost:8000')}

def save_cfg(c):
    CFG.parent.mkdir(parents=True, exist_ok=True); CFG.write_text(yaml.safe_dump(c))

def client_headers():
    c=cfg(); return {'Authorization': f"Bearer {c.get('token')}"} if c.get('token') else {}

_lock_mutex = threading.Lock()

def lock_load(): return yaml.safe_load(LOCK.read_text()) if LOCK.exists() else {'packages': {}}
def lock_save(d): LOCK.write_text(yaml.safe_dump(d, sort_keys=False))

HANDLER_URL = os.getenv('HANDLER_URL', 'http://localhost:8700')

_logger = logging.getLogger(__name__)


def _resolve_strategy(canonical_id: str) -> Literal['auto', 'manual']:
    """Resolve the update strategy for *canonical_id*.

    Priority order:
      1. Per-package override in lock: ``lock['packages'][canonical_id]['update_strategy']``
      2. Global config: ``cfg()['update_strategy']``
      3. Default: ``'auto'``

    Raises:
        ValueError: if the value read at step 1 or 2 is neither ``'auto'`` nor ``'manual'``.
    """
    _VALID = ('auto', 'manual')

    pkg_override = lock_load().get('packages', {}).get(canonical_id, {}).get('update_strategy')
    if pkg_override is not None:
        if pkg_override not in _VALID:
            raise ValueError(
                f"Invalid per-package update_strategy {pkg_override!r} for {canonical_id!r}: "
                f"expected one of {_VALID}."
            )
        return pkg_override

    global_strategy = cfg().get('update_strategy')
    if global_strategy is not None:
        if global_strategy not in _VALID:
            raise ValueError(
                f"Invalid global update_strategy {global_strategy!r} in config: "
                f"expected one of {_VALID}."
            )
        return global_strategy

    return 'auto'


def _check_updates_batch(
    packages: dict[str, str],
    hub_url: str,
    timeout: float = 30.0,
    channel: str | None = None,
) -> dict[str, dict]:
    """Batch-check installed packages for available updates.

    POSTs ``{"packages": {canonical_id: current_version, ...}}`` to
    ``{hub_url}/api/packages/sync-check`` and returns the ``"packages"``
    dict from the response (mapping canonical_id -> info dict with at least
    ``"latest_version"``).

    ``channel`` (version discipline): when set, the hub resolves ``latest``
    WITHIN that channel (non-yanked versions only) — so a stable-channel
    instance never sees a dev-only or yanked version as an update.

    Best-effort semantics — returns ``{}`` on any of:
    - ``httpx.RequestError`` (network / connection / timeout errors)
    - ``httpx.HTTPStatusError`` (4xx / 5xx from the hub)
    - ``json.JSONDecodeError`` (malformed response body)
    - Response JSON missing the ``"packages"`` key (unexpected hub schema)

    The caller (sync command) must continue gracefully when this returns
    an empty dict.
    """
    body_payload: dict = {"packages": packages}
    if channel:
        body_payload["channel"] = channel
    try:
        resp = httpx.post(
            f"{hub_url}/api/packages/sync-check",
            json=body_payload,
            headers=client_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except httpx.RequestError as exc:
        _logger.warning("sync-check request failed (network/timeout): %s", exc)
        return {}
    except httpx.HTTPStatusError as exc:
        _logger.warning("sync-check HTTP error %s: %s", exc.response.status_code, exc)
        return {}
    except json.JSONDecodeError as exc:
        _logger.warning("sync-check malformed JSON in response: %s", exc)
        return {}

    if "packages" not in body:
        _logger.warning(
            "sync-check response missing 'packages' key (got keys: %s)",
            list(body.keys()),
        )
        return {}

    return body["packages"]


def _entry_has_update(info: dict) -> bool:
    """Extract the has-update flag from a sync-check response entry.

    The hub serializes camelCase (``hasUpdate`` — BaseAPIModel's
    ``alias_generator=to_camel``); snake_case (``has_update``) is kept as a
    fallback for older hubs and hand-built payloads. Reading only the
    snake_case key against a real hub response silently reports "no update"
    for every package — `pkg sync` becomes a no-op forever.
    """
    return bool(info.get('hasUpdate', info.get('has_update')))


def _entry_latest(info: dict) -> str | None:
    """Extract the latest version from a sync-check response entry.

    The hub's key is ``latest`` (see SyncCheckResponseEntry); the historical
    snake_case ``latest_version`` is kept as a fallback.
    """
    return info.get('latest', info.get('latest_version'))


def _configured_channel() -> str | None:
    """Read and validate the ``channel:`` key from ``~/.pkg/config.yaml``.

    A typo'd channel (e.g. ``stble``) sent to the hub would resolve every
    ``latest`` to None — the instance silently believes it is up to date
    forever. Fail loudly instead of stalling silently.
    """
    channel = cfg().get('channel')
    if channel is not None and channel not in VALID_CHANNELS:
        typer.echo(
            f"invalid channel {channel!r} in config ({CFG}): must be one of "
            f"{', '.join(VALID_CHANNELS)}. Fix the `channel:` key before retrying.",
            err=True,
        )
        raise typer.Exit(1)
    return channel


# ---------------------------------------------------------------------------
# Helpers — canonical_id parsing
# ---------------------------------------------------------------------------


def _parse_package_ref(ref: str) -> tuple[str, str, str]:
    """Parse a user-facing package reference like ``owner/name`` or ``owner/name@version``.

    Returns ``(canonical_id, fs_slug, version)`` where ``version`` is the
    explicit version suffix or ``'latest'`` if none was provided.

    Exits with a clear error message on malformed input — this is the only
    place CLI commands accept a package identifier, so we fail fast and
    consistently.
    """
    raw = ref.strip()
    if '@' in raw:
        head, _, version = raw.partition('@')
    else:
        head, version = raw, 'latest'
    try:
        owner, name = parse_canonical_id(head)
        validate_name_format(owner)
        validate_name_format(name)
    except ValueError as e:
        typer.echo(
            f"Invalid package reference {ref!r}: expected 'owner/name' "
            f"format (e.g. 'hugo/telegram'). {e}",
            err=True,
        )
        raise typer.Exit(1)
    canonical_id = make_canonical_id(owner, name)
    fs_slug = make_fs_slug(owner, name)
    return canonical_id, fs_slug, version


def check_rule_conflicts(new_meta: dict, packages_dir: Path) -> list:
    """Verifie les conflits de rules entre le nouveau package et les existants."""
    conflicts = []
    new_rules = new_meta.get('rules', [])
    if not new_rules:
        return conflicts
    new_id = new_meta.get('id', 'unknown')
    for pkg_dir in packages_dir.iterdir():
        if not pkg_dir.is_dir() or pkg_dir.name.startswith('.'):
            continue
        meta_path = pkg_dir / 'meta.yaml'
        if not meta_path.exists():
            continue
        existing_meta = yaml.safe_load(meta_path.read_text())
        if not existing_meta:
            continue
        existing_rules = existing_meta.get('rules', [])
        existing_id = existing_meta.get('id', pkg_dir.name)
        if existing_id == new_id:
            continue
        for new_rule in new_rules:
            new_match = new_rule.get('match', {})
            for existing_rule in existing_rules:
                existing_match = existing_rule.get('match', {})
                if (new_match.get('source') == existing_match.get('source') and
                    new_match.get('type') == existing_match.get('type')):
                    conflicts.append(
                        f"Rule conflict: {new_match} in '{new_id}' conflicts with '{existing_id}'"
                    )
    return conflicts


def reload_handler_rules():
    """Demande au handler de recharger ses rules."""
    try:
        requests.post(f'{HANDLER_URL}/reload-rules', timeout=5)
    except requests.ConnectionError:
        pass


def verify_hash(content: bytes, expected: str) -> bool:
    actual = 'sha256:' + hashlib.sha256(content).hexdigest()
    return actual == expected


def _run_reindex_hook(canonical_id: str, timeout: int) -> None:
    """Synchronous reindex hook run post-extract (AC-16 immediate addressability).

    Sub-package bundles must be addressable right after `pkg install` returns,
    not after the next async registry-watcher poll. Mechanism: a `registry.py
    build` subprocess in the workspace root (cwd) — mirrors the existing
    setup.sh subprocess+timeout+non-silent-warning pattern above, as a
    distinct hook (no shared abstraction, no refactor of setup.sh handling).

    Best-effort (AC-17 / fm-2): if `registry.py` is absent from the workspace
    root, the mechanism is considered unavailable for this workspace — skip
    with a non-blocking notice and return normally (exit 0), leaving a
    plain-package install functionally identical to today.

    No destructive rollback on failure (fm-3 / fm-5): a failing or erroring
    reindex subprocess degrades the install (non-zero exit, differentiated
    actionable warning with a manual-rebuild hint) but never rolls back the
    already-extracted package directory or the already-saved lock entry. A
    later `pkg install` re-attempt replays this hook and repairs the index
    (idempotence).
    """
    registry_script = Path.cwd() / 'registry.py'
    if not registry_script.exists():
        typer.echo(
            f"No registry.py found in workspace root — skipping reindex for '{canonical_id}'. "
            "Index not refreshed automatically; run `python3 registry.py build` manually "
            "once the workspace registry is available.",
            err=True,
        )
        return
    try:
        result = subprocess.run(
            [sys.executable, 'registry.py', 'build'],
            cwd=str(Path.cwd()),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        typer.echo(
            f"Reindex timed out for '{canonical_id}' after {timeout}s. Package installed "
            "but the index was not refreshed — run `python3 registry.py build` manually "
            "to repair the index.",
            err=True,
        )
        raise typer.Exit(1)
    if result.returncode != 0:
        typer.echo(
            f"Reindex failed for '{canonical_id}' (registry.py build exited "
            f"{result.returncode}). Package installed but the index was not refreshed "
            "and the package may not be addressable yet — run `python3 registry.py build` "
            "manually to repair the index.",
            err=True,
        )
        raise typer.Exit(1)


def _parse_deps_validation_body(r, canonical_id: str) -> None:
    """Parse a 422 deps_validation_failed response and render human-readable lines.

    Returns normally if error_code is not deps_validation_failed (caller then
    falls through to raise_for_status).  Raises typer.Exit(1) when the body is
    parsed and each detail is rendered — so the caller never sees raw JSON.
    """
    try:
        body = r.json()
    except Exception:
        body = {}
    if not (isinstance(body, dict) and body.get("error_code") == "deps_validation_failed"):
        return
    typer.echo(f"Dependency validation failed for '{canonical_id}':", err=True)
    for detail in body.get("details", []):
        pkg = detail.get("package", "?")
        constraint = detail.get("constraint", "")
        reason = detail.get("reason", "")
        line = f"  {pkg}"
        if constraint:
            line += f" ({constraint})"
        if reason:
            line += f": {reason}"
        typer.echo(line, err=True)
    raise typer.Exit(1)


def safe_extract(tar: tarfile.TarFile, dest: Path):
    """Extract `tar` into `dest`, safely.

    `dest` is created by the caller via `dest.mkdir(parents=True,
    exist_ok=True)` right before this call. A `dest` that already contains
    entries at this point means this call is an UPDATE over a pre-existing
    install (mod-1 volet B) — a freshly created `dest` (first install) is
    always empty at this point. On an update, any archive member whose
    top-level path segment is `user` is skipped so the archive can never
    overwrite locally-preserved user data (`dest/user/`), whether the
    archive predates mod-4's publish-time exclusion or is malicious. First
    installs extract every member unfiltered.

    Absolute-path / traversal validation is done UNCONDITIONALLY, for every
    member, BEFORE branching on the Python version (review k1 W-1 fix): a
    member named e.g. `/user/secret.db` is rejected outright on every
    supported version. This used to be legacy-branch-only (`py<3.12`); on
    `py>=3.12`, `tarfile.extractall(..., filter='data')` does NOT reject an
    absolute member — it silently normalizes it to a relative path (stripping
    the leading `/`) and extracts it anyway. Combined with the `/user` skip
    filter reasoning on the RAW member name (which never strips a leading
    `/`, only `./`), an absolute `/user/secret.db` member used to slip past
    both defenses and land at `dest/user/secret.db`, overwriting locally
    preserved user data. Rejecting it unconditionally closes that gap on both
    branches, matching the invariant the legacy branch already enforced.
    """
    all_members = tar.getmembers()
    for m in all_members:
        p = Path(m.name)
        if p.is_absolute() or '..' in p.parts:
            raise ValueError(f'Unsafe path in archive: {m.name}')
        if m.issym() or m.islnk():
            link = Path(m.linkname)
            if link.is_absolute() or '..' in link.parts:
                raise ValueError(f'Unsafe symlink in archive: {m.name} -> {m.linkname}')
    skip_user = any(dest.iterdir())
    members = all_members
    if skip_user:
        members = [
            m for m in all_members
            if not _path_is_ignored(_normalize_rel(m.name), ['/user'])
        ]
    if sys.version_info >= (3, 12):
        tar.extractall(dest, members=members, filter='data')
    else:
        tar.extractall(dest, members=members)


@app.command()
def login(email: str = typer.Argument(None), password: str = typer.Argument(None),
          hub_url: str = typer.Option('http://localhost:8000'),
          token: str = typer.Option(None, '--token', help='Authenticate with a personal access token instead of email/password')):
    """Log in with email+password, or with a personal access token (--token)."""
    if token:
        c={'hub_url':hub_url,'token':token}; save_cfg(c); typer.echo('Logged in with token'); return
    if not email or not password:
        typer.echo('Provide <email> <password>, or use --token <PAT>', err=True); raise typer.Exit(1)
    r=requests.post(f'{hub_url}/api/auth/login', json={'email':email,'password':password}); r.raise_for_status()
    c={'hub_url':hub_url,'token':r.json()['token']}; save_cfg(c); typer.echo('Logged in')


token_app = typer.Typer(help='Manage personal access tokens')
app.add_typer(token_app, name='token')

@token_app.command('create')
def token_create(name: str):
    """Create a personal access token (requires an existing session). Prints the raw token once."""
    c=cfg(); r=requests.post(f"{c['hub_url']}/api/auth/tokens", headers=client_headers(), json={'name':name}); r.raise_for_status()
    typer.echo(r.json()['token'])

@token_app.command('list')
def token_list():
    """List your active access tokens (metadata only)."""
    c=cfg(); r=requests.get(f"{c['hub_url']}/api/auth/tokens", headers=client_headers()); r.raise_for_status()
    for t in r.json().get('items',[]):
        last=t.get('lastUsedAt') or 'never'
        typer.echo(f"{t['id']}  {t['name']}  {t['tokenPrefix']}…  (last used: {last})")

@token_app.command('revoke')
def token_revoke(token_id: str):
    """Revoke one of your access tokens by id."""
    c=cfg(); r=requests.delete(f"{c['hub_url']}/api/auth/tokens/{token_id}", headers=client_headers()); r.raise_for_status()
    typer.echo('Revoked')


@app.command()
def auth(token: str, hub_url: str = typer.Option('http://localhost:8000')):
    """Authenticate directly with a personal access token (writes config without a password)."""
    c={'hub_url':hub_url,'token':token}; save_cfg(c); typer.echo('Authenticated with token')


@app.command()
def whoami():
    """Print the username of the currently authenticated user.

    Useful for diagnosing 'owner mismatch' errors at publish-time — the
    `owner` field in meta.yaml must match what this command returns.
    """
    c = cfg()
    headers = client_headers()
    if not headers:
        typer.echo('Not logged in (run `pkg login` or `pkg auth <token>`)', err=True)
        raise typer.Exit(1)
    try:
        r = requests.get(f"{c['hub_url']}/api/users/me", headers=headers, timeout=10)
        r.raise_for_status()
    except requests.HTTPError as e:
        typer.echo(f'whoami failed: {e}', err=True)
        raise typer.Exit(1)
    data = r.json() or {}
    username = data.get('username') or data.get('email') or '<unknown>'
    typer.echo(username)


@app.command()
def search(query: str = '', type: str | None = None):
    """Search the hub catalog. Items are printed as `canonical_id@version`."""
    c = cfg()
    r = requests.get(f"{c['hub_url']}/api/search", params={'q': query, 'type': type})
    r.raise_for_status()
    for p in r.json().get('items', []):
        # Backend returns `canonical_id` (owner/name). Old `package_id` is gone.
        canonical_id = p.get('canonical_id') or p.get('package_id') or '?/?'
        typer.echo(
            f"{canonical_id}@{p.get('latest_version')} "
            f"[{p['type']}] - {p['description']}"
        )


@app.command()
def info(package_id: str):
    """Print package details. `package_id` must be in `owner/name` format."""
    canonical_id, _fs_slug, _version = _parse_package_ref(package_id)
    owner, name = parse_canonical_id(canonical_id)
    c = cfg()
    r = requests.get(f"{c['hub_url']}/api/packages/{owner}/{name}")
    r.raise_for_status()
    typer.echo(json.dumps(r.json(), indent=2, default=str))


@app.command()
def publish(
    path: Path,
    visibility: str = typer.Option(
        None,
        '--visibility',
        help=(
            "Package visibility: public, private, or shared. Overrides "
            "meta.yaml. Default: value in meta.yaml, or 'private' if absent."
        ),
    ),
    changelog: str = '',
    channel: str = typer.Option(
        'dev',
        '--channel',
        help=(
            "Distribution channel the version is published to (dev|stable). "
            "Version discipline: publish lands in 'dev'; 'stable' is reached "
            "by PROMOTING a validated dev version (`pkg promote`), never by "
            "re-publishing."
        ),
    ),
):
    """Publish a package archive built from `path`.

    Local validation runs BEFORE any network call: meta.yaml must exist and
    must declare `owner` and `name` (both passing `validate_name_format`).
    This prevents anonymous or malformed publishes (cross-FM-2).

    Visibility resolution order (most specific first):
      1. ``--visibility`` CLI flag (if provided)
      2. ``visibility`` field in meta.yaml (if present and valid)
      3. Default ``'private'`` (secure default — even in non-TTY/CI runs we
         never silently leak a package to the world).

    The resolved value is sent to the hub as form-data alongside the tarball.

    The tarball never includes `user/` (local, non-distributable user data —
    see `DEFAULT_PUBLISH_IGNORES`), regardless of the package's own .gitignore.
    """
    meta_path = path / 'meta.yaml'
    if not meta_path.exists():
        typer.echo(
            f"publish failed: {meta_path} not found. A package must contain "
            "a meta.yaml at its root.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        meta = yaml.safe_load(meta_path.read_text()) or {}
    except yaml.YAMLError as e:
        typer.echo(f"publish failed: invalid YAML in {meta_path}: {e}", err=True)
        raise typer.Exit(1)
    if not isinstance(meta, dict):
        typer.echo(
            f"publish failed: {meta_path} must be a YAML mapping (got "
            f"{type(meta).__name__})",
            err=True,
        )
        raise typer.Exit(1)
    owner = meta.get('owner')
    name = meta.get('name')
    if not owner or not name:
        typer.echo(
            "publish failed: meta.yaml must declare both 'owner' and 'name' "
            f"fields (got keys: {sorted(meta.keys())}). Example:\n"
            "  owner: alice\n"
            "  name: my-package",
            err=True,
        )
        raise typer.Exit(1)
    try:
        validate_name_format(owner)
        validate_name_format(name)
    except ValueError as e:
        typer.echo(
            f"publish failed: invalid owner/name in meta.yaml. {e}",
            err=True,
        )
        raise typer.Exit(1)

    # --- Resolve final visibility ---------------------------------------
    # We validate BEFORE building the tarball so an invalid value fails
    # fast (no temp file, no upload), matching the cross-FM-2 pattern.
    valid_list = ", ".join(VALID_VISIBILITIES)
    if visibility is not None:
        # CLI flag wins over meta.yaml.
        if visibility not in VALID_VISIBILITIES:
            typer.echo(
                f"publish failed: invalid --visibility {visibility!r}. "
                f"Valid values: {valid_list}.",
                err=True,
            )
            raise typer.Exit(1)
        resolved_visibility = visibility
    elif 'visibility' in meta:
        meta_vis = meta.get('visibility')
        if meta_vis not in VALID_VISIBILITIES:
            typer.echo(
                f"publish failed: invalid 'visibility' value {meta_vis!r} "
                f"in meta.yaml. Valid values: {valid_list}.",
                err=True,
            )
            raise typer.Exit(1)
        resolved_visibility = meta_vis
    else:
        # Default to 'private' — secure default for non-TTY/CI runs.
        resolved_visibility = 'private'

    canonical_id = make_canonical_id(owner, name)

    # --- Validate channel (fail-fast, before tarball/upload) -------------
    if channel not in VALID_CHANNELS:
        typer.echo(
            f"publish failed: invalid --channel {channel!r}. "
            f"Valid values: {', '.join(VALID_CHANNELS)}.",
            err=True,
        )
        raise typer.Exit(1)

    # All local checks passed — build the tarball and POST.
    c = cfg()
    tmp = Path(tempfile.mkdtemp()) / 'pkg.tar.gz'
    patterns = DEFAULT_PUBLISH_IGNORES + _load_gitignore_patterns(path)
    with tarfile.open(tmp, 'w:gz') as tar:
        tar.add(path, arcname='.', filter=_make_publish_filter(patterns))
    with tmp.open('rb') as f:
        r = requests.post(
            f"{c['hub_url']}/api/packages",
            headers=client_headers(),
            files={'file': ('package.tar.gz', f, 'application/gzip')},
            data={
                'changelog': changelog,
                'visibility': resolved_visibility,
                'channel': channel,
            },
        )
    if r.status_code == 409:
        # Server detected a name collision under another owner — surface the
        # suggested alternative so the user can rename and retry.
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        suggestion = body.get('detail', {}).get('suggestion') if isinstance(body.get('detail'), dict) else body.get('suggestion')
        typer.echo(
            f"publish failed: name '{canonical_id}' is already taken by "
            f"another owner. Suggested alternative: {suggestion or '<none>'}",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code == 403:
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        detail = body.get('detail') if isinstance(body, dict) else None
        typer.echo(
            f"publish failed: forbidden. {detail or 'You can only publish '\
            'under your own username. Run `pkg whoami` to check.'}",
            err=True,
        )
        raise typer.Exit(1)
    r.raise_for_status()
    typer.echo(json.dumps(r.json(), indent=2, default=str))


@app.command()
def promote(
    package_ref: str = typer.Argument(..., help="Package version to promote: owner/name@version (explicit version required)"),
    to: str = typer.Option(..., '--to', help=f"Target channel ({'|'.join(VALID_CHANNELS)})"),
):
    """Promote an EXISTING published version to another channel.

    Version discipline: a release to `stable` is the promotion of an exact,
    validated `dev` version — the hub re-tags the SAME artifact (same tarball,
    same checksum). No re-upload, no rebuild, no new version number. A yanked
    version is never promoted (the hub refuses with 409).
    """
    canonical_id, _fs_slug, version = _parse_package_ref(package_ref)
    if version == 'latest':
        typer.echo(
            "promote failed: an explicit version is required "
            "(owner/name@x.y.z). Promotion re-tags an EXACT validated "
            "version — never a moving 'latest'.",
            err=True,
        )
        raise typer.Exit(1)
    if to not in VALID_CHANNELS:
        typer.echo(
            f"promote failed: invalid --to {to!r}. "
            f"Valid values: {', '.join(VALID_CHANNELS)}.",
            err=True,
        )
        raise typer.Exit(1)
    owner, name = parse_canonical_id(canonical_id)
    c = cfg()
    r = requests.post(
        f"{c['hub_url']}/api/packages/{owner}/{name}/versions/{version}/promote",
        headers=client_headers(),
        json={'channel': to},
    )
    if r.status_code == 409:
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        detail = body.get('detail') if isinstance(body, dict) else None
        message = detail.get('message') if isinstance(detail, dict) else detail
        typer.echo(
            f"promote refused: {message or f'{canonical_id}@{version} is yanked (known-bad)'}",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code == 403:
        typer.echo(
            f"promote failed: forbidden — only the package owner can promote "
            f"{canonical_id}.",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code == 404:
        typer.echo(f"promote failed: {canonical_id}@{version} not found", err=True)
        raise typer.Exit(1)
    r.raise_for_status()
    body = r.json()
    if body.get('already_in_channel'):
        typer.echo(
            f"{canonical_id}@{version} already in channel '{to}' "
            f"(channels: {', '.join(body.get('channels', []))}) — no-op"
        )
    else:
        typer.echo(
            f"Promoted {canonical_id}@{version} -> {to} "
            f"(same artifact, hash {body.get('archive_hash', '')[:12]}…; "
            f"channels: {', '.join(body.get('channels', []))})"
        )


@app.command()
def yank(
    package_ref: str = typer.Argument(..., help="Package version to yank: owner/name@version (explicit version required)"),
    reason: str = typer.Option('', '--reason', help="Why this version is known-bad (audit trail)"),
):
    """Mark a published version as YANKED (known-bad).

    A yanked version is never resolved as latest, never promoted, and
    `pkg sync` never pulls it. Only an explicitly pinned
    `pkg install owner/name@version` still serves it — with a warning.
    Nothing is deleted: the artifact and its number remain (audit + pin).

    WARNING: yanking is IRREVERSIBLE via the API — there is no un-yank
    endpoint. Double-check the owner/name@version before executing.
    """
    canonical_id, _fs_slug, version = _parse_package_ref(package_ref)
    if version == 'latest':
        typer.echo(
            "yank failed: an explicit version is required (owner/name@x.y.z).",
            err=True,
        )
        raise typer.Exit(1)
    owner, name = parse_canonical_id(canonical_id)
    c = cfg()
    r = requests.post(
        f"{c['hub_url']}/api/packages/{owner}/{name}/versions/{version}/yank",
        headers=client_headers(),
        json={'reason': reason},
    )
    if r.status_code == 403:
        typer.echo(
            f"yank failed: forbidden — only the package owner can yank "
            f"{canonical_id}.",
            err=True,
        )
        raise typer.Exit(1)
    if r.status_code == 404:
        typer.echo(f"yank failed: {canonical_id}@{version} not found", err=True)
        raise typer.Exit(1)
    r.raise_for_status()
    body = r.json()
    if body.get('already_yanked'):
        typer.echo(f"{canonical_id}@{version} was already yanked — no-op")
    else:
        typer.echo(
            f"Yanked {canonical_id}@{version}"
            + (f" ({reason})" if reason else "")
            + f". New latest: {body.get('latest_version') or '<none>'}"
        )


@app.command()
def install(
    package_id: str,
    version: str = 'latest',
    timeout: int = typer.Option(300, '--timeout', help='Timeout in seconds for setup.sh and download'),
):
    """Install a package by canonical id (`owner/name` or `owner/name@version`).

    The local folder is created as `packages/<owner>--<name>/` (fs_slug). The
    lock file keys entries by `canonical_id` (the global address).

    On an update over an already-installed package, `user/` is always
    preserved (never overwritten by the archive). Rollback on extraction
    failure is conditional: a dest that pre-existed before this install is
    left on disk in a partial state instead of being wiped — re-run
    `pkg install` to repair it.
    """
    canonical_id, fs_slug, parsed_version = _parse_package_ref(package_id)
    owner, name = parse_canonical_id(canonical_id)
    ver = parsed_version if parsed_version != 'latest' else version

    # Fix 1 (test-cli-21): pre-flight — validate lockfile integrity before any
    # network call or filesystem mutation.  A corrupt lockfile discovered only
    # inside _lock_mutex (after extraction) would leave packages/<slug>/ on disk.
    if LOCK.exists():
        try:
            yaml.safe_load(LOCK.read_text())
        except yaml.YAMLError:
            typer.echo(
                f"Corrupt lockfile: {LOCK} — remove or repair it before installing",
                err=True,
            )
            raise typer.Exit(1)

    c = cfg()
    # Version discipline: when resolving 'latest' and a channel is configured
    # (`channel:` key in ~/.pkg/config.yaml — e.g. 'dev' on dev-integration,
    # 'stable' on fleet instances), resolve latest WITHIN that channel.
    # Explicit version pins bypass channels entirely.
    params = {}
    configured_channel = _configured_channel()
    if ver == 'latest' and configured_channel:
        params['channel'] = configured_channel
    # Fix 3 (test-cli-26): catch Timeout so the user gets a readable message
    # instead of an unhandled exception propagating through CliRunner.
    try:
        r = requests.get(
            f"{c['hub_url']}/api/packages/{owner}/{name}/versions/{ver}/download",
            params=params or None,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        typer.echo(f"Download timed out for '{canonical_id}'", err=True)
        raise typer.Exit(1)
    # Fix 4 (test-cli-33): intercept 422 deps_validation_failed before
    # raise_for_status swallows the body — render each detail as a human-readable
    # line so the user knows which constraint failed.
    if r.status_code == 422:
        _parse_deps_validation_body(r, canonical_id)
    r.raise_for_status()
    # Version discipline: an explicitly pinned yanked version is still served
    # by the hub, but NEVER silently — surface the warning loudly. ('latest'
    # can never resolve to a yanked version, so this only fires on pins.)
    if r.headers.get('X-Version-Yanked') == 'true':
        yank_reason = r.headers.get('X-Version-Yanked-Reason', '')
        typer.echo(
            f"WARNING: {canonical_id}@{ver} is YANKED (known-bad)"
            + (f": {yank_reason}" if yank_reason else "")
            + ". Installing anyway because the version is explicitly pinned.",
            err=True,
        )
    expected_hash = r.headers.get('X-Archive-Hash')
    if expected_hash and not verify_hash(r.content, expected_hash):
        typer.echo('Hash mismatch — archive may be corrupted or tampered', err=True)
        raise typer.Exit(1)

    # Pre-extract: read meta.yaml from archive and check rule conflicts
    tmp = Path(tempfile.mkdtemp()) / 'pkg.tar.gz'
    tmp.write_bytes(r.content)
    pre_meta = {}
    with tarfile.open(tmp, 'r:gz') as tar:
        for member in tar.getmembers():
            if member.name.endswith('meta.yaml') and '/' not in member.name.replace('./', ''):
                f = tar.extractfile(member)
                if f:
                    pre_meta = yaml.safe_load(f.read()) or {}
                    break
    if pre_meta and PKG_DIR.exists():
        conflicts = check_rule_conflicts(pre_meta, PKG_DIR)
        if conflicts:
            for conflict in conflicts:
                typer.echo(conflict, err=True)
            raise typer.Exit(1)

    dest = PKG_DIR / fs_slug
    # mod-1 volet A: captured BEFORE mkdir — distinguishes a first install
    # (dest doesn't exist yet) from an update over an already-installed
    # package (dest already exists, e.g. via `pkg sync` or a repeat
    # `pkg install`).
    dest_preexisted = dest.exists()
    # Extract OVER the existing install instead of wiping it. Service packages
    # keep runtime data beside their code (.env, *.db, .venv, node_modules,
    # .next, data/) that is not part of the archive — a blind rmtree would
    # destroy secrets and databases on every update.
    dest.mkdir(parents=True, exist_ok=True)
    # Fix 2 (test-cli-22) + mod-1 volet A: if safe_extract raises OSError
    # (e.g. ENOSPC), a dest freshly created by mkdir above is rolled back so
    # packages/ is left completely untouched. A dest that PRE-EXISTED before
    # this install (update path) is NOT rolled back — an unconditional
    # rmtree would destroy locally-preserved user data (user/, .env, *.db,
    # data/) that never came from the failed archive. The exception is left
    # to propagate as a clean exit 1, and dest is left on disk in a partial
    # state (existing code/data intact, new extraction interrupted),
    # repairable by re-running `pkg install`.
    try:
        with tarfile.open(tmp, 'r:gz') as tar:
            safe_extract(tar, dest)
    except OSError as e:
        if not dest_preexisted:
            shutil.rmtree(dest, ignore_errors=True)
            typer.echo(
                f"Disk full or I/O error during extraction of '{canonical_id}': {e}",
                err=True,
            )
        else:
            typer.echo(
                f"Disk full or I/O error during extraction of '{canonical_id}': {e}. "
                f"'{dest}' was left in a partial, inconsistent state (not rolled "
                "back) so pre-existing user data is preserved — re-run "
                "`pkg install` to repair it.",
                err=True,
            )
        raise typer.Exit(1)
    # review k1 W-2: `dest.rglob('meta.yaml')` is recursive and its traversal
    # order is NOT contractual — without a guard, a `user/meta.yaml` (locally
    # preserved, mod-1) can be returned instead of the package's own root
    # meta.yaml, letting user-controlled data dictate the resolved version /
    # lock entry. Filter out anything under `/user` via the SAME engine
    # `safe_extract` already uses, so only a genuine root-level meta.yaml
    # (or a legitimate sub-package one) is ever considered.
    meta_path = next(
        p for p in dest.rglob('meta.yaml')
        if not _path_is_ignored(str(p.relative_to(dest)).replace(os.sep, '/'), ['/user'])
    )
    meta = yaml.safe_load(meta_path.read_text()) or {}
    with _lock_mutex:
        l = lock_load()
        l.setdefault('packages', {})[canonical_id] = {
            'version': meta.get('version', 'unknown'),
            'archive_hash': expected_hash or '',
            'fs_slug': fs_slug,
            'mode': 'sync',
        }
        lock_save(l)
    setup = dest / 'setup.sh'
    if setup.exists():
        try:
            result = subprocess.run(['bash', 'setup.sh'], cwd=str(dest), timeout=timeout)
            if result.returncode != 0:
                typer.echo(
                    f"setup.sh for '{canonical_id}' failed (exit {result.returncode}). "
                    "Package extracted but not fully configured.",
                    err=True,
                )
                raise typer.Exit(1)
        except subprocess.TimeoutExpired:
            typer.echo(f"setup.sh for '{canonical_id}' timed out after {timeout}s", err=True)
            raise typer.Exit(1)
    # Synchronous reindex hook (AC-16): freshly-installed sub-packages must be
    # addressable immediately, not after the next async registry-watcher poll.
    # Best-effort (AC-17): no rollback of the extract/lock entry on failure.
    _run_reindex_hook(canonical_id, timeout)
    # Reload handler rules
    reload_handler_rules()
    typer.echo(f'Installed {canonical_id}@{meta.get("version", "unknown")}')


@app.command('list')
def list_installed():
    """List installed packages keyed by `canonical_id`."""
    for k, v in lock_load().get('packages', {}).items():
        typer.echo(f"{k}@{v.get('version')} ({v.get('mode')})")


@app.command()
def remove(
    package_id: str,
    force: bool = typer.Option(
        False,
        '--force',
        help='Delete a non-empty user/ without confirmation (destructive).',
    ),
):
    """Remove an installed package by canonical id (`owner/name`).

    mod-2: a non-empty `dest/user/` is never destroyed silently. Without
    `--force`, an interactive TTY is asked to confirm (default: refuse);
    a non-interactive context (CI/cron/pipe) refuses systematically —
    never a silent prompt. `--force` skips the check entirely. A refusal
    exits 1 and leaves the package directory AND its lockfile entry
    untouched (no rmtree, no lock_save, no reload_handler_rules).
    """
    canonical_id, fs_slug, _version = _parse_package_ref(package_id)
    # Prefer the fs_slug recorded in the lock (handles rare cases where the
    # legacy slug differs from the derived fs_slug); fall back to derived.
    l = lock_load()
    entry = l.get('packages', {}).get(canonical_id, {})
    install_dir = entry.get('fs_slug') or fs_slug
    dest = PKG_DIR / install_dir

    user_dir = dest / 'user'
    user_dir_nonempty = user_dir.is_dir() and any(user_dir.iterdir())

    if user_dir_nonempty and not force:
        if sys.stdin.isatty():
            confirmed = typer.confirm(
                f"'{user_dir}' is not empty. Removing {canonical_id} will "
                "permanently delete it. Continue?",
                default=False,
            )
            if not confirmed:
                typer.echo(
                    f"Aborted: {canonical_id} was not removed — '{user_dir}' is "
                    "not empty. Re-run with --force to delete it anyway.",
                    err=True,
                )
                raise typer.Exit(1)
        else:
            typer.echo(
                f"Refusing to remove {canonical_id}: '{user_dir}' is not empty "
                "and no TTY is attached to confirm. Re-run with --force to "
                "delete it anyway.",
                err=True,
            )
            raise typer.Exit(1)

    shutil.rmtree(dest, ignore_errors=True)
    # Re-read the lockfile here (not the `l` loaded above, before the
    # confirmation prompt): int-1 (interference report) — the initial load
    # only serves to *decide* (install_dir/fs_slug resolution, user_dir
    # check), but the unbounded `typer.confirm` wait above can let a
    # concurrent writer (another `pkg install`/`fork`/`config set-strategy`)
    # commit a full lock_load/lock_save while this process is suspended.
    # Reusing the stale pre-prompt snapshot here would clobber that write on
    # save. Re-loading fresh right before the mutate+save keeps the
    # read-modify-write window back down to the same few microseconds it had
    # before mod-2 introduced the prompt.
    l = lock_load()
    l.get('packages', {}).pop(canonical_id, None)
    lock_save(l)
    reload_handler_rules()
    typer.echo(f'Removed {canonical_id}')


@app.command()
def fork(
    source_ref: str = typer.Argument(..., help="Package to fork (owner/name)"),
    new_name: str = typer.Argument(None, help="Optional new name (defaults to source name)"),
    as_: str = typer.Option(None, '--as', help="Fully qualified target owner/name (must match your username)"),
):
    """Fork an installed package locally under your own namespace.

    Fail-fast order (mod-8.fm-7): the source-installed check runs BEFORE any
    network call, so a typo'd or never-installed source never triggers a
    whoami round-trip.
    """
    source_canonical_id, _source_fs_slug, _version = _parse_package_ref(source_ref)

    l = lock_load()
    entry = l.get('packages', {}).get(source_canonical_id)
    if entry is None:
        typer.echo(f"package not installed locally: {source_canonical_id}", err=True)
        raise typer.Exit(1)

    # --- Whoami (single online dependency of this command) -----------------
    c = cfg()
    headers = client_headers()
    if not headers:
        typer.echo('not logged in, run: pkg login', err=True)
        raise typer.Exit(1)
    try:
        r = requests.get(f"{c['hub_url']}/api/users/me", headers=headers, timeout=10)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        typer.echo(f"cannot reach hub: {e}", err=True)
        raise typer.Exit(1)
    try:
        r.raise_for_status()
    except requests.HTTPError:
        # Both 401 (bad/expired token) and any other unexpected status share
        # the same guidance — the fix is always to re-authenticate.
        typer.echo('authentication failed, run: pkg login', err=True)
        raise typer.Exit(1)
    data = r.json() or {}
    username = data.get('username')
    if not username:
        typer.echo('authentication failed, run: pkg login', err=True)
        raise typer.Exit(1)

    # --- Resolve target owner/name ------------------------------------------
    if as_:
        parts = as_.split('/', 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            typer.echo(
                f"Invalid --as value {as_!r}: expected 'owner/name'", err=True
            )
            raise typer.Exit(1)
        as_owner, as_name = parts
        if as_owner != username:
            typer.echo(
                "cannot publish to namespace of another user (--as owner must "
                "match current user)",
                err=True,
            )
            raise typer.Exit(1)
        target_owner = as_owner
        target_name = as_name
    elif new_name:
        target_owner = username
        target_name = new_name
    else:
        target_owner = username
        target_name = parse_canonical_id(source_canonical_id)[1]

    try:
        validate_name_format(target_name)
    except ValueError as e:
        typer.echo(f"Invalid target name {target_name!r}: {e}", err=True)
        raise typer.Exit(1)

    # --- Auto-suffix on collision (mod-8.fm-8) ------------------------------
    target_fs_slug = make_fs_slug(target_owner, target_name)
    if (PKG_DIR / target_fs_slug).exists():
        base_name = target_name
        for suffix in range(2, 100):
            candidate_name = f"{base_name}-{suffix}"
            candidate_slug = make_fs_slug(target_owner, candidate_name)
            if not (PKG_DIR / candidate_slug).exists():
                target_name = candidate_name
                target_fs_slug = candidate_slug
                break
        else:
            typer.echo(
                f"cannot fork: exhausted suffix -2..-99 for {target_owner}/{base_name}",
                err=True,
            )
            raise typer.Exit(1)

    # --- Copy tree with runtime-state exclusions (mod-8.fm-21 CATASTROPHIC) -
    source_dir = PKG_DIR / entry['fs_slug']
    target_dir = PKG_DIR / target_fs_slug
    shutil.copytree(source_dir, target_dir, ignore=_make_copytree_ignore(DEFAULT_PUBLISH_IGNORES, source_dir))

    # --- Rewrite target meta.yaml atomically --------------------------------
    meta_path = target_dir / 'meta.yaml'
    meta = yaml.safe_load(meta_path.read_text()) or {}
    meta['owner'] = target_owner
    meta['name'] = target_name
    meta['version'] = '0.1.0'
    meta['forked_from'] = f"{source_canonical_id}@{entry['version']}"
    _atomic_yaml_write(meta_path, meta)

    # --- Register in lock ----------------------------------------------------
    target_canonical_id = make_canonical_id(target_owner, target_name)
    with _lock_mutex:
        l = lock_load()
        l.setdefault('packages', {})[target_canonical_id] = {
            'version': '0.1.0',
            'fs_slug': target_fs_slug,
            'mode': 'forked',
            'forked_from': f"{source_canonical_id}@{entry['version']}",
            'archive_hash': '',
        }
        lock_save(l)

    typer.echo(
        f"Forked {source_canonical_id}@{entry['version']} -> "
        f"{target_owner}/{target_name} at packages/{target_fs_slug}/"
    )


def _check_update(canonical_id: str, current_ver: str, hub_url: str):
    """Check if an update is available. Returns (canonical_id, current, latest) or None."""
    try:
        owner, name = parse_canonical_id(canonical_id)
    except ValueError as e:
        typer.echo(f'{canonical_id}: invalid canonical id ({e})')
        return None
    try:
        rv = requests.get(f"{hub_url}/api/packages/{owner}/{name}/versions", timeout=10)
        rv.raise_for_status()
        items = rv.json().get('items', [])
    except Exception as e:
        typer.echo(f'{canonical_id}: failed to fetch versions ({e})')
        return None
    if not items:
        typer.echo(f'{canonical_id}: no versions available')
        return None
    latest_ver = items[0].get('version', 'unknown')
    if latest_ver == current_ver:
        typer.echo(f'{canonical_id}@{current_ver} is up to date')
        return None
    return (canonical_id, current_ver, latest_ver)


def _install_one(canonical_id: str, version: str = 'latest', timeout: int = 300):
    """Install a single package, returning (canonical_id, True/False, message)."""
    try:
        install(canonical_id, version, timeout=timeout)
        return (canonical_id, True, '')
    except (SystemExit, typer.Exit):
        return (canonical_id, False, 'install failed (exit)')
    except Exception as e:
        return (canonical_id, False, str(e))


@app.command()
def sync(
    package_id: str = typer.Argument(None),
    apply: bool = typer.Option(False, '--apply'),
    check: bool = typer.Option(False, '--check'),
    timeout: int = typer.Option(300, '--timeout', help='Timeout in seconds per package'),
):
    """Sync installed packages with the hub.

    Without arguments: operate on ALL installed packages from the lock.
    With `package_id` (owner/name): operate on that single package.

    Strategy resolution (per-package override > global config > default 'auto'):
      - auto   : update is applied automatically.
      - manual : update is listed only (unless --apply is provided).

    Flags:
      --check  : list available updates with zero FS mutation.
      --apply  : force-install manual-strategy packages.
    """
    if check and apply:
        typer.echo("cannot combine --check and --apply", err=True)
        raise typer.Exit(1)

    l = lock_load()
    pkgs_lock = l.get('packages', {})

    # Resolve target list
    if package_id is not None:
        cid, _slug, _ver = _parse_package_ref(package_id)
        if cid not in pkgs_lock:
            typer.echo(f"Package {cid} not installed", err=True)
            raise typer.Exit(1)
        if pkgs_lock[cid].get('mode') == 'forked':
            typer.echo(f"{cid} is a fork — no upstream sync")
            return
        canonical_ids = [cid]
    else:
        all_ids = list(pkgs_lock)
        canonical_ids = [cid for cid in all_ids if pkgs_lock[cid].get('mode') != 'forked']
        skipped = len(all_ids) - len(canonical_ids)
        if skipped > 0:
            typer.echo(f"Skipping {skipped} fork(s) — use `pkg publish` to publish forks upstream")

    if not canonical_ids:
        typer.echo("No packages installed")
        return

    # Batch check for updates (1 HTTP round-trip for all packages)
    packages_for_batch = {cid: pkgs_lock[cid].get('version', '0.0.0') for cid in canonical_ids}
    c = cfg()
    try:
        batch_result = _check_updates_batch(
            packages_for_batch, c['hub_url'], channel=_configured_channel()
        )
    except RuntimeError as exc:
        typer.echo(f"Cannot reach hub: {exc}", err=True)
        raise typer.Exit(1)

    if not batch_result:
        typer.echo("no updates available (or hub unreachable — best-effort)")
        return

    # Process each package sequentially to keep the lockfile coherent
    n_updated = 0
    n_pending_manual = 0
    n_errors = 0

    try:
        for cid in canonical_ids:
            info = batch_result.get(cid)
            if not info or not _entry_has_update(info):
                continue
            latest = _entry_latest(info)

            try:
                strategy = _resolve_strategy(cid)
            except ValueError as exc:
                typer.echo(f"warning: {exc} — skipping {cid}", err=True)
                n_errors += 1
                continue

            # --check: zero FS mutation, just list
            if check:
                current = info.get('current') or pkgs_lock[cid].get('version')
                typer.echo(
                    f"{cid}: update available {current} -> {latest} (strategy={strategy})"
                )
                continue

            # manual without --apply: list only, no FS mutation
            if strategy == 'manual' and not apply:
                current_ver = pkgs_lock[cid].get('version')
                typer.echo(
                    f"{cid}: update available {current_ver} -> {latest}"
                    " (manual — run pkg sync --apply)"
                )
                n_pending_manual += 1
                continue

            # auto OR (manual + --apply): install sequentially
            _cid, ok, msg = _install_one(cid, latest, timeout)
            if ok:
                typer.echo(f"Updated {cid} -> {latest}")
                n_updated += 1
            else:
                typer.echo(f"Failed to update {cid}: {msg}", err=True)
                n_errors += 1

    except KeyboardInterrupt:
        typer.echo(
            f"Sync interrupted. Summary so far: {n_updated} updated, "
            f"{n_pending_manual} pending manual, {n_errors} errors",
            err=True,
        )
        raise typer.Exit(130)

    typer.echo(
        f"Summary: {n_updated} updated, {n_pending_manual} pending manual, {n_errors} errors"
    )
    # Exit 0 unless EVERYTHING attempted failed (best-effort semantics)
    if (n_updated + n_pending_manual) == 0 and n_errors > 0:
        raise typer.Exit(1)


@app.command('self-update')
def self_update_deprecated(
    package_id: str = typer.Argument(None),
    apply: bool = typer.Option(False, '--apply'),
    check: bool = typer.Option(False, '--check'),
    timeout: int = typer.Option(300, '--timeout', help='Timeout in seconds per package'),
):
    """[DEPRECATED] Renamed to `pkg sync`. Will be removed post-MVP."""
    typer.echo(
        "DEPRECATED: pkg self-update is renamed to pkg sync. Will be removed post-MVP.",
        err=True,
    )
    # Delegate to sync with the same args. typer.Exit raised inside sync()
    # propagates naturally through this call frame to the CLI runner.
    sync(package_id=package_id, apply=apply, check=check, timeout=timeout)


# ---------------------------------------------------------------------------
# config subcommand group (mod-7)
# ---------------------------------------------------------------------------

config_app = typer.Typer(help='Manage pkg client config (global + per-package overrides).')

_VALID_STRATEGIES = ('auto', 'manual')


def _atomic_yaml_write(path: Path, data: dict) -> None:
    """Write *data* as YAML to *path* atomically.

    Uses a temp file in the same directory + os.replace to ensure no
    partial writes are ever observed on disk. If os.replace raises,
    the original file content is preserved untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_name, str(path))
    except Exception:
        # Clean up tmp file on failure; preserve the original untouched.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _mask_token(token: str | None) -> str:
    """Return a masked representation of *token* safe to display in terminals."""
    if not token:
        return '(unset)'
    if len(token) <= 8:
        return token[:2] + '***'
    # Show a short prefix only — no trailing chars, so full token is never reconstructable.
    return token[:6] + '***'


@config_app.command('get')
def config_get():
    """Print the current pkg client config (token masked for safety)."""
    c = cfg()
    printable = {**c}
    if 'token' in printable:
        printable['token'] = _mask_token(printable.get('token'))
    typer.echo(yaml.safe_dump(printable, sort_keys=False).rstrip())


@config_app.command('set')
def config_set(
    arg1: str = typer.Argument(...),
    arg2: str = typer.Argument(None),
    arg3: str = typer.Argument(None),
):
    """Set a config value.

    Forms:
        pkg config set update-strategy <auto|manual>
        pkg config set <canonical_id> update-strategy <auto|manual>
    """
    if arg1 == 'update-strategy':
        # Global: pkg config set update-strategy <value>
        value = arg2
        if value not in _VALID_STRATEGIES:
            typer.echo(
                f"Invalid value {value!r}: must be 'auto' or 'manual'",
                err=True,
            )
            raise typer.Exit(1)
        c = cfg()
        c['update_strategy'] = value
        _atomic_yaml_write(CFG, c)
        typer.echo(f"global update_strategy = {value}")
        return

    # Per-package: pkg config set <canonical_id> update-strategy <value>
    canonical_id = arg1
    key = arg2
    value = arg3
    if key != 'update-strategy':
        typer.echo(
            f"Unknown key {key!r}: only 'update-strategy' is supported",
            err=True,
        )
        raise typer.Exit(1)
    if value not in _VALID_STRATEGIES:
        typer.echo(
            f"Invalid value {value!r}: must be 'auto' or 'manual'",
            err=True,
        )
        raise typer.Exit(1)
    l = lock_load()
    pkgs = l.get('packages', {})
    if canonical_id not in pkgs:
        typer.echo(
            f"package not installed: {canonical_id}",
            err=True,
        )
        raise typer.Exit(1)
    pkgs[canonical_id]['update_strategy'] = value
    l['packages'] = pkgs
    _atomic_yaml_write(LOCK, l)
    typer.echo(f"{canonical_id} update_strategy = {value}")


app.add_typer(config_app, name='config')


@app.command()
def status():
    """Show installed packages, their effective update strategy + origin, and version availability."""
    l = lock_load()
    pkgs = l.get('packages', {})
    if not pkgs:
        typer.echo('No packages installed')
        return
    c = cfg()
    for canonical_id, info_data in pkgs.items():
        ver = info_data.get('version', '?')

        # Forked entries: badge only, skip strategy resolution AND upstream
        # HTTP check entirely (mod-10.fm-4) — there is no upstream to check.
        if info_data.get('mode') == 'forked':
            forked_from = info_data.get('forked_from')
            badge = f" [FORKED from {forked_from}]" if forked_from else " [FORKED]"
            typer.echo(f"{canonical_id}@{ver}{badge}")
            continue

        # Resolve effective update strategy + origin (per-package override or global)
        try:
            strategy = _resolve_strategy(canonical_id)
            # Determine origin: 'override' iff per-package update_strategy was set in lock
            has_override = info_data.get('update_strategy') is not None
            origin = 'override' if has_override else 'global'
        except ValueError as exc:
            typer.echo(f"warning: {exc}", err=True)
            strategy = 'unknown'
            origin = 'unknown'

        line = f"{canonical_id}@{ver} (strategy={strategy}, origin={origin})"

        # Version availability check — best-effort, offline falls back silently
        try:
            owner, name = parse_canonical_id(canonical_id)
            rv = requests.get(
                f"{c['hub_url']}/api/packages/{owner}/{name}/versions", timeout=3
            )
            rv.raise_for_status()
            items = rv.json().get('items', [])
            if items:
                latest = items[0].get('version', '?')
                if latest == ver:
                    line += ' [up to date]'
                else:
                    line += f' [UPDATE AVAILABLE -> {latest}]'
            else:
                line += ' [no versions]'
        except Exception:
            line += ' [offline]'
        typer.echo(line)


# Register the migrate-legacy command (defined in migrate_legacy.py).
app.command('migrate-legacy')(migrate_legacy_cmd)


# Register the ACL subcommand group (defined in acl_commands.py — mod-18).
# Imported lazily with try/except so that if mod-18 hasn't landed yet (parallel
# implementer), `pkg <other-command>` still works. Once acl_commands.py exists,
# `pkg acl list|add|remove` are wired in automatically.
try:
    from .acl_commands import acl_app  # noqa: E402
    app.add_typer(acl_app, name='acl')
except ImportError:
    pass


# Register the Role subcommand group (iter #4 — mod-11 wiring of mod-12).
# Same defensive try/except shape as `acl_app` so an absent module never
# breaks the rest of the CLI.
try:
    from .role_commands import role_app  # noqa: E402
    app.add_typer(role_app, name='role')
except ImportError:
    pass


# Register the Perms subcommand group (iter #4 — mod-11 wiring of mod-13).
try:
    from .perms_commands import perms_app  # noqa: E402
    app.add_typer(perms_app, name='perms')
except ImportError:
    pass


# Register the Deps subcommand group (iter #5 — mod-14).
try:
    from .deps_commands import deps_app  # noqa: E402
    app.add_typer(deps_app, name='deps')
except ImportError:
    pass


# Register the PR subcommand group (iter #07-pr-simple — mod-22 + mod-24 wiring).
try:
    from .pr import pr_app  # noqa: E402
    app.add_typer(pr_app, name='pr')
except ImportError:
    pass


# Register the Notifications subcommand group (iter #07-pr-simple — mod-23 + mod-24 wiring).
try:
    from .notifications import notifications_app  # noqa: E402
    app.add_typer(notifications_app, name='notifications')
except ImportError:
    pass


# Register the Quota subcommand group (iter 09-scan-publish-and-quotas — mod-25 + mod-26 wiring).
try:
    from .quota import quota_app  # noqa: E402
    app.add_typer(quota_app, name='quota')
except ImportError:
    pass


if __name__ == '__main__':
    app()
