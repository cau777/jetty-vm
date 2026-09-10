---
name: orca-ssh-setup
description: Provision a Multipass VM as an SSH run target for the current project (with coding-agent harnesses and git/gh CLI wired to the host-side orca-proxy credential-injection & logging proxy) and connect it to Orca (onorca.dev) as an SSH-type project.
---

# Orca SSH Project Setup

Use this procedure when the user wants to run their current repo's Orca agents
on a dedicated local VM instead of their laptop, connected via SSH (Orca's
"SSH target" run mode — see https://www.onorca.dev/docs/ways-to-run, mode #2).

Scope note: this is **one long-lived VM per project**, reused across
worktrees/branches.

The end state: a running Multipass VM, reachable over SSH with key-based auth,
with the project's toolchain **and** the requested coding-agent harness(es)
installed, its outbound traffic transparently enforced by the host-side
**orca-proxy** service (no explicit proxy configuration inside the VM), and
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
- **Base Ubuntu image** — default to the latest Multipass LTS image (check
  `multipass find` for the current default, e.g. `24.04`) unless the repo
  needs a specific OS version.
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
  services) that isn't reachable by default from a Multipass VM.

Only proceed to provisioning once these are settled.

## 3. Set up the host-side orca-proxy service and register Credentials

Skip this step entirely if step 2 established the VM needs no coding-agent
harness and no GitHub access.

Coding-agent and GitHub credentials must **not** be baked into the VM — a
live provider session sitting on a machine that an agent runs with full sudo
on is a bad combination, and it means every rebuild/recreate of the VM costs
another interactive login. **orca-proxy** is the host-side app that replaces
the previous CLIProxyAPI broker and mitmproxy gh-proxy combination with one
unified service: it forces all outbound 80/443 traffic from each registered
VM through itself via host-side DNAT (agent-proof — a VM's root user cannot
bypass it), and injects a live Credential Value only into requests matching
an explicit Rule. It runs once per *machine* (share it across every project
VM, not per project).

**3a. Check for an existing installation first** — it may already be running
from a previous project:

```bash
systemctl --user status orca-proxy.service
```

If active, skip straight to 3c — do not reinstall or restart a service that
may already be enforcing other projects' VMs.

**3b. If absent, install it.** The install script must run as root in its
entirety, not just for two small steps — it installs the firewall-sync
helper to a root-owned location outside the target user's home directory,
which only root can arrange (a NOPASSWD sudoers entry pointing at anything
writable by the user it names is a straight escalation, which an earlier
version of this install had).

**Never attempt this with `sudo -n`, even if it happens to succeed
passwordlessly.** Unlike the sudoers-gated firewall-sync helper the
Management API calls routinely (safe to auto-invoke *because* it's a
root-owned binary with pinned arguments the `cau777` account cannot
influence), `install.sh` is an arbitrary, user-writable script — running it
as root without a human typing their password would just re-create the same
escalation shape one level up (anything that can write to this checkout
could get root, unattended, the moment a general passwordless-sudo grant
exists for any reason). Installing/upgrading orca-proxy is a deliberate,
human-authorized action, full stop — hand the user the exact command and
wait for them to run it themselves:

The installed Jetty skill includes a version file. Read it and direct the user
to the matching persisted release source — never a checkout's `main` branch or
a mutable raw-GitHub URL:

```bash
JETTY_RELEASE_DIR="$HOME/.local/share/jetty/releases/v1.0.0"
echo "Need one-time root access to install the matching orca-proxy — please run:" >&2
echo "  sudo env ORCA_PROXY_VERSION=$(basename "$JETTY_RELEASE_DIR") bash $JETTY_RELEASE_DIR/orca-proxy/deploy/install.sh" >&2
```

If the skill was installed by another method and that directory is absent, ask
the user to install a Jetty release first; do not substitute an unpinned source.

