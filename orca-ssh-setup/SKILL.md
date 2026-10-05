---
name: orca-ssh-setup
description: Set up a Jetty-managed LXD VM as an SSH run target for the current project, install its toolchain and requested coding-agent harnesses, and connect it to Orca as an SSH project.
---

# Orca SSH Project Setup

Use this procedure when the user wants to run their current repo's Orca agents
on a dedicated local VM instead of their laptop, connected via SSH (Orca's
"SSH target" run mode — see https://www.onorca.dev/docs/ways-to-run, mode #2).

Scope note: this is **one long-lived VM per project**, reused across
worktrees/branches.

The end state: a running LXD VM, reachable over SSH with key-based auth
(through the Jetty gateway VM), with the project's toolchain **and** the
requested coding-agent harness(es) installed, its outbound traffic
transparently enforced by the host-side **orca-proxy** service (no explicit
proxy configuration inside the VM), and
the user knows exactly what to click/type in Orca to register it and start a
worktree on it.

Work through the steps below in order. Do not skip step 2 — guessing at VM
sizing, base image, harness choice, or naming instead of confirming with the
user is the most common way this goes wrong.

## 1. Inspect the current repo to determine required technologies

From the repo root, gather enough signal to know what the VM needs installed.
Look for (not exhaustive — adapt to what you find):

- **Language/runtime version pins**: `.python-version`, `.nvmrc`,
  `.node-version`, `.ruby-version`, `.tool-versions` (asdf), `go.mod`
  (`go` directive), `rust-toolchain.toml`, `.java-version`.
- **Package managers / lockfiles**: `package-lock.json` / `pnpm-lock.yaml` /
  `yarn.lock` (npm/pnpm/yarn), `requirements.txt` / `pyproject.toml` /
  `poetry.lock` / `uv.lock` (pip/poetry/uv), `Gemfile.lock` (bundler),
  `Cargo.lock` (cargo), `go.sum` (go modules).
- **Containers/services the project depends on**: `docker-compose.yml`,
  `Dockerfile`, references to Postgres/Redis/etc. in env files or config.
- **Build/CI config** for the canonical install & test commands:
  `.github/workflows/*.yml`, `Makefile`, `justfile`, `package.json` scripts.
- **System-level deps**: anything imported/required that needs native
  libraries (e.g. `psycopg2`, `sharp`, `pillow`, CUDA-dependent packages).
- **Repo size / disk needs**: rough size of the working tree plus any large
  data/model directories the agent will need locally.

Summarize findings in a short list (language(s), versions, package manager,
services, anything unusual) before moving to step 2.

## 2. Clarify open questions with the user

Do not assume. Confirm at minimum:

- **Project/VM name** — propose one derived from the repo directory name
  (kebab-case, e.g. `orca-<reponame>`), but let the user override it.
- **VM resources** — default proposal: 4 CPUs, 8GB RAM, 40GB disk. Ask if the
  workload (large builds, ML training, big monorepo) needs more.
- **Base Ubuntu image** — default to the latest LTS (`ubuntu:24.04`) unless
  the repo needs a specific OS version.
- **Which coding-agent harness(es) to install** — Codex CLI (`@openai/codex`),
  Claude Code (`@anthropic-ai/claude-code`), both, or neither. This decides
  what step 4 installs and whether steps 3/6 (orca-proxy) run at all. Default
  to asking rather than assuming both, since step 3 needs an explicit
  one-time interactive login per harness the user actually wants
  credentialed.
- **Whether the VM needs GitHub access** (cloning, pushing, opening
  PRs/issues via `git`/`gh`) — and if so, **which repo(s)/org(s)** and
  **which operations** (read-only clone/PR/issue-read, or also push and
  PR/issue-create). This decides whether step 3's GitHub Credential is
  registered and what Rules step 6 creates. Never `gh auth login` inside the
  VM or copy a GitHub token into it — see step 3 for why and what to do
  instead.
- **Credentials/secrets** the agent will need inside the VM (API keys, cloud
  creds, private registry auth, `.env` values) — ask how the user wants these
  provisioned (manually after setup, via a secrets file they'll copy in, etc).
  Do not ask the user to paste secrets into chat; ask *how* they want to
  deliver them.
- **Networking specifics** — anything the VM needs to reach (VPN, internal
  services). Jetty VMs **cannot** reach private addresses (RFC1918, the
  host, the LAN, Tailscale/CGNAT), other Jetty VMs, or IPv6 at all; if the
  project needs any of that, stop and tell the user rather than working
  around it.

Only proceed to provisioning once these are settled.

## 3. Set up the host-side orca-proxy service and register Credentials

Always do 3a and 3b: every Jetty VM depends on orca-proxy and the gateway.
Skip 3c and 3d if step 2 established the VM needs no coding-agent harness and
no GitHub access.

Coding-agent and GitHub credentials must **not** be baked into the VM — a
live provider session sitting on a machine that an agent runs with full sudo
on is a bad combination, and it means every rebuild/recreate of the VM costs
another interactive login. **orca-proxy** is the host-side app that replaces
the previous CLIProxyAPI broker and mitmproxy gh-proxy combination with one
unified service. Jetty VMs sit on a private LXD network whose only exit is
the Jetty gateway VM, which sends all their outbound 80/443 into orca-proxy
(agent-proof — a VM's root user cannot bypass it), and orca-proxy injects a
live Credential Value only into requests matching an explicit Rule. It runs once per *machine* (share it across every project
VM, not per project).

**3a. Connect to the installed Jetty app.** Jetty installs the `jetty`
command and starts a per-user daemon. Set its loopback Management API URL:

```bash
JETTY="${JETTY_CLI:-$HOME/.local/bin/jetty}"
API="http://127.0.0.1:8080/api/v1"
JETTY_TOOLS_DIR="$HOME/.local/share/jetty"
curl -fsS "${API%/api/v1}/readyz"
```

Use these variables in each shell that runs the remaining commands.

The Management API is available before gateway setup. `/readyz` remains
unready until Jetty's network exists and the proxy listener is running.

**3b. If the Jetty daemon is missing**, stop and hand the user these
installation commands. They run as the desktop user:

```bash
curl -fL https://github.com/cau777/jetty-vm/releases/latest/download/jetty-x86_64.AppImage \
  -o "$HOME/Downloads/jetty-x86_64.AppImage"
chmod +x "$HOME/Downloads/jetty-x86_64.AppImage"
"$HOME/Downloads/jetty-x86_64.AppImage" setup
```

The setup command installs the AppImage under `~/.local/bin/jetty`, starts
the daemon, installs LXD if needed, and adds the user to the `lxd` group. If
it reports that a new login is needed, have the user sign out and back in,
then resume this skill. If Jetty still cannot access LXD, reboot so the
lingering user manager receives the new group membership.

**3c. Ensure the needed Credentials exist.** Credential creation is a plain
`PUT` the Provisioning Agent issues directly — it is not gated behind the
Web UI's Quick Add button, which is a human convenience layered on the same
Management API, not a separate mechanism. Read the source-of-truth catalog
(the same file Quick Add and the compatibility tests both load) rather than
hardcoding command strings here:

```bash
CATALOG="$JETTY_TOOLS_DIR/quick-add-catalog.json"

put_credential() {
  local key="$1" entry command ttl
  entry=$(jq -c --arg k "$key" '.[] | select(.key == $k)' "$CATALOG")
  command=$(echo "$entry" | jq -r .command)
  ttl=$(echo "$entry" | jq -r .ttl_seconds)
  curl -fsS -X PUT "$API/credentials/${key}" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg cmd "$command" --argjson ttl "$ttl" '{command: $cmd, ttl_seconds: $ttl}')"
}

# Only the ones actually needed, per step 2's answers:
put_credential github-host-login       # if GitHub access was requested
put_credential codex-subscription      # if Codex was requested
put_credential claude-code-subscription # if Claude Code was requested
```

This is idempotent — safe to rerun on every project's setup; it never
touches the live cached value (per the Credential execution engine), only
the command string and TTL.

The Claude Code and Codex Credentials refresh the host's own logins
(`~/.claude/.credentials.json`, `~/.codex/auth.json`), and that rotates the
token. Anything else that refreshes the same login (the user's own `claude`
or `codex` on the host, or a side-by-side Multipass-era proxy) can revoke the
access token orca-proxy has cached. orca-proxy drops a cached value as soon
as upstream rejects it with 401, so the next request refetches it; a single
failed turn right after such a refresh is expected and recovers by itself.

**3d. Do not perform the interactive login yourself.** Authentication is an
interactive OAuth/device flow (or, for GitHub, `gh auth login`) that needs
the user's own browser session. Check each Credential's live status first —
it may already be valid from a previous project's setup on this same host —
and only ask the user to log in if it isn't:

```bash
curl -fsS "$API/credentials/github-host-login" | jq -r .status
# "empty" just means nothing is cached yet; "error" needs the user's login.
```

Hand the user the exact command for whichever Credential is in `error`
(for `empty`, the first real request in step 6 tells you):

- `github-host-login` → `gh auth login` (on the **host**, not the VM)
- `claude-code-subscription` → run `claude` and log in (on the **host**)
- `codex-subscription` → `codex login` (on the **host**)

Tell the user this only needs to happen **once per machine, ever** — not per
project, not per rebuild. Credential Values are picked up live on the next
request; no restart needed after login.

## 4. Create a properly named LXD VM

Check that Jetty can access LXD with `"$JETTY" status`. If LXD is unavailable,
ask the user to run `"$JETTY" setup` and sign out and back in if requested.
LXD group access grants root-equivalent control of the host, so the user must
authorize that setup themselves.

All VM lifecycle goes through the Jetty app's loopback API:

```bash
"$JETTY" gateway setup     # creates the LXD project, networks and gateway VM
curl -fsS "${API%/api/v1}/readyz"
"$JETTY" status    # expect the gateway service active and a recent handshake
```

Gateway setup takes a few minutes the first time while LXD downloads the
Ubuntu image and cloud-init installs the gateway tools. The daemon starts the
proxy listener when the network becomes available. Do not continue unless
`/readyz` returns `"ready": true` and status shows a recent handshake. Then
create the VM with the confirmed name and sizing:

```bash
"$JETTY" vm create <vm-name> --cpus <n> --memory <n>GiB --disk <n>GiB --image ubuntu:24.04
```

`vm create` authorizes the host user's SSH key (`~/.ssh/id_ed25519.pub` by
default, `--ssh-key PATH.pub` otherwise), gives the VM a fixed address
(`10.202.0.11` upward), registers it with orca-proxy before it first boots,
and installs the Interception CA into its trust store. It waits for
cloud-init to finish. Run noninteractive commands inside it as the `ubuntu`
user (passwordless sudo) with `"$JETTY" vm exec <vm-name> -- ...`. To copy a
file, use `"$JETTY" vm upload <vm-name> <guest-path> [--file HOST_PATH]`;
without `--file`, upload reads stdin. Add `--owner root --mode 0755` for
root-owned executables.

**Required, unconditionally, before Orca ever connects:** install a C/C++
build toolchain. This has nothing to do with the target repo's own language —
Orca's SSH relay compiles `node-pty` (a native Node addon) on the remote host
the first time it connects, to power remote terminals. Skip this and the
first connection in Orca fails with:

```
Remote terminals are unavailable: node-pty's native binding is not loadable
on this host. If it is missing the C/C++ build tools needed to compile
node-pty, install make, a C++ compiler, and python3 on the remote host, then
reconnect. ...
```

Install it now so day-one connections work:

```bash
"$JETTY" vm exec <vm-name> -- sudo apt-get update -qq
"$JETTY" vm exec <vm-name> -- sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential python3 git jq
```

(`build-essential` pulls in `make` and `g++`; `python3` is the one
`node-gyp` dependency it doesn't already include. `git` and `jq` are needed
later for the repo and for `gh`.)

If the user already added the host in Orca and hit this error *before* these
were installed: installing them now is not enough by itself — Orca cached the
failed build attempt. Have the user reconnect the SSH host in Orca (remove
and re-add it under **Settings → SSH**, or use its "reconnect" action if one
is shown) so it retries the `node-pty` build against the now-present
toolchain; no VM or sshd restart is needed.

Then clone the repo into the VM. Prefer cloning fresh inside the VM over a
live host mount, since Orca agents will run natively inside the VM and a real
git checkout avoids filesystem-passthrough edge cases:

```bash
"$JETTY" vm exec <vm-name> -- bash -c 'git clone <repo-url> ~/<reponame>'
```

(Wrap in `bash -c '...'` rather than passing `~/<reponame>` as a bare
argument — `jetty vm exec` does not itself invoke a shell, so an unquoted
`~` gets expanded by the *host's* shell first, producing a path under the
host's home directory instead of the VM's.)

If the repo isn't pushed anywhere the VM can reach, ask the user to publish
it to a remote the VM can reach before continuing.

Install the toolchain identified in step 1 inside the VM (language runtime at
the pinned version, package manager, system libs), then install project
dependencies (`npm ci`, `pip install -r requirements.txt`, etc.) and confirm
the project's normal build/test command succeeds.

If any harness was requested in step 2, also install it now — both CLIs are
npm-distributed and need Node.js **22.x specifically** (`@anthropic-ai/claude-code`
requires node >=22; npm only warns, not fails, on an older node, so this must
be right at install time rather than caught later):

```bash
"$JETTY" vm exec <vm-name> -- bash -c '
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - &&
  sudo apt-get install -y -qq nodejs
'
# then, per harness requested:
"$JETTY" vm exec <vm-name> -- sudo npm install -g @openai/codex
"$JETTY" vm exec <vm-name> -- sudo npm install -g @anthropic-ai/claude-code
```

Write each instructions file below by uploading it, e.g.
`printf '%s\n' "$NOTE" | "$JETTY" vm upload <vm-name> /home/ubuntu/.claude/CLAUDE.md`.

Drop a short note into each installed harness's global instructions file
(`~/.codex/AGENTS.md` for codex, `~/.claude/CLAUDE.md` for claude-code — never
the target repo's own `AGENTS.md`/`CLAUDE.md`) covering these points:

- It's running in a disposable-feeling but actually persistent VM
  with full sudo, so the agent doesn't over-hedge on system changes.
- If GitHub access was requested, `gh` here is **not** the real GitHub CLI —
  it's `gh-rest.py` (see step 6), installed at `/usr/local/bin/gh`, which
  implements the same subcommands (`pr`, `issue`, `workflow`, `run`, `api`,
  `auth status`) entirely against the REST API, since the Rules set up in
  step 6 only cover specific REST paths on `api.github.com` and GitHub's
  GraphQL endpoint (`api.github.com/graphql`) isn't one of them. Day-to-day
  usage is unchanged (`gh pr create`, `gh issue list`, `gh api ...`, etc. all
  work as expected). Two operations have no REST equivalent at all in
  GitHub's API — `gh pr merge --auto` (enabling auto-merge) and `gh pr ready`
  (marking a draft PR ready for review) — and fail with a clear error instead
  of silently doing nothing; do those manually in the GitHub web UI.

## 5. Set up SSH access

The host has no address on the agent network. Instead, the gateway forwards
one port per VM to that VM's SSH (`10.201.0.2`, port 2200 + the last octet of
the VM's address), accepting only the host. That is still a single, ordinary
SSH connection. `jetty vm create` already authorized the key; generate the matching
`~/.ssh/config` entries:

```bash
"$JETTY" ssh-config    # entries for the gateway and every Jetty VM
```

Jetty writes the generated entries to `~/.ssh/jetty_config` after gateway
setup and VM create/delete, then adds a top-level `Include jetty_config` to
`~/.ssh/config` if needed. It preserves the rest of the user's config. Do not
rewrite either file yourself. If the user's private key is not available to
the SSH agent or a standard SSH key lookup, ask which `IdentityFile` they
want to use and have them add that setting to their Jetty host entries.

Each VM entry carries `HostKeyAlias <vm-name>`, so its host key is stored
under the VM's name. Accept it on first use, then verify:

```bash
ssh -o StrictHostKeyChecking=accept-new <vm-name> echo ok
```

If a VM with the same name was deleted and recreated, remove the old key
first (`ssh-keygen -R <vm-name>`); a "host key verification failed" error
after a re-launch means exactly that.

Both the VM's address and the gateway's are fixed, so these entries survive
reboots.

Orca does not read these entries (step 7). It connects to the VM's own address
(`"$JETTY" vm ip <vm-name>`) through the gateway as a jump host, using the system
`ssh` with no terminal, so the VM's host key must also be trusted **under that
IP**; the alias above doesn't count, and Orca fails with "Host key
verification failed" (plus a harmless `ssh_askpass` error). `known_hosts` is
usually hashed, so add the entry with `ssh-keyscan` rather than editing text.
Fetch the key through the gateway, and compare its fingerprint with the one
the VM reports over the verified `ssh <vm-name>` connection before trusting it:

```bash
VM_IP=$("$JETTY" vm ip <vm-name>)
ssh ubuntu@10.201.0.2 "ssh-keyscan -t ed25519 $VM_IP 2>/dev/null" > "$TMPDIR/hk"
ssh-keygen -lf "$TMPDIR/hk"
ssh <vm-name> 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub'   # must match
cp ~/.ssh/known_hosts ~/.ssh/known_hosts.bak && cat "$TMPDIR/hk" >> ~/.ssh/known_hosts
ssh -o BatchMode=yes -J ubuntu@10.201.0.2 ubuntu@$VM_IP echo ok
```

If a VM with the same name was deleted and recreated, its address may be
reused with a new key; `ssh-keygen -R <vm-ip>` first.

Do not proceed to step 6 until `ssh <vm-name> echo ok` and the jump-host
command above both succeed.

## 6. Register the VM with orca-proxy and wire the harness(es), git, and gh

Skip this step entirely if steps 3c and 3d were skipped (no harness, no GitHub access).

### Confirm registration

`jetty vm create` already registered the VM. Confirm it, and that the
proxy is ready:

```bash
curl -fsS "$API/vms/<vm-name>"
curl -fsS "${API%/api/v1}/readyz"
```

`jetty vm create` also installed the Interception CA into the VM's system trust
store. Trust is verified below, once a Rule exists to actually exercise it
through — confirming "the file landed" isn't enough, per the CA lifecycle
decision.

### Create Rules for this project

Narrow to exactly what step 2 confirmed. Check `curl -fsS "$API/rules"` for
priorities already in use — duplicates are rejected with `409`. For GitHub:

```bash
curl -fsS -X PUT "$API/rules/<vm-name>-github-api" \
  -H 'Content-Type: application/json' -d '{
    "priority": <next available>,
    "vm_selector": {"type": "only", "vms": ["<vm-name>"]},
    "hostname": "api.github.com",
    "action": {"type": "allow_with_credential", "credential": "github-host-login",
               "path_prefix": "/repos/<org>/<repo>", "injection": {"type": "bearer"}}
  }'
# only if push access was requested — path_prefix MUST exactly match the
# request path git's smart-HTTP client actually sends, which is whatever
# the repo's own `origin` remote says verbatim (git does NOT normalize a
# ".git"-less remote by appending ".git", nor strip it from one that has
# it) -- run `git remote get-url origin` in the target repo and mirror it
# exactly. Rules of thumb: a repo cloned via `git clone https://github.com/
# <org>/<repo>.git` keeps ".git" in its remote forever, one cloned via
# `.../<repo>` (no ".git") never gets it added. Getting this wrong is a
# same-shaped bug either direction, not just the ".git"-missing case: the
# rule engine's path_prefix match is segment-boundary-aware (matches only
# on "/" or end-of-string), so "/<org>/<repo>" and "/<org>/<repo>.git" are
# two different, mutually exclusive prefixes -- whichever one doesn't match
# the actual remote silently 401s every git push/fetch/pull (falls through
# to default-Allow passthrough with no credential injected, so it reaches
# GitHub for real and gets a real "Invalid username or token" back, not an
# obvious proxy-side error):
curl -fsS -X PUT "$API/rules/<vm-name>-github-git" \
  -H 'Content-Type: application/json' -d '{
    "priority": <next available>,
    "vm_selector": {"type": "only", "vms": ["<vm-name>"]},
    "hostname": "github.com",
    "action": {"type": "allow_with_credential", "credential": "github-host-login",
               "path_prefix": "/<org>/<repo>.git", "injection": {"type": "basic", "username": "x-access-token"}}
  }'
```

For Claude Code (only if requested) — the real inference host, confirmed
from the shipped CLI itself, not a broker:

```bash
curl -fsS -X PUT "$API/rules/<vm-name>-claude" \
  -H 'Content-Type: application/json' -d '{
    "priority": <next available>,
    "vm_selector": {"type": "only", "vms": ["<vm-name>"]},
    "hostname": "api.anthropic.com",
    "action": {"type": "allow_with_credential", "credential": "claude-code-subscription",
               "path_prefix": "/", "injection": {"type": "bearer"}}
  }'
```

For Codex (only if requested) — `chatgpt.com`, not `api.openai.com`: the
`codex-subscription` Credential refreshes the ChatGPT-plan subscription
tokens, which is a different inference host than API-key mode:

```bash
curl -fsS -X PUT "$API/rules/<vm-name>-codex" \
  -H 'Content-Type: application/json' -d '{
    "priority": <next available>,
    "vm_selector": {"type": "only", "vms": ["<vm-name>"]},
    "hostname": "chatgpt.com",
    "action": {"type": "allow_with_credential", "credential": "codex-subscription",
               "path_prefix": "/", "injection": {"type": "bearer"}}
  }'
