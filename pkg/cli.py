from __future__ import annotations
import hashlib, json, os, shutil, sys, tarfile, tempfile
from pathlib import Path
import typer, requests, yaml

app = typer.Typer(help='Package Hub CLI')
CFG = Path.home()/'.pkg/config.yaml'
LOCK = Path('.pkg-lock.yaml')
SUBS = Path('subscriptions.yaml')
PKG_DIR = Path('packages')

def cfg():
    return yaml.safe_load(CFG.read_text()) if CFG.exists() else {'hub_url': os.getenv('PACKAGE_HUB_URL','http://localhost:8000')}

def save_cfg(c):
    CFG.parent.mkdir(parents=True, exist_ok=True); CFG.write_text(yaml.safe_dump(c))

def client_headers():
    c=cfg(); return {'Authorization': f"Bearer {c.get('token')}"} if c.get('token') else {}

def lock_load(): return yaml.safe_load(LOCK.read_text()) if LOCK.exists() else {'packages': {}}
def lock_save(d): LOCK.write_text(yaml.safe_dump(d, sort_keys=False))

def subs_load():
    """Load subscriptions.yaml. Returns {'version': 1, 'channels': []} if missing."""
    return yaml.safe_load(SUBS.read_text()) if SUBS.exists() else {'version': 1, 'channels': []}

def subs_save(d):
    """Write subscriptions.yaml (preserves key order)."""
    SUBS.write_text(yaml.safe_dump(d, sort_keys=False))

SCHEDULER_URL = os.getenv('SCHEDULER_URL', 'http://localhost:8701')
HANDLER_URL = os.getenv('HANDLER_URL', 'http://localhost:8700')

def register_schedules(package_id: str, schedules: list, quiet: bool = False):
    """Enregistre les schedules d'un package dans le scheduler externe."""
    for sched in schedules:
        job_data = {
            'name': f"{package_id}:{sched.get('id', 'unnamed')}",
            'schedule_type': 'cron',
            'cron': sched['cron'],
            'package_id': package_id,
        }
        if 'command' in sched:
            job_data['command'] = sched['command']
        elif 'emit' in sched:
            job_data['event_source'] = sched['emit']['source']
            job_data['event_type'] = sched['emit']['type']
            job_data['event_payload'] = sched['emit'].get('payload', {})
        try:
            resp = requests.post(f'{SCHEDULER_URL}/jobs', json=job_data, timeout=5)
            if resp.ok and not quiet:
                typer.echo(f'  Schedule registered: {job_data["name"]}')
        except requests.ConnectionError:
            if not quiet:
                typer.echo(f'  Warning: scheduler not reachable, schedule {job_data["name"]} not registered')

def unregister_schedules(package_id: str):
    """Supprime tous les schedules d'un package."""
    try:
        resp = requests.delete(f'{SCHEDULER_URL}/jobs', params={'package_id': package_id}, timeout=5)
        if resp.ok:
            count = resp.json().get('deleted', 0)
            if count > 0:
                typer.echo(f'  Removed {count} scheduled job(s)')
    except requests.ConnectionError:
        pass

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

def safe_extract(tar: tarfile.TarFile, dest: Path):
    if sys.version_info >= (3, 12):
        tar.extractall(dest, filter='data')
    else:
        for m in tar.getmembers():
            p = Path(m.name)
            if p.is_absolute() or '..' in p.parts:
                raise ValueError(f'Unsafe path in archive: {m.name}')
            if m.issym() or m.islnk():
                link = Path(m.linkname)
                if link.is_absolute() or '..' in link.parts:
                    raise ValueError(f'Unsafe symlink in archive: {m.name} -> {m.linkname}')
        tar.extractall(dest)

@app.command()
def login(email: str, password: str, hub_url: str = typer.Option('http://localhost:8000')):
    r=requests.post(f'{hub_url}/api/auth/login', json={'email':email,'password':password}); r.raise_for_status()
    c={'hub_url':hub_url,'token':r.json()['token']}; save_cfg(c); typer.echo('Logged in')

@app.command()
def search(query: str='', type: str|None=None, channel: str|None=None):
    c=cfg(); r=requests.get(f"{c['hub_url']}/api/search", params={'q':query,'type':type,'channel':channel}); r.raise_for_status()
    for p in r.json().get('items',[]): typer.echo(f"{p['package_id']}@{p.get('latest_version')} [{p['type']}] - {p['description']}")

@app.command()
def info(package_id: str):
    c=cfg(); r=requests.get(f"{c['hub_url']}/api/packages/{package_id}"); r.raise_for_status(); typer.echo(json.dumps(r.json(), indent=2, default=str))

