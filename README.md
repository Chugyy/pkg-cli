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

## License

MIT
