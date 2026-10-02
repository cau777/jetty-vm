# Multipass to Incus migration plan

Date: 2026-09-09. Status: proposed plan; no VMs or proxy services have been migrated.

This restores the earlier plan from this conversation. Its proposed architecture keeps orca-proxy and host credentials on the host, and uses an Incus gateway VM to send governed traffic into mitmproxy's userspace WireGuard mode. It is a hypothesis to prototype, not an established Incus feature or a promise that the current locked proxy supports this topology without changes. The repository now contains separate, newer Incus egress research; this restored document preserves the assumptions of the earlier plan.

## Goal and current baseline

Replace Multipass as the VM launcher while retaining Jetty's current workflow and every required orca-proxy capability. Remove proxy-owned host iptables updates. Keep the host as the only location for provider credentials, Credential execution, the interception CA private key, proxy databases, policy API, and UI.

At the time of the original plan, the repository described a single host service shared across persistent project VMs. `orca-proxy` transparently redirects registered VM IPv4 TCP ports 80 and 443, inspects TLS SNI, applies exact-host and path-scoped rules, selectively intercepts TLS for credential injection, and defaults unmatched traffic to passthrough. The VM runs an agent with sudo. Provisioning also supports SSH, Orca remote execution, harness CLIs, git and gh access, and project data.

The existing behavior is not equivalent to “all IP traffic is proxied.” The current firewall helper matches IPv4 TCP 80/443. IPv6, UDP/QUIC, DNS/DoT, SSH, alternate ports, and other protocols have separate behavior. Preserve required connectivity and state these limits accurately.

## Proposed topology

```text
Agent VM (one private NIC)
        │
        ▼
Private Incus network (no direct external route)
        │
        ▼
Gateway VM
   ├── agent IPv4 TCP 80/443 → userspace WireGuard tunnel → host orca-proxy
   └── other currently permitted traffic → ordinary gateway routing

Host operator → loopback Management API / Web UI
Host credentials + CA key + proxy databases remain on the host
```

The project VMs have one private NIC. The host does not route that network directly to the Internet. A trusted gateway VM has private and uplink NICs and is the sole egress route for agent VMs. Route the governed TCP 80/443 traffic from the gateway through a WireGuard tunnel to the host's unprivileged mitmproxy listener; the host proxy retains transparent TLS inspection and the current addon. Route other traffic according to its current behavior.

This plan keeps arbitrary Credential commands on the host. The Quick Add refresh commands read and update host CLI authentication files; moving the proxy into the gateway, mounting host secrets there, or exposing credential values over an RPC would change the security model and may break user-defined commands.

Incus still manages host networking and is a privileged system daemon. The goal here is for the application proxy to stop managing host iptables, not to make VM management itself rootless.

## Transport prototype gate