@app.command()
def publish(path: Path, channel: str|None=typer.Option(None), changelog: str=''):
    c=cfg(); tmp=Path(tempfile.mkdtemp())/'pkg.tar.gz'
    with tarfile.open(tmp, 'w:gz') as tar: tar.add(path, arcname='.')
    with tmp.open('rb') as f:
        r=requests.post(f"{c['hub_url']}/api/packages", headers=client_headers(), files={'file':('package.tar.gz',f,'application/gzip')}, data={'channel':channel or '', 'changelog':changelog})
    r.raise_for_status(); typer.echo(json.dumps(r.json(), indent=2, default=str))

@app.command()
def install(package_id: str, version: str='latest'):
    c=cfg(); slug=package_id.split('@')[0]; ver=package_id.split('@')[1] if '@' in package_id else version
    r=requests.get(f"{c['hub_url']}/api/packages/{slug}/versions/{ver}/download"); r.raise_for_status()
    expected_hash = r.headers.get('X-Archive-Hash')
    if expected_hash and not verify_hash(r.content, expected_hash):
        typer.echo('Hash mismatch — archive may be corrupted or tampered', err=True); raise typer.Exit(1)
    # Pre-extract: read meta.yaml from archive and check rule conflicts
    tmp=Path(tempfile.mkdtemp())/'pkg.tar.gz'; tmp.write_bytes(r.content)
    pre_meta = {}
    with tarfile.open(tmp,'r:gz') as tar:
        for member in tar.getmembers():
            if member.name.endswith('meta.yaml') and '/' not in member.name.replace('./',''):
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
    dest=PKG_DIR/slug
    if dest.exists(): shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(tmp,'r:gz') as tar: safe_extract(tar, dest)
    meta_path=next(dest.rglob('meta.yaml')); meta=yaml.safe_load(meta_path.read_text()) or {}
    l=lock_load(); l.setdefault('packages',{})[slug]={'version':meta.get('version','unknown'),'archive_hash':expected_hash or '','mode':'sync'}; lock_save(l)
    setup=dest/'setup.sh'
    if setup.exists():
        rc = os.system(f'cd {dest} && bash setup.sh')
        code = os.waitstatus_to_exitcode(rc) if hasattr(os, 'waitstatus_to_exitcode') else (rc >> 8)
        if code != 0:
            typer.echo(f"setup.sh for '{slug}' failed (exit {code}). Package extracted but not fully configured.", err=True)
            raise typer.Exit(1)
    # Post-setup: register schedules if defined
    schedules = meta.get('schedules', [])
    if schedules:
        register_schedules(slug, schedules)
    # Reload handler rules
    reload_handler_rules()
    typer.echo(f'Installed {slug}@{l["packages"][slug]["version"]}')

@app.command('list')
def list_installed():
    for k,v in lock_load().get('packages',{}).items(): typer.echo(f"{k}@{v.get('version')} ({v.get('mode')})")

@app.command()
def remove(package_id: str):
    slug=package_id.split('@')[0]
    unregister_schedules(slug)
    shutil.rmtree(PKG_DIR/slug, ignore_errors=True)
    l=lock_load(); l.get('packages',{}).pop(slug,None); lock_save(l)
    reload_handler_rules()
    typer.echo(f'Removed {slug}')

@app.command('self-update')
def self_update(package_id: str = typer.Argument(None), all: bool = typer.Option(False, '--all')):
    l=lock_load(); pkgs_lock=l.get('packages',{})
    if not all and not package_id:
        typer.echo('Usage: pkg self-update <package_id> or pkg self-update --all'); return
    slugs = list(pkgs_lock) if all else [package_id]
    c=cfg(); updated=0
    for slug in slugs:
        if slug not in pkgs_lock:
            typer.echo(f'{slug} is not installed — skipping'); continue
        current_ver = pkgs_lock[slug].get('version','0.0.0')
        try:
            rv=requests.get(f"{c['hub_url']}/api/packages/{slug}/versions"); rv.raise_for_status()
            items=rv.json().get('items',[])
        except Exception as e:
            typer.echo(f'{slug}: failed to fetch versions ({e})'); continue
        if not items:
            typer.echo(f'{slug}: no versions available'); continue
        latest_ver = items[0].get('version','unknown')
        if latest_ver == current_ver:
            typer.echo(f'{slug}@{current_ver} is up to date'); continue
        install(slug, latest_ver)
        typer.echo(f'Updated {slug} from {current_ver} to {latest_ver}'); updated+=1
    if all: typer.echo(f'{updated} package(s) updated')
    # Auto-install new packages from subscribed channels
    if all:
        subs = subs_load()
        new_installed = 0
        for sub in subs.get('channels', []):
            if not sub.get('auto', False):
                continue
            channel_slug = sub['id']
            try:
                cr = requests.get(f"{c['hub_url']}/api/channels/{channel_slug}")
                cr.raise_for_status()
                ch_data = cr.json()
            except Exception as e:
                typer.echo(f'Channel {channel_slug}: failed to fetch ({e})'); continue
            current_lock = lock_load().get('packages', {})
            for pkg in ch_data.get('packages', ch_data.get('items', [])):
                pid = pkg.get('package_id', pkg.get('id', ''))
                if not pid or pid in current_lock:
                    continue
                typer.echo(f'New from channel {channel_slug}: installing {pid}...')
                try:
                    install(pid, 'latest')
                    new_installed += 1
                except Exception as e:
                    typer.echo(f'  Failed to install {pid}: {e}')
        if new_installed:
            typer.echo(f'{new_installed} new package(s) installed from subscribed channels')