```

### Configure the harness(es) — no explicit proxy config

This is the key behavior change from the old CLIProxyAPI-broker setup:
neither harness needs a custom base URL, a custom `config.toml` provider
pointed at a broker, or any credential the VM's client itself treats as
real. The gateway already forces their real, default outbound traffic through
orca-proxy transparently; each just needs a placeholder credential on its
own native auth surface so it sends a real request with *something* for the
Rule above to overwrite.

**Claude Code** — one env var, base URL left **unset** (leaving it unset
isn't just simpler, it's strictly better: an explicit non-default
`ANTHROPIC_BASE_URL` disables Remote Control and MCP tool search, per
Anthropic's own docs):

```bash
"$JETTY" vm exec <vm-name> -- bash -c "
  sudo sed -i '/^ANTHROPIC_AUTH_TOKEN=/d' /etc/environment
  printf 'ANTHROPIC_AUTH_TOKEN=placeholder\n' | sudo tee -a /etc/environment >/dev/null
  printf 'export ANTHROPIC_AUTH_TOKEN=placeholder\n' | sudo tee /etc/profile.d/orca-proxy-claude.sh >/dev/null
"
```

**Codex** — a custom-provider `config.toml` block, the same shape the old
broker setup already used, just repointed at the real ChatGPT-plan host
instead of the broker (this exact mechanism is what's already proven to
work for the subscription mode the Codex Credential targets):

```bash
"$JETTY" vm exec <vm-name> -- bash -c "
  mkdir -p ~/.codex
  cat > ~/.codex/config.toml <<'TOML'
