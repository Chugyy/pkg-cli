# pkg — Package Hub CLI

Client CLI for the [Package Hub](https://hub-api.multimodal-house.fr). Installs,
publishes, and updates AI agent packages (tools, MCP servers, agents, roadmaps,
services) into a local workspace.

## Install

```bash
# With uv (recommended — isolated)
uv tool install git+https://github.com/Chugyy/pkg-cli.git

# With pipx
pipx install git+https://github.com/Chugyy/pkg-cli.git

# With pip
pip install --user git+https://github.com/Chugyy/pkg-cli.git
```

## Configure

The CLI reads its Hub URL from `~/.pkg/config.yaml`:

```yaml
hub_url: https://hub-api.multimodal-house.fr
```

You can also override it per-invocation with `PACKAGE_HUB_URL`.

## Usage

```bash
pkg search <query>                 # search the Hub
pkg install <package-id>           # download + extract + run setup.sh
pkg list                           # installed packages (reads .pkg-lock.yaml)
pkg status                         # installed vs latest on the Hub
pkg self-update --all              # update everything + auto-channels
pkg remove <package-id>            # uninstall

pkg channels                       # list channels
pkg subscribe <channel> --auto     # subscribe (auto-install on self-update)
pkg unsubscribe <channel>
pkg subscriptions                  # list subscriptions

pkg login <email> <password>       # auth (required to publish)
pkg publish <path> --channel <ch>  # publish a package
```

## Workspace layout

`pkg` operates on the current directory as a workspace:

```
<workspace>/
├── packages/             # installed packages (one dir each)
├── .pkg-lock.yaml        # installed versions + archive hashes
└── subscriptions.yaml    # subscribed channels
```

## Security

Every download is verified against the `X-Archive-Hash` header (SHA-256) before
extraction. Archives are extracted with path-traversal and symlink protection.

### Profiles (`.profiles/`) never leave the machine

A profile (`.profiles/`, a tool's local credentials) is never sent to the Hub. A
value that must ship with a package belongs in its code or declaration
(`meta.yaml` and sidecar), not in a profile.

- **Excluded on upload**: `pkg publish`, `pkg pr create` and `pkg fork` drop every
  `.profiles` path (file or directory, at any depth, any case), with no
  `.gitignore` or `.pkg-ignore` needed. A link pointing into `.profiles/` is dropped
  on fork too.
- **Preserved on extraction**: `pkg install` and `pkg sync` (like `pkg self-update`)
  never create, modify or traverse `.profiles/` on the recipient's side, on first
  install as on update. Links (symbolic or hard) into `.profiles/` are dropped.
- **Refused by the Hub**: if an archive still contains `.profiles/`, the Hub answers
  403 (`scan_failed`). The CLI prints the rule, then one path per line, never a value.

Known limits: an older CLI (without this filter) is not protected on extraction —
update `pkg`; `pkg remove` deletes the package directory, `.profiles/` included; a
`setup.sh` must never write under `.profiles/` (it runs with the recipient's rights,
the CLI does not control it).

## License

MIT
