# orca-proxy

Core service skeleton for the unified VM credential-injection & logging proxy
(see `cau777/jetty-vm` issue #1 for the full design spec). This slice covers the
entity model, SQLite persistence, the Management API CRUD surface, and
Interception CA generation — not the mitmproxy addon, firewall enforcement,
Credential execution engine, request logging, or Web UI, which are later
slices.

## Install (as a host service)

```bash
curl -fsSL https://raw.githubusercontent.com/cau777/jetty-vm/main/orca-proxy/deploy/install.sh | sudo bash
```

Or, from an existing checkout: `sudo bash deploy/install.sh`. Either way it
must run as root — see `deploy/install.sh`'s header comment and design.md's
"Service installation and firewall-rule lifecycle" section for why (the
short version: it compiles the firewall-sync helper and grants it
`CAP_NET_ADMIN`/`CAP_NET_RAW` via `setcap`, which is only safe if nothing in
the resulting binary's own path is writable by the unprivileged user that
invokes it, which only root can arrange). Requires a C compiler (`cc` or
`gcc`) and `setcap` (Debian/Ubuntu: `libcap2-bin`) on the host. The service
itself still runs as your own user (`systemctl --user status
orca-proxy.service`), not root, and there is no sudoers entry anywhere in
this design.

Upgrading is the same command, run again. It's a deliberate action, not
something a background process does for you.

## Development

```bash
uv sync
uv run pytest -v
uv run python -m orca_proxy   # dev server on loopback, data dir defaults to ~/.orca-proxy
```

Set `ORCA_PROXY_HOME` to override the data directory (used by tests to isolate
each run in a temp directory).