model_provider = \"hostproxy\"

[model_providers.hostproxy]
name = \"hostproxy\"
base_url = \"https://chatgpt.com/backend-api/codex\"
wire_api = \"responses\"
env_key = \"HOSTPROXY_KEY\"
TOML
  sudo sed -i '/^HOSTPROXY_KEY=/d' /etc/environment
  printf 'HOSTPROXY_KEY=placeholder\n' | sudo tee -a /etc/environment >/dev/null
  printf 'export HOSTPROXY_KEY=placeholder\n' | sudo tee /etc/profile.d/orca-proxy-codex.sh >/dev/null
"
```

Now confirm CA trust actually landed — this needs a real intercepted call to
prove, which is why it's checked here rather than right after installing it.
**No `-f`** — a bare `GET /` against `api.anthropic.com` legitimately 502s
(it's not a real endpoint), and `-f` would treat that as a curl failure,
masking the actual signal. The thing this check verifies is that the TLS
handshake completed and the response has an `Orca Local Interception CA`
issuer, not that the HTTP status is 2xx:

```bash
"$JETTY" vm exec <vm-name> -- bash -lc '
  curl -sS -o /dev/null https://api.anthropic.com/ \
    && echo "CA trust OK (a non-2xx status here is expected and fine — this only confirms curl completed the TLS handshake without a certificate error)"
