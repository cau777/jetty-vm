# orca-proxy — Design Spec

This document is the written design spec produced by the `wayfinder` planning
cycle on [cau777/jetty-vm#1](https://github.com/cau777/jetty-vm/issues/1) ("Unified
VM credential-injection & logging proxy"). It consolidates the decisions
reached across tickets #2–#17 into one reference, and reflects the shipped
implementation — every schema, endpoint, and algorithm described below is the
one actually in `src/orca_proxy/`, not an aspirational draft.

## Destination

A single self-hosted app (Web UI + Management API) replacing both the old
CLIProxyAPI broker and the mitmproxy gh-proxy from the `orca-ssh-setup` skill.
It forces all outbound 80/443 traffic from each registered VM through itself
via host-side DNAT on the Multipass bridge (agent-proof — a VM's root user
cannot bypass it), inspects SNI/Host on every connection for logging and
Block/Allow decisions, and only fully MITMs hosts reached by an
Allow-with-credential rule — everything else passes through untouched.

Domain: a single-user, single-host local dev tool for Multipass VM
provisioning, not a multi-tenant/SaaS system.

## Entity model

Three named resources, persisted in `state.sqlite` (`src/orca_proxy/migrations/0001_init.sql`):

### VM
```
name         TEXT PRIMARY KEY   -- lowercase slug, immutable
ip_address   TEXT UNIQUE        -- IPv4, validated
created_at / updated_at
```

### Credential
```
name          TEXT PRIMARY KEY  -- lowercase slug, immutable
command       TEXT              -- trusted bash command, run via /usr/bin/bash -lc
ttl_seconds   INTEGER           -- 0 disables caching
created_at / updated_at
```
A Credential's *value* is never persisted — only the command string and TTL
are durable. The live value lives only in the in-memory `CredentialCache`
(#10, below).

### Rule
```
name              TEXT PRIMARY KEY
priority          INTEGER UNIQUE   -- ascending; sole tiebreaker among matches
vm_selector_json  TEXT             -- {"type":"all"} | {"type":"only","vms":[...]}
hostname          TEXT             -- bare hostname, no scheme/port/path/wildcard
action_json       TEXT             -- discriminated union, see below
created_at / updated_at
```
`action` is one of:
- `{"type": "allow"}`
- `{"type": "block"}`
- `{"type": "allow_with_credential", "credential": "<name>", "path_prefix": "/...", "injection": {...}}`
  where `injection` is `{"type": "bearer"}` or `{"type": "basic", "username": "..."}`.

Nested shapes (`vm_selector`, `action`) are stored as validated JSON text
rather than normalized tables — write-path validation (`validation.py`) is
the single source of truth for shape correctness, not the DB schema. Unknown
fields on any of the discriminated union variants are rejected, not ignored.

### Interception CA (singleton, `interception_ca` table)
```
certificate_pem, private_key_pem, fingerprint_sha256, not_before, not_after, created_at
```
One 10-year self-signed root (`cryptography.x509`, subject "Orca Local
Interception CA"), generated once on first `create_app()` startup if absent.
Lives in `state.sqlite`, not a separate keystore — there is exactly one CA
for the service's whole lifetime, no rotation in v1 (#8).

Two request-logging tables — `connections` and `http_requests` — live in a
**separate** database, `requests.sqlite` (see Logging, below).

## Management API surface (#9)

Loopback-only aiohttp app (`127.0.0.1:8080` by default), **no authentication**
(#6) — the Management API and same-origin Web UI are trusted by virtue of
binding to loopback only; VMs and remote callers have no network path to it.

All entities use full-replacement `PUT` — idempotent by construction, no
partial-update semantics, no bulk-apply, no optimistic-concurrency machinery:

```
GET    /readyz
GET    /api/v1/ca

GET    /api/v1/vms
GET    /api/v1/vms/{name}
PUT    /api/v1/vms/{name}          -> 201 create / 200 replace
DELETE /api/v1/vms/{name}          -> 204 / 404 / 409 if referenced by a Rule

GET    /api/v1/credentials
GET    /api/v1/credentials/{name}
PUT    /api/v1/credentials/{name}
DELETE /api/v1/credentials/{name}  -> 409 if referenced by a Rule

GET    /api/v1/rules
GET    /api/v1/rules/{name}
PUT    /api/v1/rules/{name}
DELETE /api/v1/rules/{name}

GET    /api/v1/requests            -- keyset-paginated, filterable
GET    /api/v1/requests/{id}       -- nested connection + child http_requests
```

Collections are unpaginated and sorted (VMs/Credentials by name, Rules by
priority) except `/api/v1/requests`, which uses keyset pagination (#11).
`PUT` validates then writes inside one transaction — a failed validation
never partially mutates the existing resource. `DELETE` on a VM/Credential
still referenced by a Rule returns `409 Conflict`; there is no cascade, the
caller must delete/update the referring Rule first.

Errors use one envelope shape, `errors.py`:
```json
{"error": {"code": "validation_failed", "message": "...", "fields": {"priority": "..."}}}
```
`fields` is present only on `422`s that pinpoint specific fields — this is
what the Web UI's editor drawer renders inline. Codes: `invalid_json` (400),
`not_found` (404), `conflict` (409), `validation_failed` (422),
`credential_unavailable` (502, proxy-only — see Credential execution below).

`GET /readyz` folds firewall-sync status into its body rather than exposing a
separate diagnostics endpoint (#12):
```json
{"ready": true, "checks": {"migrations": true, "ca_materialized": true, "firewall_synced": true},
 "firewall_status": {"<vm-name>": "in_sync", ...}}
```
Returns `503` while any check is false — vacuously ready with zero registered
VMs.

## Validation rules (#9, `validation.py`)

- **Names**: immutable lowercase slugs — `^[a-z0-9][a-z0-9-]{0,62}$`.
- **Hostnames**: lowercase, trailing dot stripped, reject scheme/port/path/wildcard.
- **Path prefixes** (Allow-with-credential only): must start with `/`; reject
  `.`/`..` segments, percent-encoding, and empty segments (`is_safe_absolute_path`)
  — the same check is reused live at request-match time (#5 step 7), so a
  path that couldn't have been registered as a prefix also can't be matched
  against one, closing a normalization-mismatch bypass.
- **VM selector**: `{"type":"all"}` or `{"type":"only","vms":[...]}`, non-empty,
  duplicate-free, every name checked against the live `vms` table.
- **Rule action**: exactly the fields each discriminated-union variant allows,
  `allow_with_credential.credential` checked against the live `credentials`
  table, `injection.basic.username` required non-empty.
- **Uniqueness**: duplicate `priority` (Rules) and duplicate `ip_address`
  (VMs) both rejected at write time.

## DNAT / SNI-routing / selective-MITM architecture (#3, #4)

Implemented as a `mitmproxy` addon (`proxy_addon.py`) running under `mitmdump`,
Python/aiohttp/SQLite stack chosen in #4 over a Go implementation.

**Firewall (host-side, `firewall.py`, #12):** two dedicated iptables chains,
fully rebuilt (flush + repopulate, never incrementally patched) on every VM
create/delete:
- `ORCA_PROXY_NAT` (nat table, hooked from PREROUTING): `REDIRECT` each
  registered VM's 80/443 to the local proxy port.
- `ORCA_PROXY_FILTER` (filter table, hooked from FORWARD): `DROP` the same
  VM/port combinations. This is the fail-closed baseline (#12) — if the NAT
  redirect is ever missing (startup race, reconciliation failure), a
  registered VM's traffic is blocked rather than reaching the internet
  unenforced. Once a NAT rule successfully redirects a packet it takes the
  INPUT path, not FORWARD, so this DROP rule is inert for correctly-routed
  traffic.

The unprivileged Management API triggers reconciliation via a sudoers-gated
helper script (`orca-proxy-firewall-sync`) on every VM create/delete — never
running as root itself. The helper first checks both parent-chain hooks and
the complete expected per-VM rule set, rebuilding only when that state has
drifted. The Management API checks rapidly during its startup window and then
periodically thereafter. This repairs the boot race where Multipass reports
its systemd service started, then asynchronously rebuilds `mpqemubr0`'s
firewall and removes the orca-proxy hooks after the initial reconciliation.

**Connection routing (`proxy_addon.py`, `tls_clienthello` hook):** for every
incoming TLS ClientHello, resolve the VM by source IP, read SNI (if present)
and check for the ECH extension (`0xFE0D`), then call `rule_engine.evaluate_connection()`:
- No matching Rule, or the first-priority match is `allow` → `data.ignore_connection = True`
  (true passthrough — mitmproxy never terminates TLS, no CA involved).
- First-priority match is `block` → `context.client.error = "..."` kills the
  connection.
- First-priority match is `allow_with_credential` → falls through to full
  MITM interception; the actual per-request decision (see Rule matching) is
  resolved later, since a single intercepted connection carries multiple
  HTTP requests each with a different path.

Only hosts reached by an Allow-with-credential Rule are ever MITMed — this is
the "selective" half of the architecture; everything else (default-Allow,
explicit Allow, Block) never sees the Interception CA.

**Request handling (`async def request()`):** re-evaluates per HTTP request
via `rule_engine.evaluate_request()` (path varies per request even on one
intercepted connection), injects the Bearer/Basic header from
`CredentialCache.get_value()`, and synthesizes `403` (Block) or `502`
(`credential_unavailable`, #10's fail-closed contract) responses directly
rather than forwarding.

**Management API embedding:** the aiohttp app is not a separate process —
it's started inside mitmdump's own asyncio event loop via the addon's
`running()`/`done()` lifecycle hooks (`AppRunner`/`TCPSite`), so the proxy
core and Management API are one process, one event loop, sharing the same
`state.sqlite` connection.

## Rule matching / priority semantics (#5)

Implemented in `rule_engine.py`, pure and dependency-free (no I/O, no
mitmproxy/aiohttp import) so it's directly unit-testable and shared between
the proxy core and (conceptually) the logging layer.

1. Candidate rules = those whose `vm_selector` selects the connecting VM
   *and* whose `hostname` exactly matches the canonicalized incoming
   SNI/Host — no wildcard/subdomain matching.
2. Candidates are ordered by ascending `priority` — the sole tiebreaker;
   duplicate priorities are rejected at write time so this is never
   ambiguous. There is no separate specificity rule.
3. **Connection-level** (`evaluate_connection`, one-time, pre-TLS): the
   first candidate decides. `allow`/`block` are terminal and require no
   interception. `allow_with_credential` forces interception — the
   connection is now MITMed, but the actual per-request outcome (whether
   *this* request's path falls inside the rule's `path_prefix`) is
   deferred to the request level, since a Rule's mere presence in the
   candidate list is what forces interception, not yet its path match.
4. **Request-level** (`evaluate_request`, once per HTTP request on an
   intercepted connection): walks the *same* candidate list in priority
   order. `allow`/`block` are still terminal on first encounter. For
   `allow_with_credential`, the request path must be a "safe absolute path"
   (no `.`/`..`/percent-encoding/empty segments) and match the rule's
   `path_prefix` (`normalized_path == prefix` or starts with `prefix + "/"`)
   to match; otherwise evaluation **continues** to the next candidate rather
   than terminating — a lower-priority rule with an unmatched path prefix
   does not shadow a later rule for a different path prefix on the same
   host. If no candidate ever produces a terminal match, the outcome is
   `allow_default`.
5. Method matching is explicitly out of scope for v1 — Rules match on
   `(vm_selector, hostname[, path_prefix])` only, not HTTP method.
6. Every evaluation step is captured in a `trace` (`TraceEntry` list:
   `matched_terminal` / `path_no_match_continue` / `skipped_unsafe_path`)
   that's persisted with the request log row and rendered in the Web UI's
   decision inspector — so "why did this request get this outcome" is always
   answerable from a single log entry, not by re-deriving it from the live
   Rule set.

Unmatched traffic defaults to **Allow** (destination-wide standing
preference, reaffirmed by #13's ECH survey).

## Credential command execution and cache semantics (#10)

`credential_exec.py`'s `CredentialCache` — in-memory, per-Credential,
single-flight:

- Each Credential command runs as `/usr/bin/bash -lc "<command>"` with stdin
  closed, stdout piped, stderr discarded, `start_new_session=True` (own
  process group so a timeout/invalidate kill reaches descendants too), a
  **30-second timeout**.
- Output validation: reject if >16 KiB, not valid UTF-8, empty after
  stripping one trailing newline, or containing a control character.
  Non-zero exit is a separate failure category.
- Concurrent callers for the same Credential while an execution is in flight
  all `await` the same future (single-flight) rather than triggering
  parallel executions.
- `ttl_seconds > 0` caches the validated value in memory for that many
  seconds; `ttl_seconds == 0` disables caching — every call re-executes.
  (Note: this is the inverse of "no expiry" — a fixed bug during
  implementation had this backwards.)
- Failures are **never cached** — the next request always retries — and
  never expose stdout/stderr/exit text to any API caller, only one of three
  categories: `exit` | `timeout` | `invalid_output` (`CredentialExecutionError.category`).
  A failed Credential makes the proxy respond `502 credential_unavailable`
  (fail-closed — no request ever proceeds with a missing/expired injection).
- `invalidate()` (called on Credential `PUT`/`DELETE`) clears cached state
  *and* actively kills any in-flight execution via `os.killpg` — this turns
  an in-flight execution's eventual outcome into a failure rather than a
  stale success reaching an already-waiting caller.
- No refresh/test/cache-clear Management API operations exist in v1 — the
  only way to force a re-execution is to `PUT` the Credential again (which
  invalidates) or wait out the TTL.
- `GET /api/v1/credentials/{name}` exposes only safe ephemeral status —
  `status` (`empty`/`refreshing`/`valid`/`error`), `expires_at`,
  `last_success_at`, `last_failure_at`, `failure_category` — never the value,
  stdout, or stderr.

## Request logging, redaction, and retention (#11)

Separate database, `requests.sqlite`, two-level schema — because rule
evaluation (and therefore outcome) is fundamentally per-HTTP-request once
TLS is intercepted, not per-connection, but a connection row is worth
logging even when nothing is ever intercepted:

```sql
connections(id, started_at, vm_name, destination_ip, destination_port,
            destination_hostname, sni_present, ech_present, duration_ms,
            intercepted, outcome, matched_rule_json, intercepted_by_rule_json)

http_requests(id, connection_id REFERENCES connections(id) ON DELETE CASCADE,
              occurred_at, method, path, query_keys_json, status, status_origin,
              latency_ms, outcome, matched_rule_json, matched_credential,
              trace_json, headers_json)
```

- A `connections` row is written for **every** connection, intercepted or
  not — `sni_present`/`ech_present` (checked against ECH extension `0xFE0D`)
  are always recorded regardless of outcome, the buildable delta identified
  by #13's ECH survey for detecting decoy-SNI/silent-MITM-failure patterns
  later, without committing to alerting logic now (explicitly out of scope).
- `http_requests` rows exist only for intercepted connections, cascade-deleted
  with their parent connection.
- **Redaction** (`redaction.py`) is allowlist-based, not blocklist-based — an
  unrecognized header defaults to `[REDACTED]` so the credential-never-leaks
  guarantee fails safe against headers nobody thought to enumerate.
  Allowlisted: `user-agent`, `accept`, `accept-encoding`, `accept-language`,
  `content-type`, `content-length`, `host`. The one header a matched
  Credential actually injected gets a distinct placeholder —
  `[REDACTED · injected by <credential-name>]` — naming which Credential
  wrote it without ever exposing the value. Request/response **bodies are
  never logged at all**.
- Retention is a fixed row-count cap with cascade delete (oldest first) —
  no time-based retention.
- `GET /api/v1/requests` is keyset-paginated with filters including
  cross-level `decision`/`status`; `GET /api/v1/requests/{id}` returns the
  connection nested with its child `http_requests`.
- Logging is **fail-open**: `log_connection()`/`log_http_request()` catch and
  print any logging failure to stderr rather than blocking the request path —
  a broken log write must never become a denial of service on live traffic.
  This is a deliberately different posture from the firewall's fail-closed
  default (enforcement failing safe vs. observability failing available).
- Credential values, stdout, and stderr never enter this database at all —
  guaranteed structurally (the schema has no column that could hold one),
  not by a redaction pass that could be bypassed.

## CA lifecycle and VM trust installation (#8)

One ten-year self-signed root, generated once, stored in `state.sqlite`
(not a separate keystore) and materialized to
`{ORCA_PROXY_HOME}/mitm-confdir/mitmproxy-ca.pem` — that exact path and
filename matter: it's mitmproxy's own `CertStore` lookup location
(`--set confdir=...`, basename `mitmproxy`), so mitmdump loads *this* CA to
sign intercepted connections instead of silently auto-generating an
unrelated one on first run (a real bug caught in ultrareview and fixed —
every intercepted handshake failed cert validation until this path matched
exactly). `GET /api/v1/ca` returns only the
public certificate PEM, SHA-256 fingerprint, subject, and validity dates —
never the private key. The Provisioning Agent installs and verifies the
public root into each VM's system trust store at registration time (see
orca-ssh-setup integration, below); there is no rotation and no remote
per-VM cleanup in v1.

## Service installation and firewall-rule lifecycle (#12)

Blue-green deployment for the *unprivileged* half:
- `~/.local/share/orca-proxy/<version>/` — an immutable, versioned,
  `uv sync`-locked install (source copied, not symlinked, so a later working-tree
  change can't retroactively affect a running version). Owned by the target
  user, not root.
- `~/.orca-proxy/current` — a symlink, the only thing an upgrade repoints.
- systemd **user** unit (`%h`-relative paths, no root service), `orca-proxy.service`.
- Firewall reconciliation is triggered synchronously by the Management API
  on every VM create/delete (not a background poller), and once at startup
  against whatever VMs are already registered (so `/readyz` reflects reality
  immediately after a restart, not a stale "nothing to sync").
- Firewall-sync status is folded into `/readyz` (see API surface, above) —
  no separate diagnostics endpoint.
- 53/tcp+udp / 853 (DNS / DNS-over-TLS) rules were considered and explicitly
  **deferred** for v1 — traced during #13's survey to a reliability edge
  case, not a credential-leak or Block-bypass, which is the bar the
  destination sets for v1 scope.

**The privileged half is deliberately *not* blue-green** — a security fix,
not a simplification. The original design resolved
`config.firewall_sync_script_path()` through the stable `~/.orca-proxy/
current/venv/bin/...` symlink. That was a real bug: every link in that
chain (`current`, the `venv` symlink inside it, the script file itself) is
owned and writable by the same unprivileged user that ultimately gets to
run it with elevated access — trivially self-escalating (overwrite the
file, run it, no audit). A second design (sudoers `NOPASSWD` on a
fixed, root-owned copy of the script) closed that hole but kept a standing
passwordless-root grant on disk indefinitely.

The current design instead compiles `deploy/orca-proxy-firewall-sync` — a
**single, dependency-free, stdlib-only file**, not a package — with
Nuitka's `--standalone` mode, and grants it `CAP_NET_ADMIN`/`CAP_NET_RAW`
directly via `setcap cap_net_admin,cap_net_raw+eip`, installed to
`/usr/local/sbin/orca-proxy-firewall-sync` (root:root, `go-w` cleared)
alongside the libpython it links against (same directory, required by its
`$ORIGIN` RPATH). There is no sudoers entry anywhere in this design. File
capabilities are tied to the file's own inode and are honored regardless of
which user invokes it — the kernel checks the file's extended attributes at
`exec`, not the caller's UID — so the security property that matters is
unchanged from the sudoers design: the binary must live somewhere the
unprivileged service account cannot write, or that account could simply
overwrite it and inherit the capability. Compiling (rather than shipping
the raw `.py`) is required, not cosmetic — file capabilities don't attach
to a `#!`-scripted file, only to a real ELF.

`--standalone`, specifically, not Nuitka's `--onefile` mode, which was the
first thing tried and turned out to be silently broken for this use case:
onefile's bootstrap self-extracts its payload into a fresh `/tmp` directory
— owned by whichever user *invokes* the binary, i.e. the target user, on
every single run — and fork+execs the actual compiled program from there.
That extracted copy is a different file, with no file capabilities of its
own; `setcap` was only ever applied to the outer onefile binary, and
capabilities don't survive that inner fork+exec except via the *ambient*
set, which nothing in Nuitka's generic bootstrap knows to raise before
forking. Confirmed empirically during development (a throwaway onefile
build's own `/proc/self/exe`, read from inside the running program,
resolved to the extracted temp copy, not the setcap'd file): with
`--onefile`, the capability grant would never reach the process actually
running this script's Python code at all, and its
`_raise_ambient_capabilities()` call would fail — closed, silently — for
real, in production, not just under a plain `pytest` invocation. Switching
to `--standalone` removes the extra hop entirely: the compiled binary is
what's directly exec'd (same check — its own `/proc/self/exe` matches the
installed path), so the file `setcap` targets is the same file whose code
runs.

The helper carries its own copy of the small amount of logic it actually
needs (`build_commands`, `reconcile`, a two-line sqlite connect/query)
rather than importing `orca_proxy.db`/`orca_proxy.firewall`/
`orca_proxy.repo.vms` — those live in the target user's venv, and importing
from there would just reintroduce the same writable-dependency problem one
hop away. The duplication cost is deliberately accepted: a handful of
small, stable functions copied once is cheaper than a dependency on
anything mutable by the account that invokes the binary. `firewall.py` (in
the regular package) keeps only the unprivileged half —
`FirewallSync`/`run_reconcile_script`, which runs this binary directly (no
`sudo` prefix) and never runs `build_commands`/`reconcile` itself.

One real cost of moving off sudoers: a `setcap`'d binary's capabilities
land in its own effective/permitted set on `exec`, but do **not**
automatically propagate to a plain child process it execs (`iptables`
itself carries no file capabilities) — only the *ambient* capability set
survives that hop, and file capabilities clear the ambient set by default.
`orca-proxy-firewall-sync` explicitly raises `CAP_NET_ADMIN`/`CAP_NET_RAW`
into its own ambient set (`_raise_ambient_capabilities()`, via
`prctl(PR_CAP_AMBIENT_RAISE)`) once at startup, before the first `iptables`
call, to make that inheritance happen; this is best-effort and non-fatal —
if it fails (e.g. running the source file directly rather than the
`setcap`'d compiled binary), the subsequent `iptables` calls simply fail
with their own non-zero exit, which `reconcile()`/`is_in_sync()` already
turn into the normal fail-closed "error" status.

There is also no more argument-pinning. The sudoers entry used to pin the
exact `--db`/`--bridge`/`--proxy-port` values a `NOPASSWD: /path/to/cmd`
grant would otherwise let through unpinned — a setcap'd binary has no
equivalent mechanism; whoever can execute it chooses its arguments. The
`_BRIDGE_NAME_RE` validation inside the helper (see its own comment) is
now the *only* thing standing between an attacker-controlled `--bridge`
value and a shell-injection-to-capability primitive, not a second layer
behind sudoers pinning.

Consequence: upgrading now genuinely needs a fresh `sudo bash install.sh`
run — there is no passwordless path left that could re-provision the
privileged half on its own, by design. `install.sh` itself must run as
root (`curl .../install.sh | sudo bash`, or `sudo bash deploy/install.sh`
against a local checkout); it still builds the venv and manages the
systemd **user** unit as the target user (`SUDO_USER`, or `ORCA_PROXY_USER`
to override), only elevating for the steps that actually need root:
compiling and installing the firewall-sync binary and applying `setcap` to
it. Unlike the old design, `ORCA_PROXY_BRIDGE`/`ORCA_PROXY_PORT` are read
live by the running service (`config.py`) and passed as plain `argv` to
the helper on every reconcile — there is nothing install-time to keep in
sync, and nothing that goes stale if they're changed without a reinstall.

## Web UI (#7, #15)

Dependency-free vanilla JS/HTML/CSS, no build step, no framework — evolving
the accepted Variant A prototype (entity-console layout: left nav, dense
tables, contextual inspector, editor drawer) directly rather than a rewrite.
Served as static files by the same aiohttp process as the Management API
(`app.py`'s `add_static`), not a separate frontend server.

- Views: VMs, Credentials, Rules, Logs — table + inspector per entity, an
  editor drawer for Rule/Credential/VM writes with inline field-error
  rendering sourced directly from the API's `fields` error envelope.
- **Quick Add**: a single source-of-truth JSON file,
  `static/quick-add-catalog.json`, loaded both by the frontend's own
  `fetch()` and by a Python compatibility test
  (`tests/test_quick_add_catalog.py`) — not a new API endpoint, and not
  duplicated data. Contains the three known-working Credential command
  templates: `gh auth token` (GitHub), and pure-bash (curl + jq)
  implementations of the OAuth-refresh logic for Claude/Codex (wire format
  below). A Credential command is a plain bash string handed to `bash -lc`
  (`credential_exec.py`) — it must never depend on anything beyond what a
  bare VM/host shell already has (curl, jq), so there is no packaged
  console-script or venv `bin/`-on-`PATH` dependency to keep working across
  installs/upgrades. `tests/test_refresh_scripts.py` runs the catalog's
  actual command strings (loaded from the same JSON, not a duplicated copy)
  through the real `CredentialCache` execution path against a stubbed
  `curl`, so the catalog itself — the thing that actually ships — is what's
  under test.
- No URL-based routing — in-memory view state, matching the prototype.
- Logs view refresh is a manual button, not polling.

## OAuth refresh wire format (#2)

Confirmed from CLIProxyAPI's open-source implementation (reference only —
**no runtime dependency** on it, per the destination's standing preference):

- **Claude Code** → `POST platform.claude.com/v1/oauth/token`, JSON body,
  Axios-mimicking headers, `client_id = 9d1c250a-e61b-44d9-88ed-5944d1962f5e`.
- **Codex** → `POST auth.openai.com/oauth/token`, form-urlencoded,
  `client_id = app_EMoamEEZ73f0CkXaXp7hrann`. Codex refresh tokens are
  **single-use** — the credential file must be rewritten on every refresh,
  not just on failure.
- Access-token TTL is not assumed from source — always read live from each
  response's `expires_in`.

Both Quick Add commands print only the access token to stdout on success
(the Credential Value) and nothing on failure (`set -euo pipefail` plus an
explicit exit on any missing/invalid response field — so a failed refresh
becomes `CredentialExecutionError`'s `exit` category, not a false success),
rewriting only the confirmed/plausibly-inferred keys in each tool's local
credentials file via `jq` and preserving everything else, including the
file's original permission bits.

## Claude Code / Codex CLI placeholder-auth mechanism (#16, #17)

- **Claude Code** always calls `api.anthropic.com/v1/messages`.
  `ANTHROPIC_AUTH_TOKEN=placeholder` is the native bearer-bypass mechanism;
  `ANTHROPIC_BASE_URL` is deliberately left **unset** — an explicit override
  disables Remote Control and MCP tool search per Anthropic's own docs, so
  leaving it unset is strictly better, not just simpler.
- **Codex** is bimodal by auth mode: API-key mode calls
  `api.openai.com/v1/responses`; ChatGPT-plan mode — the mode the
  `codex-subscription` Credential targets, and the one already proven
  working in the *current* `orca-ssh-setup` skill via CLIProxyAPI's
  `-codex-login` — calls `chatgpt.com/backend-api/codex/responses`. Reached
  via the same mechanism already proven in production by this skill: a
  `[model_providers.hostproxy]` custom-provider `config.toml` block
  (`base_url` + `env_key`-sourced placeholder bearer token), which bypasses
  `AuthMode`/`auth.json` resolution entirely regardless of auth mode. The new
  design just repoints `base_url` from CLIProxyAPI's broker to the real host
  — considered and explicitly closed as *not* needing a separate
  architectural fork (#17).

## ECH and missing-SNI containment (#13)

Given the destination's fixed default-Allow-unmatched, none of
ECH / no-SNI / direct-IP / custom-DNS / DoH can become a hard Block without
contradicting that standing preference. ECH's own accept-confirmation
mechanism makes silent MITM cryptographically impossible even if attempted —
so the residual risk is an Inject rule silently *not firing* against a
decoy SNI, not a spoofed injection. The buildable delta actually shipped:
log `sni_present`/`ech_present` on every connection row (feeds #11's
schema). DoH and direct-IP connections need no bespoke handling beyond what
the DNAT/SNI-routing design already does. Alerting on these signals (e.g.
detecting a host's traffic silently dropping to zero under decoy-SNI ECH) is
explicitly out of scope — a monitoring feature, not part of this spec.

## orca-ssh-setup integration contract (#14)

The Provisioning Agent (`orca-ssh-setup` skill) is a first-class Management
API caller, not a human using the Web UI. Full registration sequence,
implemented in `orca-ssh-setup/SKILL.md` steps 3 and 6, each step idempotent
and failing loud rather than proceeding past an unconfirmed state:

1. Check for an existing orca-proxy install (`systemctl --user status`) —
   it's shared across every project VM on the host, install only if absent.
2. Register needed Credentials via `PUT` (never gated behind Web UI Quick
   Add — that's a human convenience over the same API, not a separate
   mechanism), reading command/TTL from the same `quick-add-catalog.json`
   the Web UI uses.
3. Register the VM (`PUT /api/v1/vms/{name}`) — this alone triggers firewall
   reconciliation.
4. Poll `/readyz` until this VM's `firewall_status` entry is `in_sync`
   before doing anything else — no safe way to proceed with Rules while
   enforcement is unconfirmed.
5. Install and verify the Interception CA into the VM's trust store.
6. Create Rules scoped to exactly what was confirmed necessary.
7. Configure each harness/tool with **native placeholder-auth** — no
   explicit proxy config, no base-URL overrides, no wrapper scripts, no git
   `.proxy` config — since DNAT already forces the real traffic through
   transparently and the matching Rule unconditionally overwrites whatever
   placeholder credential each tool sends.
8. Smoke-test both the allowed and default-Allow-unmatched paths — corrected
   from the old gh-proxy's semantics: an unmatched path is no longer a
   proxy-generated `403`, it passes through to the real upstream.

## Not yet specified

- VM/Rule/Credential decommissioning — deregistering a VM or retiring its
  Rules once a project is done. Neither the destination nor any ticket has
  asked for this yet; revisit if it becomes a real need.

## Out of scope

- Migrating VMs already provisioned under the old two-proxy scheme.
- Persisting per-VM CA-fingerprint acknowledgement / blocking
  Allow-with-credential on a mismatch — provisioning-time trust verification
  is sufficient for v1; runtime attestation is a possible later enhancement.
- Alerting/anomaly-detection on `sni_present`/`ech_present` signals.
