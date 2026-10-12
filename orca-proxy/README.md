# orca-proxy

The host-side policy proxy for Jetty VMs: Management API, native Qt Quick UI,
rule engine, Credential execution, request logging, and the mitmproxy addon.

## Desktop runtime

The release AppImage contains the `jetty` CLI, daemon, tray, and native
PySide6 Qt Quick (QML) window. `jetty daemon` serves the loopback API as soon
as the user service starts, then starts and supervises mitmproxy when the Jetty
uplink address exists. `jetty tray` polls the daemon and renders the management
UI with QML. The tray and daemon are separate processes so a desktop failure
does not stop VM web egress. All UI HTTP requests run in worker threads.

VM creation and gateway setup run as daemon jobs. The API returns a job ID and
reports real provisioning stages from `GET /api/v1/jetty/jobs/{job_id}`. The
CLI follows the same job until completion, so its commands remain blocking.

LXD operations use its local Unix-socket REST API. Jetty manages the gateway,
agent VMs, and the generated `~/.ssh/jetty_config` fragment. Local membership
in the `lxd` group is root-equivalent on the host.

Port Forwards expose a VM port on the host's `127.0.0.1`, for example a dev
server. The daemon supervises an `ssh -N -L` per forward through the gateway's
per-VM SSH port, so services bound to the VM's localhost are reachable too. It
uses its own key in `~/.orca-proxy/port-forwards/`, authorized on the VM and
checked against the VM's host key over the LXD socket. One-time forwards last
until closed or the daemon stops; persistent ones are stored and reopen when
the daemon starts. Deleting a VM removes its forwards.

Reverse Forwards go the other way: they expose a port on the host's localhost
on the VM's `127.0.0.1`, for example a database or a local model server. The
daemon supervises an `ssh -N -R` over the same connection, so the gateway's
firewall still drops VM traffic to host and LAN addresses. VM ports start at
1024 because the VM side binds as `ubuntu`. Several VMs may reach the same host
port. The agent gets whatever access that service grants, so expose only
services you would hand it.

## CLI

```bash
jetty status
jetty gateway setup
jetty vm create my-agent --cpus 4 --memory 8GiB --disk 40GiB
jetty vm exec my-agent -- uname -a
jetty vm upload my-agent /home/ubuntu/setup.sh --file ./setup.sh
jetty vm port open my-agent 5173                 # one-time, localhost:5173
jetty vm port open my-agent 8000 --host-port 18000 --persistent
jetty vm port list
jetty vm port close my-agent 5173
jetty vm reverse-port open my-agent 5432         # VM localhost:5432 -> host localhost:5432
jetty vm reverse-port open my-agent 11434 --vm-port 21434 --persistent
jetty vm reverse-port list
jetty vm reverse-port close my-agent 5432        # by VM port
```

Run `jetty --help` and `jetty vm --help` for the other lifecycle operations.

## Development

```bash
uv sync
uv run python -m orca_proxy   # Management API on loopback
```

Set `ORCA_PROXY_HOME` to override the data directory (used by tests to isolate
each run in a temporary directory). The desktop release is built with
`release/jetty.spec` and `.github/workflows/release.yml`.

`tests/e2e/lxd-gateway-checks.sh` checks the full topology against real VMs
(identity, policy, spoofing, isolation, and fail-closed behavior); see its
header for how to run it against a test proxy.