'
```

Then verify the whole chain with one real call per harness — fail loudly
here rather than let the user discover a bare auth error on the harness's
first real turn:

```bash
"$JETTY" vm exec <vm-name> -- bash -lc 'claude -p "say ok" --output-format text'
"$JETTY" vm exec <vm-name> -- bash -lc 'cd ~/<reponame> && codex exec "say ok"'
```

(`codex exec` refuses to run outside a git repository unless given
`--skip-git-repo-check`, so run it from the clone.)

If either fails, check `curl "${API%/api/v1}/readyz"` and that
Credential's live status on the host first (see also step 8) — a down service or an invalid
Credential fails every harness identically, and shouldn't be mistaken for a
VM-side problem.

### git / gh CLI

Skip this subsection if step 2 established the VM needs no GitHub access.

Same simplification as the harnesses: no `HTTPS_PROXY`, no wrapper script,
no per-host git `.proxy` config — the gateway already routes `github.com`/
`api.github.com` transparently once the Rules above exist. git only needs
the placeholder credential helper so `git push` doesn't hang prompting
interactively (CA trust is already installed above):

```bash
"$JETTY" vm exec <vm-name> -- bash -c "
  git config --global credential.https://github.com.helper '!f() { echo username=x-access-token; echo password=placeholder; }; f'