Everything *unprivileged* (the venv, the systemd **user** unit, the data
directory) still ends up owned by and running as the invoking user, not
root — only the sudoers file and the firewall-sync helper's install
location need the elevated run. Upgrading later is the same command, run
again by the user — there is no passwordless path left that reprovisions
the privileged half on its own, by design (see the installed orca-proxy
repo's `design.md`).

Confirm it's actually ready before continuing — this is more than "is the
process running," it also covers migrations and CA materialization:

```bash
curl -fsS http://127.0.0.1:8080/readyz
```

**3c. Ensure the needed Credentials exist.** Credential creation is a plain
`PUT` the Provisioning Agent issues directly — it is not gated behind the
Web UI's Quick Add button, which is a human convenience layered on the same
Management API, not a separate mechanism. Read the source-of-truth catalog
(the same file Quick Add and the compatibility tests both load) rather than
hardcoding command strings here:

```bash
CATALOG=~/.orca-proxy/current/source/src/orca_proxy/static/quick-add-catalog.json

put_credential() {
  local key="$1" entry command ttl
  entry=$(jq -c --arg k "$key" '.[] | select(.key == $k)' "$CATALOG")
  command=$(echo "$entry" | jq -r .command)
  ttl=$(echo "$entry" | jq -r .ttl_seconds)
  curl -fsS -X PUT "http://127.0.0.1:8080/api/v1/credentials/${key}" \
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

**3d. Do not perform the interactive login yourself.** Authentication is an
interactive OAuth/device flow (or, for GitHub, `gh auth login`) that needs
the user's own browser session. Check each Credential's live status first —
it may already be valid from a previous project's setup on this same host —
and only ask the user to log in if it isn't:

```bash
curl -fsS http://127.0.0.1:8080/api/v1/credentials/github-host-login | jq -r .status
```

Hand the user the exact command for whichever Credential isn't yet `valid`:

- `github-host-login` → `gh auth login` (on the **host**, not the VM)
- `claude-code-subscription` → run `claude` and log in (on the **host**)
- `codex-subscription` → `codex login` (on the **host**)

Tell the user this only needs to happen **once per machine, ever** — not per
project, not per rebuild. Credential Values are picked up live on the next
request; no restart needed after login.

## 4. Create a properly named Multipass VM

Check Multipass is installed first (`multipass version`); if not, stop and
tell the user to install it (https://multipass.run) before continuing.

Launch the VM with the confirmed name and sizing:

```bash
multipass launch <image> \
  --name <vm-name> \
  --cpus <n> \
  --memory <n>G \
  --disk <n>G
```

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
multipass exec <vm-name> -- sudo apt-get update -qq
multipass exec <vm-name> -- sudo apt-get install -y -qq build-essential python3
```

(`build-essential` pulls in `make` and `g++`; `python3` is the one
`node-gyp` dependency it doesn't already include.)

If the user already added the host in Orca and hit this error *before* these
were installed: installing them now is not enough by itself — Orca cached the
failed build attempt. Have the user reconnect the SSH host in Orca (remove
and re-add it under **Settings → SSH**, or use its "reconnect" action if one
is shown) so it retries the `node-pty` build against the now-present
toolchain; no VM or sshd restart is needed.

Then mount or clone the repo into the VM. Prefer cloning fresh inside the VM
over a live host mount, since Orca agents will run natively inside the VM and
a real git checkout avoids filesystem-passthrough edge cases:

```bash
multipass exec <vm-name> -- bash -c 'git clone <repo-url> ~/<reponame>'
```

(Wrap in `bash -c '...'` rather than passing `~/<reponame>` as a bare
argument — `multipass exec` does not itself invoke a shell, so an unquoted
`~` gets expanded by the *host's* shell before multipass ever sees it,
producing a path under the host's home directory instead of the VM's.)

If the repo isn't pushed anywhere the VM can reach, use
`multipass mount <host-path> <vm-name>:<vm-path>` instead and note this as a
deviation to the user.

Install the toolchain identified in step 1 inside the VM (language runtime at
the pinned version, package manager, system libs), then install project
dependencies (`npm ci`, `pip install -r requirements.txt`, etc.) and confirm
the project's normal build/test command succeeds.

If any harness was requested in step 2, also install it now — both CLIs are
npm-distributed and need Node.js **22.x specifically** (`@anthropic-ai/claude-code`
requires node >=22; npm only warns, not fails, on an older node, so this must
be right at install time rather than caught later):

```bash
multipass exec <vm-name> -- bash -c '
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - &&
  sudo apt-get install -y -qq nodejs
'
# then, per harness requested:
multipass exec <vm-name> -- sudo npm install -g @openai/codex
multipass exec <vm-name> -- sudo npm install -g @anthropic-ai/claude-code
```

Drop a short note into each installed harness's global instructions file
(`~/.codex/AGENTS.md` for codex, `~/.claude/CLAUDE.md` for claude-code — never
the target repo's own `AGENTS.md`/`CLAUDE.md`) covering these points:

- It's running in a disposable-feeling but actually persistent Multipass VM
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

Multipass VMs run SSH by default but Orca needs a real, host-reachable
SSH endpoint with key-based auth (not `multipass shell`/`multipass exec`,
which tunnel through the Multipass daemon instead of plain SSH).

1. Get the VM's IP:
   ```bash
   multipass info <vm-name> | grep IPv4
   ```
2. Ensure the user has an SSH keypair (`~/.ssh/id_ed25519` or similar); if
   not, generate one (`ssh-keygen -t ed25519`).
3. Authorize that public key inside the VM:
   ```bash
   multipass exec <vm-name> -- bash -c \
     'mkdir -p ~/.ssh && echo "<contents of id_ed25519.pub>" >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'
   ```
4. Verify from the host:
   ```bash
   ssh -i ~/.ssh/id_ed25519 ubuntu@<vm-ip> echo ok
   ```
   (default Multipass user is `ubuntu` unless the user customized it).
5. Note that the VM's IP can change across host reboots unless the user has
   set a static IP/DHCP reservation — flag this to the user, since a changed
   IP will break both the Orca SSH target and the VM's registration with
   orca-proxy (step 6) until updated.

Do not proceed to step 6 until the `ssh ... echo ok` check above succeeds.

## 6. Register the VM with orca-proxy and wire the harness(es), git, and gh

Skip this step entirely if step 3 was skipped (no harness, no GitHub access).

### Register the VM and confirm enforcement

```bash
VM_IP=$(multipass info <vm-name> | awk '/IPv4/{print $2}')
curl -fsS -X PUT "http://127.0.0.1:8080/api/v1/vms/<vm-name>" \
  -H 'Content-Type: application/json' -d "{\"ip_address\": \"${VM_IP}\"}"
```

This alone triggers firewall reconciliation — DNAT now forces this VM's
outbound 80/443 through orca-proxy. Poll `/readyz` until this specific VM's
firewall status is `in_sync` before doing anything else — there's no safe
way to proceed with Rules while this VM's enforcement is unconfirmed, so
fail loud rather than continue past a timeout:

```bash
# No -f here: /readyz legitimately returns 503 while still unsynced, and the
# body — which -f would discard — is exactly what this loop needs to read.
SYNCED=""
for i in $(seq 1 15); do
  SYNCED=$(curl -sS http://127.0.0.1:8080/readyz | jq -r --arg vm "<vm-name>" '.firewall_status[$vm] // "missing"')
  [ "$SYNCED" = "in_sync" ] && break
  sleep 2
done
[ "$SYNCED" = "in_sync" ] || {
  echo "firewall sync did not complete for <vm-name> — resolve before continuing" >&2
  exit 1
}
```

Install the Interception CA into the VM's system trust store — required for
Allow-with-credential hosts to work transparently. Piping through
`multipass exec` as base64 sidesteps the same snap-confinement
`multipass transfer` permission error the previous gh-proxy setup worked
around:

```bash
B64=$(curl -fsS http://127.0.0.1:8080/api/v1/ca | jq -r .certificate_pem | base64 -w0)
multipass exec <vm-name> -- bash -c "echo '$B64' | base64 -d | sudo tee /usr/local/share/ca-certificates/orca-proxy-ca.crt > /dev/null && sudo update-ca-certificates"
```

(Trust is verified below, once a Rule exists to actually exercise it through
— confirming "the file landed" isn't enough, per the CA lifecycle decision.)

### Create Rules for this project

Narrow to exactly what step 2 confirmed. Check `GET /api/v1/rules` for
priorities already in use — duplicates are rejected with `409`. For GitHub:

```bash
curl -fsS -X PUT "http://127.0.0.1:8080/api/v1/rules/<vm-name>-github-api" \
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
curl -fsS -X PUT "http://127.0.0.1:8080/api/v1/rules/<vm-name>-github-git" \
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
curl -fsS -X PUT "http://127.0.0.1:8080/api/v1/rules/<vm-name>-claude" \
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
curl -fsS -X PUT "http://127.0.0.1:8080/api/v1/rules/<vm-name>-codex" \
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
real. DNAT already forces their real, default outbound traffic through
orca-proxy transparently; each just needs a placeholder credential on its
own native auth surface so it sends a real request with *something* for the
Rule above to overwrite.

**Claude Code** — one env var, base URL left **unset** (leaving it unset
isn't just simpler, it's strictly better: an explicit non-default
`ANTHROPIC_BASE_URL` disables Remote Control and MCP tool search, per
Anthropic's own docs):

```bash
multipass exec <vm-name> -- bash -c "
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
multipass exec <vm-name> -- bash -c "
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
multipass exec <vm-name> -- bash -lc '
  curl -sS -o /dev/null https://api.anthropic.com/ \
    && echo "CA trust OK (a non-2xx status here is expected and fine — this only confirms curl completed the TLS handshake without a certificate error)"
'
```

Then verify the whole chain with one real call per harness — fail loudly
here rather than let the user discover a bare auth error on the harness's
first real turn:

```bash
multipass exec <vm-name> -- bash -lc 'claude -p "say ok" --output-format text'
multipass exec <vm-name> -- bash -lc 'codex exec "say ok"'
```

If either fails, check `curl http://127.0.0.1:8080/readyz` and that
Credential's live status on the host first — a down service or an invalid
Credential fails every harness identically, and shouldn't be mistaken for a
VM-side problem.

### git / gh CLI

Skip this subsection if step 2 established the VM needs no GitHub access.

Same simplification as the harnesses: no `HTTPS_PROXY`, no wrapper script,
no per-host git `.proxy` config — DNAT already intercepts `github.com`/
`api.github.com` transparently once the Rules above exist. git only needs
the placeholder credential helper so `git push` doesn't hang prompting
interactively (CA trust is already installed above):

```bash
multipass exec <vm-name> -- bash -c "
  git config --global credential.https://github.com.helper '!f() { echo username=x-access-token; echo password=placeholder; }; f'
"
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
works unmodified. It needs `python3` (already installed for node-gyp in step
4) and `jq` (`--jq` filtering shells out to it rather than reimplementing
jq's expression language):

```bash
multipass exec <vm-name> -- sudo apt-get install -y -qq jq
```

Its complete source lives in this Jetty release at `gh-rest/gh-rest.py` —
read it from the installed release directory rather than a checkout's `main`
branch, and transfer it the same base64-piped way as the CA cert above
(sidesteps the same `multipass transfer` snap-confinement permission error):

```bash
JETTY_RELEASE_DIR="$HOME/.local/share/jetty/releases/v1.0.0"  # match step 3's version
B64=$(base64 -w0 "$JETTY_RELEASE_DIR/gh-rest/gh-rest.py")
multipass exec <vm-name> -- bash -c "echo '$B64' | base64 -d | sudo tee /usr/local/bin/gh > /dev/null && sudo chmod 0755 /usr/local/bin/gh"
multipass exec <vm-name> -- gh --help
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
multipass exec <vm-name> -- bash -c "
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
multipass exec <vm-name> -- bash -lc 'gh api repos/<org>/<repo>/issues'
# unmatched path — expect GitHub's own real anonymous-request response (e.g. 401), NOT a proxy block:
multipass exec <vm-name> -- bash -lc 'gh api user'
# git through the Rule (works for any repo it covers):
multipass exec <vm-name> -- bash -lc 'git ls-remote <repo-url>'
```

`gh auth status` inside the VM will still report as not authenticated —
still expected and correct, but for a different reason than before: `/user`
isn't covered by any Allow-with-credential Rule, so it's forwarded
credential-free by default-Allow rather than being blocked. Don't try to
make `gh auth status` pass; it passing would mean a real token reached the
VM.

Confirm unrelated traffic is still unaffected — this check matters more now
than it used to, since DNAT forces *all* of the VM's 80/443 through
orca-proxy, not just the tools explicitly wired to a proxy:

```bash
multipass exec <vm-name> -- bash -lc 'npm view left-pad version'  # resolves normally via default-Allow passthrough
multipass exec <vm-name> -- bash -lc 'git ls-remote https://gitlab.com/gitlab-org/gitlab-foss.git HEAD'  # non-GitHub remote, unaffected
```

Finally, verify the *agent* — not just a manual shell command — can actually
drive `gh` through this chain, since that's the thing that matters. Run it
non-interactively inside the VM with `-p`:

```bash
multipass exec <vm-name> -- bash -lc \
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
2. Add a new host with:
   - **Host/IP**: `<vm-ip>`
   - **User**: `ubuntu` (or the confirmed user)
   - **Identity file**: `~/.ssh/id_ed25519` (or whichever key was authorized)
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
while Orca's editor/diff/UI stay local; that stopping/deleting the Multipass
VM (`multipass stop|delete <vm-name>`) will break the SSH target until it's
recreated; and that orca-proxy is now a **shared, hard dependency** for
every project VM registered with it, not just this one — if
`orca-proxy.service` is down, every registered VM loses outbound 80/443
connectivity entirely (fail-closed, per its firewall design), not just
credentialed calls.
