# Jetty

**Permission-scoped Multipass VMs for coding agents.** Jetty gives an agent a
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
- `orca-proxy` is a host-side service. It transparently directs a registered
  VM's web traffic through a policy layer and injects a host-held credential
  only for explicit VM, hostname, and path rules.

The result is a clear boundary: the coding agent can administer its VM, but it
cannot read, copy, or reuse credentials held on the host. Network policy is
enforced at the Multipass bridge, rather than relying on environment variables
the agent can remove.

## How it works

```text
your agent → dedicated Multipass VM → Jetty host policy → approved service
                                      └─ host-held credential, only when a rule matches
```

The host service has a loopback-only management API for registering VMs,
credentials, and rules. Registered VMs do not receive management authority.
Their HTTP(S) traffic is transparently redirected to the proxy; unmatched
traffic is passed through without credentials.

## Use it

Install the latest stable release, including its matching agent skill:

```bash
curl -fsSL https://github.com/cau777/jetty-vm/releases/latest/download/jetty-install.sh | bash
```

For a reproducible installation, substitute an exact release tag:

```bash
curl -fsSL https://github.com/cau777/jetty-vm/releases/download/v1.0.1/jetty-install.sh | bash
```

The bootstrap verifies and keeps the matching Jetty source under
`~/.local/share/jetty/releases/<version>/`, then uses `npx skills` to install
`orca-ssh-setup` into your detected agents. To install the matching proxy in
the same user-initiated command, add `--with-proxy`; it will request your sudo
password:

```bash
curl -fsSL https://github.com/cau777/jetty-vm/releases/download/v1.0.1/jetty-install.sh | bash -s -- --with-proxy
```

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

- Linux host with [Multipass](https://multipass.run) installed.
- `git`, `curl`, and either Codex or Claude Code on the host.
- A user able to run the one-time `orca-proxy` installation with `sudo` when
  the VM needs host-held credentials or enforced HTTP(S) policy.

The proxy is shared by all Jetty VMs on one host. Its privileged firewall
helper is installed as a root-owned file; the proxy service itself runs as a
regular user.

## Project layout

- [`orca-ssh-setup/`](orca-ssh-setup/) — the installable agent skill and
  end-to-end provisioning workflow.
- [`orca-proxy/`](orca-proxy/) — policy service, management UI, transparent
  proxy, credential execution, and firewall integration.
- [`CONTEXT.md`](CONTEXT.md) — the project's domain vocabulary.

For service development and manual installation, see
[`orca-proxy/README.md`](orca-proxy/README.md).

## Security model

Jetty is designed for a trusted host operator and an untrusted coding agent
with broad authority inside its own VM. It reduces credential exposure; it is
not a general-purpose network sandbox or a substitute for reviewing the
permissions you grant in each rule. Keep rules specific to the VM, hostname,
path, and operation the agent needs.