"
```

Commits also need an identity. Ask the user which one to use; the host's
own is a sensible default:

```bash
"$JETTY" vm exec <vm-name> -- git config --global user.name "$(git config --global user.name)"
"$JETTY" vm exec <vm-name> -- git config --global user.email "$(git config --global user.email)"
```

Install `gh` as `gh-rest.py` — a REST-only reimplementation of the `gh`
subcommands this project needs, **not** the real GitHub CLI. The reason:
the real `gh` binary routes several common subcommands (`gh pr create`,
`gh run watch`, ...) through GitHub's GraphQL endpoint, and the Rules above
only allow-list specific REST paths on `api.github.com` — GraphQL isn't one
of them, by design (a Rule that also had to allow-list arbitrary GraphQL
queries wouldn't meaningfully restrict anything). Rather than keep
documenting one-off REST workarounds per GraphQL-backed subcommand, this
installs a `gh` that only ever speaks REST in the first place, so ordinary
usage (`gh pr create`, `gh issue list`, `gh run watch`, `gh api ...`) just
works unmodified. It needs `python3` and `jq` (`--jq` filtering shells out to it rather than
reimplementing jq's expression language), both installed in step 4.

```bash
```

`jq` was already installed in step 4.

Its complete source lives in the installed release at `gh-rest/gh-rest.py` —
read it from there rather than a checkout's `main` branch, and pipe it in
over stdin:

```bash
"$JETTY" vm upload <vm-name> /usr/local/bin/gh --owner root --mode 0755 \
  --file "$JETTY_TOOLS_DIR/gh-rest.py"
