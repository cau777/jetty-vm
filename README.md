## Motivation

The current software development ecosystem just feels dangerous.

- Your agent is running bash commands in a long unsupervised session with access to all your personal files
- Each project you start immediately downloads hundreds of third-party dependencies, which execute code on your machine
- Agents are still not immune to prompt injection or to simply making a mistake (deleting your production database with that key you forgot in .env)
- Your AI credentials are very valuable targets for attacks

Also, the cost of starting a project decreased massively, but those often "pollute" you machine with random files and host config.

## How Jetty solves that

Jetty reduces the cost of creating a truly isolated environment. You can create a LXD VM for each of your project with a few clicks, then, hand off to an agent to configure it.

- The VM is isolated from your host and from your other VMs -&gt; You can let your agents run wild inside them
- The VM has access to the internet, but can't connect to other VMs or your host without an explicit SSH tunnel -&gt; You control what is exposed
- The HTTP(s) traffic is proxied -&gt; The proxy allows logging, blocking and intercepting requests
- The proxy can inject credentials to specific endpoints -&gt; Your VM does not need your GitHub or OpenAI tokens
- The VM has SSH configured -&gt; Use Orca, VSCode or JetBrains IDEs just like another SSH target

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

Download the x86_64 AppImage and run setup as your desktop user:

```bash
curl -fL https://github.com/cau777/jetty-vm/releases/latest/download/jetty-x86_64.AppImage \
  -o "$HOME/Downloads/jetty-x86_64.AppImage"
chmod +x "$HOME/Downloads/jetty-x86_64.AppImage"
"$HOME/Downloads/jetty-x86_64.AppImage" setup
```

Setup installs `~/.local/bin/jetty`, starts the per-user daemon, registers the
tray app for desktop login, and installs the agent skill when `npx` is
available. It uses `pkexec` to install and initialize snap LXD and add your
account to the `lxd` group. LXD group access is equivalent to root access on
the host. Sign out and back in after setup; reboot if the daemon still cannot
access LXD afterward.

Use the tray menu to create the gateway, then manage agent VMs in Jetty. The
same operations are available from a terminal with `jetty status`,
`jetty gateway setup`, and `jetty vm create`. `jetty update` installs the
latest release after verifying its published SHA-256 checksum.

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

- Ubuntu 22.04 or newer on x86_64, with a desktop session and `snapd`.
- A Polkit authentication agent for the one-time `pkexec` prompts.
- `npx` to install the skill automatically. Without it, install
`orca-ssh-setup/` into your coding agent using that agent's skill manager.
- Codex or Claude Code on the host if you want to use those harnesses.

The proxy daemon and tray run as your user. LXD owns the bridges, and routing
policy lives inside the gateway VM; Jetty does not install a host firewall
rule or privileged helper.

## Project layout

- [`orca-ssh-setup/`](orca-ssh-setup/) — the installable agent skill and
end-to-end provisioning workflow.
- [`orca-proxy/`](orca-proxy/) — daemon, tray, management UI, LXD REST client,
CLI, transparent proxy, and credential execution.
- [`CONTEXT.md`](CONTEXT.md) — the project's domain vocabulary.

For development and the source installer, see
[`orca-proxy/README.md`](orca-proxy/README.md).

## Security model

Jetty is designed for a trusted host operator and an untrusted coding agent
with broad authority inside its own VM. It reduces credential exposure; it is
not a general-purpose network sandbox or a substitute for reviewing the
permissions you grant in each rule. Keep rules specific to the VM, hostname,
path, and operation the agent needs.

The desktop manager is a native PySide6 Qt Quick application. It keeps its
loopback API in the separate daemon process, so a desktop UI failure does not
stop VM web egress.