@app.command()
def channels(slug: str = typer.Argument(None)):
    c=cfg()
    if slug:
        r=requests.get(f"{c['hub_url']}/api/channels/{slug}"); r.raise_for_status()
        data=r.json(); typer.echo(f"Channel: {data.get('name',slug)} — {data.get('description','')}")
        for p in data.get('packages',data.get('items',[])):
            pid=p.get('package_id',p.get('id','')); typer.echo(f"  {pid}@{p.get('latest_version','')} — {p.get('description','')}")
    else:
        r=requests.get(f"{c['hub_url']}/api/channels"); r.raise_for_status()
        for ch in r.json().get('items',[]):
            typer.echo(f"{ch.get('slug',ch.get('name',''))} — {ch.get('description','')}")

@app.command()
def subscribe(channel: str, auto: bool = typer.Option(True, '--auto/--no-auto',
              help='Auto-install new packages from this channel on self-update --all')):
    """Subscribe the workspace to a channel.

    Adds the channel to subscriptions.yaml so that `pkg self-update --all`
    can auto-install new packages from subscribed channels.

    subscriptions.yaml format::

        version: 1
        channels:
          - id: official
            auto: true    # auto-install new packages on self-update --all
          - id: community
            auto: false   # listed only, manual install
    """
    c = cfg()
    r = requests.get(f"{c['hub_url']}/api/channels/{channel}")
    if r.status_code == 404:
        typer.echo(f"Channel '{channel}' not found on the Hub", err=True)
        raise typer.Exit(1)
    r.raise_for_status()
    s = subs_load()
    existing = next((ch for ch in s['channels'] if ch['id'] == channel), None)
    if existing:
        existing['auto'] = auto
        typer.echo(f"Updated subscription '{channel}' (auto={'yes' if auto else 'no'})")
    else:
        s['channels'].append({'id': channel, 'auto': auto})
        typer.echo(f"Subscribed to '{channel}' (auto={'yes' if auto else 'no'})")
    subs_save(s)

@app.command()
def unsubscribe(channel: str):
    """Remove a channel subscription from this workspace."""
    s = subs_load()
    before = len(s['channels'])
    s['channels'] = [ch for ch in s['channels'] if ch['id'] != channel]
    if len(s['channels']) == before:
        typer.echo(f"Not subscribed to '{channel}'", err=True)
        raise typer.Exit(1)
    subs_save(s)
    typer.echo(f"Unsubscribed from '{channel}'")

@app.command('subscriptions')
def subscriptions_cmd():
    """List channel subscriptions for this workspace.

    Reads subscriptions.yaml and displays each subscribed channel
    with its auto-install mode (auto / manual).
    """
    s = subs_load()
    channels_list = s.get('channels', [])
    if not channels_list:
        typer.echo('No subscriptions')
        return
    for ch in channels_list:
        mode = 'auto' if ch.get('auto', False) else 'manual'
        typer.echo(f"{ch['id']} ({mode})")

@app.command()
def status():
    l=lock_load(); pkgs=l.get('packages',{})
    if not pkgs: typer.echo('No packages installed'); return
    c=cfg()
    for slug,info_data in pkgs.items():
        ver=info_data.get('version','?'); mode=info_data.get('mode','sync'); line=f'{slug}@{ver} ({mode})'
        try:
            rv=requests.get(f"{c['hub_url']}/api/packages/{slug}/versions", timeout=3); rv.raise_for_status()
            items=rv.json().get('items',[])
            if items:
                latest=items[0].get('version','?')
                line += f' — latest: {latest}' + (' [UPDATE AVAILABLE]' if latest != ver else ' [up to date]')
        except Exception:
            line += ' — (offline)'
        typer.echo(line)

if __name__ == '__main__': app()