"$JETTY" vm exec <vm-name> -- gh --help
```

`gh run watch <run-id>` (accepting `--exit-status`, `-i <seconds>`, and
`-R <owner/repo>`) is built into this `gh`, polling only
`GET /repos/{owner}/{repo}/actions/runs/{run_id}` and its `/jobs` child — so
the repository-scoped `api.github.com` Rule above covers it without a
separate helper binary.

Two `gh` operations genuinely have no REST equivalent in GitHub's API at
all — `gh pr merge --auto` (enabling auto-merge) and `gh pr ready` (marking
a draft PR ready for review), both GraphQL-only mutations — and this `gh`
fails those loudly with an explanation rather than silently no-op'ing. Do
those two specifically through the GitHub web UI.

`gh` reads `GH_TOKEN` (falling back to `GITHUB_TOKEN`) but doesn't require
either to be set — with neither set it sends no `Authorization` header at
all, which lets unmatched paths fall through to GitHub as a normal anonymous
request instead of a hard failure. Set a global placeholder anyway, so the
common case (`gh api repos/<org>/<repo>/...`) always sends *some*
`Authorization` header for the Rule to overwrite — `GH_TOKEN` only affects
`gh`, not npm/curl/other tools in the VM:

```bash
"$JETTY" vm exec <vm-name> -- bash -c "
  sudo sed -i '/^GH_TOKEN=/d' /etc/environment
  printf 'GH_TOKEN=placeholder\n' | sudo tee -a /etc/environment >/dev/null
  printf 'export GH_TOKEN=placeholder\n' | sudo tee /etc/profile.d/orca-proxy-gh.sh >/dev/null
