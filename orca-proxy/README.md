# orca-proxy

The host-side policy proxy for Jetty VMs: Management API, Web UI, rule engine,
Credential execution, request logging and the mitmproxy addon, all in one
mitmdump process running as your own user. See `design.md` for the full spec.

Agent VMs run on LXD and reach it only through the Jetty gateway VM, which
sends their TCP 80/443 into mitmdump's userspace WireGuard listener. Nothing
here needs root, a capability-bearing helper, or host firewall rules.

## Install (as a host service)

```bash
bash deploy/install.sh              # from a checkout, as your own user
deploy/jetty-lxd setup              # LXD project, networks and gateway VM
deploy/jetty-lxd launch my-vm       # an agent VM, registered and trusting the CA
```

`install.sh` installs a versioned copy under `~/.local/share/orca-proxy/` and a
systemd user unit. The unit's tunnel listener binds the host's address on the
Jetty uplink network (`10.201.0.1`), so it keeps retrying until
`jetty-lxd setup` has created that network. `jetty-lxd` needs your user in the
`lxd` group; run it without arguments for its commands.

Upgrading is the same `install.sh` command, run again.

## Development

```bash
uv sync
uv run pytest -v
uv run python -m orca_proxy   # dev server on loopback, data dir defaults to ~/.orca-proxy
```

Set `ORCA_PROXY_HOME` to override the data directory (used by tests to isolate
each run in a temp directory).

`tests/e2e/lxd-gateway-checks.sh` checks the whole topology against real VMs
(identity, policy, spoofing, isolation, fail-closed); see its header for how to
run it against a test proxy.