The repository lockfile at the time of this plan pinned mitmproxy 10.4.2 and mitmproxy-rs 0.6.3. The pinned mitmproxy version documents a userspace WireGuard mode, which routes WireGuard streams into its transparent proxy layer. That makes reuse of the current addon plausible; it does not prove that the gateway can route traffic from multiple guest source addresses through one peer with source and destination identity intact. [mitmproxy 10.4.2 WireGuard mode](https://github.com/mitmproxy/mitmproxy/blob/v10.4.2/docs/src/content/concepts-modes.md#wireguard-transparent-proxy), [versioned mode server source](https://github.com/mitmproxy/mitmproxy/blob/v10.4.2/mitmproxy/proxy/mode_servers.py).

Before changing production provisioning, build an isolated prototype with two agent VMs, a gateway, a test proxy using synthetic credentials and a test CA, and controlled upstream servers. Prove all of the following:

1. The gateway sends packets from both guest addresses through one tunnel without SNAT of the inner packets; the pinned userspace server accepts those routed sources and returns traffic to the correct guest.
2. mitmproxy sees the actual guest source address for VM lookup and the original destination IP and port for transparent forwarding. A gateway address shared by all requests is not enough for current VM selectors or connection logs.
3. Existing Allow, Block, Allow-with-credential, unmatched, and default-Allow decisions behave the same. Allow remains byte-level TLS passthrough; only credential rules cause MITM.
4. The current Interception CA validates intercepted flows, and no new CA is generated. Upstream sees credentials only on the matching VM, hostname, and path.
5. Test missing SNI, ECH visibility, direct-IP traffic, spoofed HTTP Host, multiple requests on one connection, HTTP/2, WebSockets, long streams, and the workloads actually used.
6. Stopping the proxy, tunnel, or gateway, restarting Incus, and rebooting the host do not create a direct TCP 80/443 escape route.
7. Measure MTU/fragmentation, latency, throughput, connection count, concurrent VMs, DNS dependency, VPN/internal destinations, and tunnel-key/log handling.

The generated WireGuard configuration is an endpoint example, not a ready-made gateway router configuration. Add gateway policy routing for the selected traffic without routing the tunnel's own outer packets back into the tunnel. Prevent host-proxy logs from recording generated client private keys. Keep tunnel keys out of VM images, agent access, the Management API, and request logs.

If the pinned userspace tunnel cannot preserve identity, destinations, selective TLS behavior, and fail-closed recovery, stop this design and evaluate another transport or a supported mitmproxy upgrade before migrating VMs. Do not quietly substitute environment proxy variables or copy credentials into guests.

## Capability preservation matrix

Treat source code and tests as the shipped baseline when prose differs. Tests of addon hooks alone do not establish real network parity.

| Area | Required behavior after migration |
| --- | --- |
| VM identity | Preserve immutable logical VM names and unique addresses. Bind trusted Incus instance/NIC identity to policy records. Preserve each original guest source address across the tunnel. |
| Rule matching | Keep exact normalized hostname matching, VM-specific and wildcard selectors, unique ascending priorities, terminal Allow/Block, and current path-prefix behavior. |
| Default Allow | Unmatched connections pass through untouched. Explicit Allow also passes through untouched. Neither path requires CA trust or injects credentials. |
| Selective MITM | Only a matching Allow-with-credential connection is MITMed. Keep SNI bound to the intercepted TLS connection; do not trust an attacker-controlled Host header to select another rule/upstream. |
| Path decisions | Re-evaluate each request; preserve segment-aware path prefixes, `/`, unsafe-path behavior, later-rule continuation, and per-request outcome. |
| Credential injection | Preserve Bearer and Basic Authorization injection, configured username, host-side arbitrary bash Credential commands, output validation, single-flight execution, timeout, TTL behavior (zero disables cache), failure retry, and in-flight invalidation. Failed injection still fails closed with the current 502 behavior. |
| Host credentials | Keep Quick Add command catalog and GitHub, Claude, and Codex refresh behavior on the host. Never copy access/refresh tokens into the gateway or agent VM. User-created Credential commands must keep the host execution context they rely on. |
| CA lifecycle | Reuse the current host CA and fingerprint. Keep its private key on the host, materialize it at mitmproxy's expected confdir path, and provision only the public root into agent VMs. |
| Management API/UI | Preserve loopback-only unauthenticated host API, CRUD semantics, validation/error shapes, protected deletes, Web UI editors, Quick Add, and same-origin serving. Do not expose management routes to the gateway or VMs. |
| Logs | Preserve connection and HTTP request records, snapshots, trace, filters, pagination, retention, fail-open logging, redaction and no body capture. VM and destination attribution must remain accurate through the tunnel. |
| ECH and missing SNI | Preserve current flags/logs and default-Allow behavior. Do not claim that hostname rules block ECH, missing-SNI or direct-IP traffic under the existing policy. |
| Health | Keep `/readyz` and meaningful per-VM status. Report transport, gateway route, trusted identity, CA, and active enforcement health. Do not report ready merely because the proxy process is running. |
| Agent workflow | Preserve persistent one-VM-per-project use, SSH/key-based access, normal sudo-capable guest user, Orca remote terminals (`node-pty` toolchain), project clones and any host mounts, requested harnesses, placeholder auth, GitHub operations and existing REST-only watcher. |
| Deployment | Keep pinned release installs, current databases and logs, service startup, upgrade/rollback, and shared-host proxy behavior. The proxy's Incus path must not invoke host sudo or modify host firewall state. |

### Existing gaps that need characterization

- The current redirect covers IPv4 TCP 80/443 only. It does not establish all-protocol proxying.
- Port 80 is redirected, but the addon initializes VM identity and connection logging in `tls_clienthello`. Characterize plaintext HTTP policy, VM attribution, logging and credential behavior before asserting full parity.
- Unknown peer addresses can fall through to an empty VM name, which can match a wildcard selector. The new ingress must reject unknown identities before credential evaluation.
- “Every connection is logged” and fail-open logging claims in design prose need to be checked against actual hook coverage, especially non-TLS and disconnect paths.
- Rules do not match HTTP method. Repository/setup wording about read-only versus write GitHub operations must not be interpreted as a method-level restriction.
- DNS, UDP/QUIC, IPv6, SSH and alternate ports require an explicit compatibility inventory; do not accidentally imply they are all governed by the current proxy.

## Incus network and privilege layout

Create a dedicated agent profile that contains exactly one private NIC and an explicit root disk. Do not inherit an external NAT NIC. Set IPv4 routing/NAT off on the private network and advertise the gateway as the guest default route. Configure IPv6 explicitly; for an IPv4-only first milestone, disable external IPv6 at the network boundary and verify the guest cannot re-enable an alternate path. If a required project depends on IPv6, support and test it before cutting that project over. [Incus bridge configuration](https://linuxcontainers.org/incus/docs/main/reference/network_bridge/).

The gateway has a private NIC and a separate Incus-managed uplink. Use a stable host address reachable by the gateway for the proxy's UDP tunnel listener. No public port forwarding is needed. Bind each agent NIC to its allocated MAC/IP using Incus NIC filtering. Consider port isolation for agent-to-agent traffic only after checking whether workspaces currently require peer communication; the gateway must remain able to communicate with the agent ports. Forwarding gateway NICs need separate source-filter treatment because response packets can legitimately carry upstream addresses. [Incus NIC controls](https://linuxcontainers.org/incus/docs/main/reference/devices_nic/), [Incus security guidance](https://linuxcontainers.org/incus/docs/main/explanation/security/).

The gateway policy must route agent IPv4 TCP 80/443 into the tunnel and independently deny those flows on the direct uplink. A missing tunnel route must fail closed rather than use the normal gateway route. Keep other forwarded traffic limited to what the current environment permits. Do not expose an alternate SOCKS/HTTP proxy, agent-accessible SSH tunnel, or gateway-management listener to guests.

Preserve host-initiated SSH to guests. Restrict access from agents to host-local services, Incus control APIs, gateway management and proxy control ports while allowing required DHCP/DNS and approved internal services. Test loopback, link-local, private/VPN and DNS paths explicitly. Current allowance for SSH/UDP also permits their use as tunnels; this plan does not turn Jetty into a general anti-exfiltration sandbox.

Use a trusted lifecycle component for Incus instance creation, NIC binding, gateway setup, and topology verification. Do not give the proxy access to the Incus administrative socket or `incus-admin` group as a replacement for firewall sudo. The proxy should own policy data and health, while a separately authorized component owns privileged VM/network lifecycle. Host installation and final legacy-rule removal remain explicit operator actions.

## Resource allocation

### Storage

Start with an Incus `dir` pool on an existing suitable host filesystem. Incus VM block files are sparse raw files, so guest-visible capacity need not reserve the same amount of host disk. The directory driver is easy to set up but copies and snapshots are slower because operations are not optimized. Avoid introducing a new partition or fixed-capacity pool just to migrate VMs. [Directory driver](https://linuxcontainers.org/incus/docs/main/reference/storage_dir/), [sparse-file implementation](https://github.com/lxc/incus/blob/main/internal/server/storage/drivers/utils.go).

| Backend | Physical allocation and implications | Position |
| --- | --- | --- |
| `dir` | Sparse raw VM files grow with written blocks; images and copies still occupy physical space. Copies/snapshots can be slower. | Initial choice on a suitable existing ext4/XFS filesystem. |
| LVM thin | Volumes allocate from a shared thin pool, while the pool itself dedicates backing capacity unavailable to ordinary host files. | Use if a suitable pool already exists and its ceiling is accounted for. |
| ZFS | Sparse zvols may avoid full reservation; snapshots/clones retain shared blocks and ZFS uses memory. Verify the option names and actual allocation for the installed version. | Suitable if already operated and measured. |
| Btrfs | Incus upstream advises against VM pools because raw images interact poorly with qgroup extent accounting and snapshots. | Do not select by default. |

Keep each existing guest's logical disk capacity initially; inventory actual sizes instead of assuming the skill's 40 GB default. Explain guest-visible capacity, guest filesystem usage, allocated host bytes, and free host/pool capacity as separate numbers. Sparse allocation does not mean the host cannot run out of space.

Measure physical allocation by launching a large logical disk from a small image, writing known incompressible data, trimming it, and repeating with snapshots and clones. Record whether deletion or guest `fstrim` returns blocks on the chosen filesystem/pool. Never infer physical use from `ls -lh` apparent size or guest `df` alone. Include image caches, logs, snapshots, exports and temporary copies.

Plan migration peak space as new images + replacement VM data + gateway image/disk + export/conversion staging + retained Multipass VMs + snapshots/clones + logs/WAL + operating headroom. Keep old Multipass disks until rollback is no longer needed; they still consume space. Do not assume cloning shares extents on `dir`.

### CPU and RAM

Preserve actual per-workspace vCPU and RAM settings. The provisioning skill proposes 4 CPUs and 8 GB RAM, but inventory existing VMs and their workloads. Incus's integer CPU setting is a vCPU count, not an exclusive physical-core reservation. Admit running and restarting VMs against host RAM plus measured guest, hypervisor, gateway, proxy and storage overhead. Sparse disk behavior says nothing about safe RAM overcommit. Incus memory decreases may depend on ballooning and may not immediately reach the target. [Incus instance resource options](https://linuxcontainers.org/incus/docs/main/reference/instance_options/).

For one shared gateway that routes and tunnels traffic while TLS policy stays on the host, use **1 vCPU, 512 MiB–1 GiB RAM, and 4–8 GiB logical sparse root disk** as a prototype starting point only. These figures are unmeasured estimates, not supported minimums. Check image size and measure throughput, latency, connection count, CPU, RSS, recovery time, and disk/log growth under concurrent builds and streams.

Stopped agents release active CPU/RAM demand but retain disk. The gateway is a shared failure and capacity point: stopping it affects every attached workspace. Test restarts and admission controls before setting product defaults.

## Migration phases

### Phase 0: inventory and recoverability

Record Multipass and host versions, backend, host filesystem/free space, and for each VM: logical and allocated disk, CPU/RAM, image, mounts, users, SSH keys/host keys, toolchains, project state, uncommitted/untracked files, IPv6/inter-VM needs, and VPN/internal destinations. The intended model is one long-lived VM per project, reused for worktrees; keep it that way.

Record proxy VM mappings, Rules/priorities, Credentials by name/TTL (never values), CA fingerprint, service config, API/UI state and log retention. Back up both SQLite databases consistently: stop the proxy before file copying or use SQLite's backup API because WAL mode can place committed state in sidecar files. Store the CA backup privately. Do not put provider authentication files in VM or gateway backups.

Deliverables: inventory, parity fixtures, free-space budget, and a tested recovery procedure. No cutover yet.

### Phase 1: prototype transport and disk

Complete the transport proof above using the exact locked mitmproxy release. Separately run the sparse allocation/reclaim experiment on the selected pool and filesystem. Pin supported Incus, image and QEMU versions. Verify virtualization, guest-agent and NIC security feature availability on the actual host.

Deliverables: transport results, measured gateway sizing, disk allocation and reclaim results, and a supported version matrix. If the design fails, stop before changing the production path.

### Phase 2: provider-aware control plane

Add trusted backend binding metadata separate from the logical VM name and Rule selectors: provider (`multipass` or `incus`), provider instance ID, NIC identity/address, and desired/observed enforcement generation. Keep Rule references and logical names stable. Make any API addition backward-compatible; old Multipass clients continue working, and an IP-only update must not reset a provider binding.

The current privileged helper reads every row from the VM table. During coexistence, explicitly filter it to Multipass bindings before adding Incus records, avoiding accidental rules against the new subnet. Prevent subnet overlap and simultaneous duplicate addresses.

Keep one host proxy/cache/Management API so real host Credential files are not refreshed by duplicate processes. Add provider-specific enforcement health: preserve `/readyz` and per-VM status as a compatibility surface while reporting tunnel, gateway route, source identity, and topology status. A registration write alone must not mean “enforced.” Missing or stale health remains unready.

### Phase 3: reproducible Incus setup

Create versioned network, profile, gateway and lifecycle configuration. Provision the gateway from a pinned image/configuration; do not treat it as a manually maintained user VM. Install the tunnel client key through trusted provisioning, suppress key-bearing generated logs, and restrict gateway administration.

Create agent instances quarantined, verify profile and NICs, configure the stable address/source binding, install only the public CA, establish the trusted SSH user and key, then install tools. Activate external routing only after gateway route, tunnel, source attribution, policy registration and CA checks pass. Route package/bootstrap web requests through enforcement. Create rules before cloning when repository access is required.

Incus command execution often runs as root. Explicitly create/select a normal sudo-capable SSH user with correct home ownership rather than assuming Multipass's `ubuntu` user. Preserve the C/C++ build tools required for Orca remote `node-pty` terminals. Preserve clone and host-mount workflows; validate mount permissions/performance or document an explicit migration for affected projects.

Keep native placeholder authentication, harness destinations, git credential helper, GitHub environment, REST watcher, and repository/path policy behavior as described by the current repo. Recheck current harness versions and integration behavior during implementation; the launcher change should not silently rewrite auth flows.

### Phase 4: canary and project cutover

Recreate a low-risk project from a pinned image and provisioning recipe. Prefer a fresh OS plus explicit project/home data migration over assuming Multipass qcow2 is directly importable as a complete VM.

Inventory and preserve uncommitted/untracked and ignored data, worktrees, permissions, symlinks, ownership, application state and required mounts. Use application-consistent backups for databases. Do an initial copy, stop writers, then final synchronization. If full-disk import is required, separately prove sparse preservation, firmware/boot mode, drivers, cloud-init identity, guest agent, disk growth and SSH access. Keep originals intact.

For cutover: stop agent work and old-VM writers; take the final data copy; stop and retain the old VM; keep the replacement quarantined; switch trusted provider/address binding; verify readiness and the full parity checks; update Orca SSH host/key details using verified fingerprints; then resume. Do not disable host-key checking or broadly erase known_hosts. Migrate projects one at a time and recheck RAM/free disk before each one.

### Phase 5: retire Multipass path

After every project passes acceptance and the rollback retention period ends, disable the Multipass adapter and remove its maintenance loop. Then remove only the exact legacy chain hooks/chains, root-owned helper and matching sudoers entry. This cleanup is privileged and must never flush the host firewall. Verify backups and replacement data before deleting each old VM; tell the operator what was removed and its recovery route. Retire Multipass itself only if no unrelated VMs use it.

Update README, design, install/rollback scripts, provisioning skill, release manifest, domain vocabulary, and versioned packaging. Ordinary proxy upgrades must not reinstall or manage host firewall rules.

## Acceptance checklist

Run current locked proxy tests plus integration tests against real VM, gateway, tunnel and controlled upstreams. Hook-mock tests alone do not establish parity.

**Policy and proxy:**

- [ ] Two VMs with different Rules are attributed correctly during concurrent traffic.
- [ ] Allow/unmatched TLS remains upstream passthrough; Block never gets a MITM cert; injection uses the original CA and applies only at matching host/path.
- [ ] Exact hostname, priority, VM selector, path boundary, unsafe path, rule continuation, SNI/Host mismatch, and multi-request connection behavior match.
- [ ] Plain HTTP behavior is characterized and any required identity, policy, injection and logging gaps are resolved.
- [ ] Credential TTL, TTL zero, concurrency, refresh, invalidation, timeout, output errors, failure status and host Quick Add command behavior match.
- [ ] Host authentication sessions survive VM recreation; no credential value or CA private key appears in gateway/agent data, logs, images, or mounts.
- [ ] API, UI, validation/errors, Rules/Credentials lifecycle, logs, filters, pagination, retention, redaction and health remain available.
- [ ] Actual requested harness, git/gh REST flows, package downloads, long streams and required private/VPN services pass.

**Isolation and failure:**

- [ ] VM has only the intended private NIC, expected reserved source identity and trusted binding.
- [ ] Guest root cannot change routes or spoof another VM to escape enforcement or obtain its policy.
- [ ] Unknown and deregistered sources cannot match wildcard credential Rules.
- [ ] Stopping the proxy, tunnel, gateway, Incus or host cannot create direct TCP 80/443 fallback, including for existing flows.
- [ ] Readiness detects missing/stale enforcement before allowing work; recovery validates state before re-enabling it.
- [ ] IPv6, DNS, UDP/QUIC, alternate ports, SSH and peer VM access have explicit compatibility results.
- [ ] Host SSH and Orca remote terminals work after normal stop/start; guest user, ownership and mounts are correct.
- [ ] Incus-only proxy CRUD/restarts run no `sudo`, iptables/nft commands, or Incus administrative API.

**Resources:**

- [ ] Guest capacity, apparent size, allocated host blocks, filesystem/pool usage, and free space are measured separately.
- [ ] Initial sparse disk allocation is image/metadata-sized; incompressible writes grow allocated blocks as expected.
- [ ] Guest trim, snapshots, snapshot deletion, clones and reclaim have measured behavior.
- [ ] Import/export/copy staging does not expand sparse holes unexpectedly; migration peak fits while old VMs remain.
- [ ] Disk growth and CPU/RAM sizing preserve data; do not test shrink on production disks.
- [ ] Concurrent workload memory, gateway overhead and new/restarted VM admission fit the host budget.
- [ ] Test exhaustion only in a bounded disposable pool, never by filling the workstation filesystem.

## Rollback

Keep old VMs stopped, their disks intact, and consistent proxy database/CA backups until the acceptance period ends. Rollback is a planned outage, not live migration.

On failure, quarantine and stop the replacement. Preserve any new user data and reconcile it to the old VM before writers resume. Restore the old provider/address binding and verified Multipass firewall enforcement, confirm the proxy CA/state and `/readyz`, update SSH details safely, then restart the original VM.

Keep schema changes backward-compatible during coexistence where possible. If an older proxy cannot read a migrated DB, restore a matching consistent snapshot and account for policy/log changes since backup; changing the software symlink alone is not enough. Keep live host OAuth files intact and restart with an empty in-memory Credential cache; do not roll refresh tokens back.

After removing the legacy helper/rules, rollback first requires reinstalling the verified legacy enforcement path before starting old VMs. Document that boundary before cleanup.

## Resource findings reference

The accompanying [Incus resource notes](incus-resource-notes.md) gives the detailed backend comparison, primary references and measurement method. Gateway resource values in this plan are starting estimates only. Record supported versions, measured disk/RAM/CPU results, parity evidence, known limits, rollback retention dates, and acceptance results before declaring the migration complete.