"
```

Verify both the allowed and unmatched paths — **note the changed semantics
from the old gh-proxy**: an unmatched path is no longer a proxy-generated
`403`, it now passes through untouched to the real upstream
(default-Allow-unmatched), so the "denied" check below expects GitHub's own
real anonymous-request response, not a block signature:

```bash
# allowed — expect a real (possibly empty) JSON response, credential injected:
"$JETTY" vm exec <vm-name> -- bash -lc 'gh api repos/<org>/<repo>/issues'
# unmatched path — expect GitHub's own real anonymous-request response (e.g. 401), NOT a proxy block:
"$JETTY" vm exec <vm-name> -- bash -lc 'gh api user'
# git through the Rule (works for any repo it covers):
"$JETTY" vm exec <vm-name> -- bash -lc 'git ls-remote <repo-url>'
# push access, if requested — authenticates without pushing anything:
"$JETTY" vm exec <vm-name> -- bash -lc 'cd ~/<reponame> && git push --dry-run origin HEAD:refs/heads/jetty-push-check'
```

`gh auth status` inside the VM doesn't query GitHub at all — it never could
pass: `/user` isn't covered by any Allow-with-credential Rule, so hitting it
for real would just forward credential-free by default-Allow and come back
"not authenticated," which reads like something's broken and sends an agent
chasing a non-issue for no reason. `gh-rest.py` instead prints "A limited
number of GitHub operations are authenticated" and exits 0, with no request
sent. Don't try to make it report a real logged-in identity; that would
mean a real token had reached the VM.

Confirm unrelated traffic is still unaffected — this check matters more now
than it used to, since the gateway forces *all* of the VM's 80/443 through
orca-proxy, not just the tools explicitly wired to a proxy:

```bash
"$JETTY" vm exec <vm-name> -- bash -lc 'npm view left-pad version'  # resolves normally via default-Allow passthrough
"$JETTY" vm exec <vm-name> -- bash -lc 'git ls-remote https://gitlab.com/gitlab-org/gitlab-foss.git HEAD'  # non-GitHub remote, unaffected
```

Finally, verify the *agent* — not just a manual shell command — can actually
drive `gh` through this chain, since that's the thing that matters. Run it
non-interactively inside the VM with `-p`:

```bash
"$JETTY" vm exec <vm-name> -- bash -lc \
  'cd ~/<reponame> && claude -p "Run: gh api repos/<org>/<repo>/issues --jq \". | length\" and tell me the number." --output-format text'
