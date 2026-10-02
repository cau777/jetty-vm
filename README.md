# Jetty

**Permission-scoped LXD VMs for coding agents.** Jetty gives an agent a
real, sudo-capable Linux machine to work in without giving that machine a copy
of your host credentials.

It is intended for agent workflows that need more isolation than a local
checkout, while still needing selected access to services such as GitHub,
Codex, or Claude Code.

## What Jetty does

Jetty combines two pieces:

- `orca-ssh-setup` is an agent skill that provisions a project VM, installs
  the requested coding-agent tools, and connects the VM to an SSH-based agent
  workflow.
- `orca-proxy` is a host-side service. It receives a registered VM's web
  traffic through a gateway VM, applies a policy layer, and injects a
  host-held credential only for explicit VM, hostname, and path rules.

The result is a clear boundary: the coding agent can administer its VM, but it
cannot read, copy, or reuse credentials held on the host. Network policy is
enforced by the network topology itself, rather than by environment variables
the agent can remove or by rules in the host's firewall: agent VMs sit on a
private LXD network whose only way out is a Jetty gateway VM.

## How it works

```text
your agent → dedicated LXD VM → Jetty gateway VM ─(WireGuard)→ host orca-proxy → approved service
                                                                  └─ host-held credential, only when a rule matches
```

The host service has a loopback-only management API for registering VMs,
credentials, and rules. Registered VMs do not receive management authority.
The gateway sends their HTTP(S) traffic (TCP 80/443) to the proxy through a
WireGuard tunnel that ends in the unprivileged proxy process itself; unmatched
traffic is passed through without credentials. If the proxy, the tunnel, or
the gateway is down, agent VMs have no web egress at all.

## Use it

Install the latest stable release, as your own user (no sudo). One command
installs the agent skill and the host-side proxy service:

```bash
curl -fsSL https://github.com/cau777/jetty-vm/releases/latest/download/jetty-install.sh | bash
```

For a reproducible installation, substitute an exact release tag:

```bash
curl -fsSL https://github.com/cau777/jetty-vm/releases/download/v1.0.2/jetty-install.sh | bash
```

The installer verifies the release and keeps it under
`~/.local/share/jetty/releases/<version>/`, points
`~/.local/share/jetty/current` at it, starts the `orca-proxy` user service
from there, and uses `npx skills` to install `orca-ssh-setup` into your
detected agents. Running a newer installer upgrades in place; proxy state
(rules, credentials, logs, CA) lives in `~/.orca-proxy` and is kept. To go
back, run `bash ~/.local/share/jetty/current/install.sh --rollback <version>`.

If an older, Multipass-based orca-proxy is already running and you want to
keep it, install beside it with `bash -s -- --instance orca-proxy-lxd --port
18080`.

Then ask your preferred coding agent to set up a Jetty VM for the current
project. For example:

```text
Use the orca-ssh-setup skill to provision a permission-scoped VM for this project.
I need Codex, GitHub read/write access to this repository, and the default VM size.
```

The skill first confirms the VM's name, resources, base image, required
harnesses, GitHub permissions, and any other network or secret requirements.
It then walks through the provisioned VM and the least-privilege host policy.
Interactive provider sign-ins remain on the host and are never performed by
the agent in the VM.

## Prerequisites

- Linux host with [LXD](https://canonical.com/lxd) installed and initialized
  (`snap install lxd && lxd init --auto`), and your user in the `lxd` group.
- `git`, `curl`, `jq`, `uv`, Node.js (`npx`), and either Codex or Claude Code
  on the host.

The proxy is shared by all Jetty VMs on one host and runs as your own user.
Nothing in Jetty needs sudo, a setuid/setcap helper, or changes to the host
firewall: LXD's daemon owns the bridges, and all routing policy lives inside
the gateway VM.

## Project layout

- [`orca-ssh-setup/`](orca-ssh-setup/) — the installable agent skill and
  end-to-end provisioning workflow.
- [`orca-proxy/`](orca-proxy/) — policy service, management UI, transparent
  proxy, credential execution, and the `jetty-lxd` VM lifecycle tool.
- [`CONTEXT.md`](CONTEXT.md) — the project's domain vocabulary.

For service development and manual installation, see
[`orca-proxy/README.md`](orca-proxy/README.md).

## Security model

Jetty is designed for a trusted host operator and an untrusted coding agent
with broad authority inside its own VM. It reduces credential exposure; it is
not a general-purpose network sandbox or a substitute for reviewing the
permissions you grant in each rule. Keep rules specific to the VM, hostname,
path, and operation the agent needs.