```

If this comes back with `Permission for this action was denied by the
Claude Code auto mode classifier`, that's unrelated to the proxy — it's
Claude Code's own server-side classifier being extra cautious about
`--dangerously-skip-permissions` specifically in non-interactive contexts.
Don't reach for `--dangerously-skip-permissions` to work around it; instead
pre-grant the exact command in `~/.claude/settings.json` inside the VM
(`{"permissions": {"allow": ["Bash(gh api repos/<org>/<repo>/issues:*)"]}}`)
and retry without that flag. Remove any settings file you added purely to
run this check once it passes — it's a verification step, not part of the
intended end state.

## 7. Tell the user exactly how to add this as an Orca SSH project

Give the user these concrete, copy-pasteable steps (fill in the real values
you just set up):

1. Open Orca → **Settings → SSH**.
2. Add a new host. Do **not** use the gateway's `10.201.0.2` plus a forwarded
   port: every VM shares that address, and Orca refuses a second one as "That
   SSH host is already in Orca". Use the VM's own address and the gateway as a
   jump host instead (Orca's **Jump Host** field is `ssh -J`):
   - **Host/IP**: the VM's address (`"$JETTY" vm ip <vm-name>`, e.g. `10.202.0.12`)
   - **Port**: `22`
   - **User**: `ubuntu`
   - **Identity file**: `~/.ssh/id_ed25519` (or whichever key was authorized)
   - **Jump Host**: `ubuntu@10.201.0.2` (the gateway)
   - **Proxy Command**: leave empty. If a build without the Jump Host field is
     in use, `ssh -W %h:%p ubuntu@10.201.0.2` is the equivalent.
   - **Name**: `<vm-name>` (so it's recognizable in the "Run on" picker)
3. Verify the connection in Orca's SSH settings (it should confirm git is
   available on the host).
4. Create a new worktree for the repo, and under **Run on**, select
   `<vm-name>` instead of Local.
5. Confirm the project directory Orca finds on the remote matches
   `~/<reponame>` (or wherever the repo was cloned in step 4).
6. If a harness was installed, tell the user which one(s) are ready to use
   with no further login step (`codex`, `claude`) — orca-proxy already
   injects their credentials transparently.
7. If GitHub access was wired up, tell the user exactly which repo(s) and
   operations the agent can use (`git clone`/`push`, `gh pr`/`issue`
   read/create) — and which it explicitly can't (anything off the Rules,
   e.g. other repos or `gh api user`) — so they aren't surprised by an
   unexpected result mid-task.

Remind the user that agents and `git worktree` will now execute on the VM,
while Orca's editor/diff/UI stay local; that stopping/deleting the VM
(`jetty vm stop|delete <vm-name>`) will break the SSH target until it's
recreated; and that orca-proxy and the `jetty-gw` gateway VM are now
**shared, hard dependencies** for every Jetty VM, not just this one — if
either is down, every Jetty VM loses outbound 80/443 connectivity entirely
(fail-closed), not just credentialed calls.

## 8. Troubleshooting

Check these before changing anything. Set the `JETTY`, `API` and
`JETTY_TOOLS_DIR` variables from step 3a first.

| Symptom | Cause and fix |
| --- | --- |
| A harness gets `401` once, then works | A host-side refresh revoked the cached token (see 3c); orca-proxy refetched it. Nothing to do. |
| A harness keeps getting `401` | The host login itself is broken. Check `curl "$API/credentials/<name>"`; if `error`, the user logs in again on the host. |
| The proxy is waiting for its network | Run `"$JETTY" gateway setup`, then wait for `/readyz` to become ready. |
| `ssh <vm-name>` says "host key verification failed" | The VM was re-launched under the same name. `ssh-keygen -R <vm-name>`, then accept the new key. |
| `ssh <vm-name>` times out | `"$JETTY" status`; the gateway must be running with `jetty-gateway.service` active. `"$JETTY" gateway setup` reapplies its config. |
| The VM has no web access at all | Fail-closed by design when the proxy, the tunnel or the gateway is down. Check `/readyz`, then `"$JETTY" status`. |
| The VM can't reach a LAN, VPN or host address | By design: Jetty VMs can't reach private addresses, IPv6 or other Jetty VMs. |
| Orca says "That SSH host is already in Orca" | It keys hosts on address, and every VM shares the gateway's `10.201.0.2`. Add the VM by its own address with the gateway as Jump Host (step 7). |
| Orca says "System SSH probe failed ... Host key verification failed" | The VM's key isn't trusted under its IP. Add it with `ssh-keyscan` as in step 5. |
| `gh api user` returns 401 | Expected: no Rule covers `/user`. |
| A request from the VM is refused with no rule match | Its source isn't registered. `curl "$API/vms"` should list the VM with the address `"$JETTY" vm ip <vm-name>` prints. |

The host may have `br_netfilter` loaded (Docker loads it), which makes the
host's own netfilter rules see traffic crossing the Jetty bridges. The
gateway already compensates for the case that broke SSH; keep it in mind if
some new path through the gateway mysteriously loses its replies.

### Removing a VM, or everything

```bash
# Its Rules first, or the proxy refuses to unregister a VM they reference:
curl -fsS "$API/rules" | jq -r --arg vm <vm-name> '.rules[] | select(.vm_selector.vms // [] | index($vm)) | .name'
curl -fsS -X DELETE "$API/rules/<rule-name>"   # for each name printed
"$JETTY" vm delete <vm-name>                          # deletes the VM and unregisters it
```

Then remove its entry from `~/.ssh/jetty_config` (or regenerate the file
with `"$JETTY" ssh-config`) and `ssh-keygen -R <vm-name>`.

To remove the whole LXD mode (only with the user's explicit OK — it stops
every Jetty VM):

Jetty does not currently provide a whole-project teardown command. Do not run
raw `lxc` commands from the skill. Hand this cleanup to the user to perform
with their LXD management tool, then disable the daemon with:

```bash
systemctl --user disable --now jetty-daemon.service
```
