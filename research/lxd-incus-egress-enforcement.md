Research for Jetty: can LXD or Incus enforce VM egress through the host-side orca-proxy using only built-in features, with no Jetty-written host firewall rules and no capability-bearing helper?

**Date:** 2026-10-02
**Method:** Primary sources only. `canonical/lxd` (main, commit `fcd4c6813eba8bfd69d5d0be27a69070770391b9`, 2026-10-02, `shared/version/flex.go` says `6.9`) and `lxc/incus` (main, commit `3e3c2edb0799cebedf992534a215ec7b79f4179c`, 2026-10-02, `internal/version/flex.go` says `7.5.1`) were cloned with `--depth 1` into the scratchpad. Their in-repo docs (`doc/`, the source of documentation.ubuntu.com/lxd and linuxcontainers.org/incus/docs) and Go source were read directly. In this note, `lxd/...` paths are relative to the LXD repo root and `incus/...` paths to the Incus repo root. `openai/codex` (commit `84d5437b6e558f5159551949a35aedb84ddb89be`) was also cloned for one proxy question. Release facts come from the official announcements on discuss.linuxcontainers.org. Node, npm, git and pip docs were read from their upstream doc sources. apt docs are the local man pages (apt 3.2.0). Every claim is followed by its source. Claims that rest only on my reading of source, or that I could not confirm, are marked **unverified** or **inference**.

---

## TL;DR

- **Neither LXD nor Incus can transparently redirect a VM's traffic to a proxy as a built-in feature.** ACL actions are only `allow`/`reject`/`drop` (Incus adds `allow-stateless`, but only on OVN). `proxy` devices and network forwards handle inbound traffic only (§1.6, §3).
- **Both can do "explicit proxy plus egress deny" with no Jetty-written firewall rules and no privileged helper.** The pieces are an admin-created bridge, an ACL on that bridge whose only egress allow is `<bridge-ip>:<proxy-port>`, `security.ipv4_filtering`/`ipv6_filtering` on each VM NIC, and a restricted project. The daemon writes and owns these nftables rules in its own `inet incus`/`inet lxd` and `bridge incus`/`bridge lxd` tables. A guest root user cannot change any of this. A restricted API identity cannot touch the network-level ACL (on Incus, per source).
- **Incus is the better fit.** It supports ACLs directly on bridged NICs; LXD supports them only on OVN NICs. It also has address sets, is nftables-only, and its restricted identities cannot edit default-project networks. LXD's TLS authorizer appears to let them (§4.3, **unverified**).
- **There is one real gap.** On every daemon start or network update, the network-level ACL chains are deleted and rebuilt while running VMs keep running. NIC-level filter chains (Incus) are not touched by that rebuild, so this design puts the egress ACL in both places (§1.4).

---

## 1. Network ACLs

**Short answer:** ACLs are daemon-managed nftables rules (LXD can also fall back to xtables). They attach to a whole network through `security.acls`, or, on Incus, also to a single bridged NIC. They apply to VMs exactly as to containers. They can express "allow only `<bridge-ip>:<port>` (+ DHCP/DNS, which are always allowed), drop everything else". They cannot redirect. The daemon applies them at startup before instances autostart, so the Multipass-style boot race does not exist. The reconcile loop is replaced by a different and smaller gap: the network-level chains are torn down and rebuilt whenever the network is re-set-up (§1.4).

### 1.1 Rule shape

- Each rule has `action`, `description`, `source`, `destination`, `destination_port`, `source_port`, `icmp_type`, `icmp_code`, `protocol` (`icmp4|icmp6|tcp|udp`) and `state` (`enabled|disabled|logged`). Source and destination take comma-separated CIDRs, IP ranges or selectors; empty matches anything. Ports apply only to tcp/udp. Sources: `lxd/doc/howto/network_acls.md` lines 280–289, 317–323; `incus/doc/howto/network_acls.md` lines 91–102.
- Valid actions:
  - LXD: `ValidActions = []string{"allow", "drop", "reject"}` (`lxd/lxd/network/acl/driver_common.go` lines 52–53, enforced at 276–277).
  - Incus: `ValidActions = []string{"allow", "allow-stateless", "drop", "reject"}` (`incus/internal/server/network/acl/driver_common.go` lines 59–60). `allow-stateless` is rejected for any non-OVN use: "Action %q is only supported on OVN networks" (`incus/internal/server/network/acl/acl_firewall.go` lines 27–33; doc `incus/doc/howto/network_acls.md` line 93).
- Ordering is fixed by action, not list position: `drop`, then `reject`, then `allow`, then the default action (default `reject`). The first match wins across all ACLs on a NIC (`lxd/doc/howto/network_acls.md` lines 419–428; `incus/doc/howto/network_acls.md` lines 70–84).
- Selectors (`@internal`, `@external`, `@net/peer`, and ACL-name groups) are **OVN only** in both projects: "This feature is supported only for the OVN NIC type and the OVN network" (`lxd/doc/howto/network_acls.md` lines 441–449, 464–469; `incus/doc/howto/network_acls.md` lines 104–113, 141–151). On a bridge they are explicitly unsupported (`lxd/doc/howto/network_acls.md` line 1104; `incus/doc/howto/network_acls.md` line 233).
- **Address sets** (Incus only): `source=$<name>` / `destination=$<name>`. Supported on bridge networks using nftables and on OVN (`incus/doc/howto/network_acls.md` lines 115–130; API extension `network_address_set`, `incus/doc/api-extensions.md` line 2778). Shipped in Incus 7.0 LTS as a headline feature ([Incus 7.0 LTS announcement](https://discuss.linuxcontainers.org/t/incus-7-0-lts-has-been-released/26641)). LXD has no address sets: grepping `lxd/doc/api-extensions.md` for `address_set` finds nothing.

### 1.2 Where ACLs attach, and on which network types

| | LXD | Incus |
|---|---|---|
| Network-level `security.acls` on a managed **bridge** | Yes (`lxd/doc/howto/network_acls.md` lines 825–845; `lxd/doc/metadata.txt` `security.acls network-bridge-network-conf`, line 3307) | Yes (`incus/doc/howto/network_acls.md` lines 187–196; `incus/doc/config_options.txt` line 4186) |
| NIC-level `security.acls` on a **bridged** NIC | **No.** "For NICs, ACLs can only be used with the OVN NIC type" (`lxd/doc/howto/network_acls.md` line 904). `lxd/lxd/device/nic_bridged.go` has no `security.acls` key (its optional-field list is lines 74–92; `grep -c security.acls` returns 0) | **Yes.** `security.acls`, `security.acls.default.{ingress,egress}.{action,logged}` (`incus/internal/server/device/nic_bridged.go` lines 332–374; `incus/doc/config_options.txt` lines 795–832). Requires nftables: "Security ACLs are only supported when using nftables firewall" (`nic_bridged.go` lines 604–608). API extension `network_bridge_acl_devices` (`incus/doc/api-extensions.md` line 2688) |
| OVN network / OVN NIC | Yes (both levels) | Yes (both levels) |
| Firewall backend | nftables or xtables, auto-selected. ACL IP ranges are unsupported under xtables (`lxd/doc/howto/network_acls.md` line 1105). Selection logic: `lxd/lxd/firewall/firewall_load.go` lines 9–46 (xtables is chosen if xtables rules are already in use and the binaries are not nft shims) | nftables only (`incus/internal/server/firewall/firewall_load.go`: `New()` always returns `drivers.Nftables{}`). The 7.0 LTS changelog says "incusd/firewall: Drop xtables/iptables/ebtables backend (nftables only)" ([announcement](https://discuss.linuxcontainers.org/t/incus-7-0-lts-has-been-released/26641)) |
| Default action keys | `security.acls.default.{ingress,egress}.action` on the network (default `reject`) and on OVN NICs (`lxd/doc/howto/network_acls.md` lines 981–1021) | Same on the network (default `reject`, `incus/doc/config_options.txt` line 4194). On a bridged NIC the default is **`drop`** (`config_options.txt` lines 802–807) |

**Bridge limitations (both):** "bridge ACLs apply only at the boundary between the bridge and the LXD host … Intra-bridge firewalls … are not supported" (`lxd/doc/howto/network_acls.md` line 1103). Incus adds an exception: intra-bridge filtering works "when ACLs are applied directly to the NIC device" (`incus/doc/howto/network_acls.md` line 232). Both also say: "Baseline network service rules are added before ACL rules … ACL rules cannot block these baseline rules" (`lxd/...` line 1106; `incus/...` lines 234–235). Those baseline rules allow DNS (tcp/udp 53), DHCP (67/547) and core ICMP to the host (see §1.4).

### 1.3 Do ACLs apply to VMs?

**Yes.** The network-level ACL matches on the bridge interface (`iifname "<bridge>"`) in the host `input`, `output` and `forward` hooks. It applies to every port on the bridge, and a VM's tap device is just a bridge port. Source: `incus/internal/server/firewall/drivers/drivers_nftables_templates.go` lines 133–182 (`nftablesNetACLSetup`); LXD has the same template at `lxd/lxd/firewall/drivers/drivers_nftables_templates.go` lines 124–173. Rule rendering: "For egress, packets leaving the network's interface toward the host" → `iifname <network>` (`incus/internal/server/firewall/drivers/drivers_nftables.go` lines 1055–1068).

Bridged NICs, including the NIC-level filters, are allowed for both instance types: `instanceSupported(instConf.Type(), instancetype.Container, instancetype.VM)` (`incus/internal/server/device/nic_bridged.go` lines 68–71; `lxd/lxd/device/nic_bridged.go` line 64). For a VM, `host_name` is a `tap` device (`incus/.../nic_bridged.go` Start, around lines 914–925), and the per-NIC chains match `iifname "<tap>"`.

### 1.4 Who owns the rules; persistence; does the boot-race reconcile loop go away?

**Ownership.** The daemon writes all rules into its own nftables table, `incus` or `lxd` (`nftablesNamespace = "incus"`, `incus/internal/server/firewall/drivers/drivers_nftables.go` line 25; `const nftablesNamespace = "lxd"`, `lxd/lxd/firewall/drivers/drivers_nftables.go` line 24):

- Network-level rules go in the `inet` family: `aclin.<net>`/`aclout.<net>`/`aclfwd.<net>` hooked chains jumping to `acl.<net>`.
- Per-NIC rules go in the `bridge` family: `in.<label>`/`fwd.<label>`/`out.<label>`.

Jetty never touches netfilter.

**Startup ordering.** Networks are started before instances:

- Incus: `networkStartup` at `incus/cmd/incusd/daemon.go` line 1562, `instancesStart` at line 1723.
- LXD: `networkStartup` at `lxd/lxd/daemon.go` line 1870, `instancesStart` at line 2063.

Per-NIC filters are installed in the NIC's `Start()` before the device returns its run config. In Incus, `setupHostFilters(nil)` is called at `nic_bridged.go` line 979, and a failure aborts the start (fail-closed). On a full host shutdown (`SIGPWR`), instances are stopped **before** networks are torn down (`incus/cmd/incusd/daemon.go` lines 1898–1905; `lxd/lxd/daemon.go` lines 2261–2290). On a plain daemon stop or reload (`SIGTERM`, e.g. a snap refresh), networks and running instances are left alone.

**Inference:** no external process rebuilds Incus's tables the way Multipass rebuilds `mpqemubr0`'s, so the specific race that forces orca-proxy's polling loop does not exist.

**The gap that replaces it: network-level chains are deleted and recreated on every bridge `setup()`.** `bridge.Start()` always calls `n.setup(nil)` (`incus/internal/server/network/driver_bridge.go` lines 1011–1030). `setup()` begins by calling `Firewall.NetworkClear(...)` (lines 1357–1374). That removes the `fwd`, `pstrt`, `in`, `out`, `aclin`, `aclout`, `aclfwd` and `acl` chains for the network (`drivers_nftables.go` lines 321–346). Only about 700 lines later, after dnsmasq setup, does it call `Firewall.NetworkSetup` and `acl.FirewallApplyACLRules` (`driver_bridge.go` lines 2086 and 2111). LXD has the same structure (`lxd/lxd/network/driver_bridge.go`: `NetworkClear` at line 1548, `NetworkSetup` at 2168, `FirewallApplyACLRules` at 2182).

Because networks and VMs survive a `SIGTERM` daemon restart, any daemon restart or `network set` re-runs `setup()` while VMs are running. That leaves a window with **no network-level ACL and no `ipv4.routing=false` reject**.

**Unverified:** how long the window lasts. Measure it.

**Mitigations:**

- **Incus per-NIC chains are not part of `NetworkClear`.** They are named by instance device label, not network name (`drivers_nftables.go` lines 348–352, 353–466), so NIC-level ACLs and IP filters stay in force during the window.
- **`ipv4.nat=false`** means there is no masquerade even when the chains are gone. **Inference:** leaked packets would leave with private source addresses and replies would not route back. That does not stop one-way UDP leakage to a LAN host.

**External flushes (unverified).** Anything that runs `nft flush ruleset`, such as restarting a distro `nftables.service` whose config starts with `flush ruleset`, deletes Incus's tables too. I found no code in Incus that watches for or re-applies deleted tables. Recovery would be `systemctl restart incus` or a network re-setup.

### 1.5 Can ACLs allow egress only to `<bridge-ip>:<proxy-port>` (+ DNS) and drop everything else?

**Yes.** Network-level egress rules apply on the host `input` hook as well as `forward` (`aclin` chain: `iifname "<net>" jump acl.<net>`, `drivers_nftables_templates.go` lines 141–160). So an ACL can allow a specific host-local `ip daddr`/`tcp dport` and drop the rest, including the host's own sshd and other services on the bridge IP.

DHCP, DNS-to-host and core ICMP are always allowed ahead of the ACL (lines 146–158) and cannot be blocked by ACL rules (§1.2). Per-NIC Incus chains likewise always accept DHCP, DNS to the bridge address and `dns.nameservers`, ARP and ND (`drivers_nftables_templates.go` lines 222–261; DNS list built at `nic_bridged.go` lines 1617–1649).

Host-originated traffic into the bridge passes through `aclout`, so the ACL's **ingress** rules also govern host→VM connections. Orca's SSH into each VM needs an explicit ingress allow.

Example for Incus (LXD is identical without the NIC-level part):

```yaml
# incus network acl create jetty-egress < this.yaml
description: Jetty VMs may only reach orca-proxy; host may SSH in
egress:
  - action: allow
    protocol: tcp
    destination: 10.77.0.1/32
    destination_port: "3128"
ingress:
  - action: allow
    protocol: tcp
    source: 10.77.0.1/32
    destination_port: "22"
```

```
incus network set jettybr0 security.acls=jetty-egress \
  security.acls.default.egress.action=reject security.acls.default.ingress.action=drop
```

`reject` on egress makes tools that ignore the proxy fail fast instead of hanging. For per-NIC egress, `reject` is rendered as `drop` in the bridge `forward` chain, because nftables can't reject there (`drivers_nftables.go` lines 614–750).

### 1.6 Is there any ACL action that redirects (transparent proxying)?

**No, confirmed.** The only actions are the four in §1.1. The nftables renderer only emits accept, drop or reject verdicts for ACL rules (`aclRulesToNftRules`, `drivers_nftables.go` lines 614–750). The only DNAT templates in the driver are:

- `nftablesNetProxyNAT` (lines 96–131): proxy-device NAT and network forwards. It matches `daddr <listenAddress>` or `fib daddr type local`, i.e. traffic addressed to the host. It cannot express "all tcp/443 from the bridge to the proxy".
- `NetworkApplyForwards` (line 1320): network forwards, which are inbound by definition (§3).

---

## 2. NIC anti-spoofing

**Short answer:** yes. `security.ipv4_filtering`/`ipv6_filtering` on a bridged NIC close the "root agent adds another IP / impersonates another VM" bypass. They are supported for VMs, implemented as per-NIC nftables bridge-family chains (or ebtables under LXD xtables), and work on a managed bridge with or without a static `ipv4.address`; with no static address, one is allocated and pinned. `security.port_isolation` blocks VM↔VM traffic on the bridge.

- **Semantics.** `security.ipv4_filtering`: "Prevent the instance from spoofing another instance's IPv4 address (enables `security.mac_filtering`)". IPv6 is the same. `security.mac_filtering`: "Prevent the instance from spoofing another instance's MAC address". `security.port_isolation`: "Prevent the NIC from communicating with other NICs in the network that have port isolation enabled". Sources: `incus/doc/config_options.txt` lines 834–864; `lxd/doc/metadata.txt` lines 635–671.
- **Implementation (Incus).** `InstanceSetupBridgeFilter` (`drivers_nftables.go` lines 353–466) renders `nftablesInstanceBridgeFilter` (`drivers_nftables_templates.go` lines 206–383) into the `bridge` family, hooks `input` and `forward`, at priority −200. For frames entering from the instance's `host_name`, it drops:
  - any `ether saddr != <hwaddr>`
  - any ARP whose sender MAC or IP is not the NIC's own
  - any IPv4 `saddr` outside the allowed set
  - IPv6 router advertisements, and IPv6 from outside the allowed prefixes
  - all non-ARP/IP/IPv6 ethertypes

  If IPv6 is unavailable (no DHCPv6 on the network), the address becomes `none`, which drops all IPv6 from the NIC (`nic_bridged.go` lines 1574–1603; template `ipv6FilterAll`). LXD's template is equivalent (`lxd/lxd/firewall/drivers/drivers_nftables_templates.go` lines 197–268). In LXD, `ipv6_filtering` needs `br_netfilter` (`lxd/lxd/device/nic_bridged.go` lines 987–994); Incus 7.5 removed that requirement ([Incus 7.5 announcement](https://discuss.linuxcontainers.org/t/incus-7-5-has-been-released/27273)).
- **Requirements.**
  - On a **managed** bridge, no static IP is needed. Incus "allocate[s] the static IPs (if needed)" through `dhcpalloc.AllocateTask` and writes them into dnsmasq, so the address is pinned (`incus/.../nic_bridged.go` lines 1566–1608; LXD lines 1130ff).
  - On an **unmanaged** bridge, a manual address is required: "IPv4 filtering requires a manually specified ipv4.address when using an unmanaged parent bridge" (`incus/...` lines 1554–1562; `lxd/...` lines 256–271).
  - A static `ipv4.address` must be inside the network's DHCP subnet and not the bridge's own IP (`incus/...` lines 432–460; `lxd/...` lines 107–139). On a network with DHCP off, it is accepted only with `ipv4_filtering`: "Cannot specify "ipv4.address" when DHCP is disabled (unless using security.ipv4_filtering)".
- **Port isolation** sets the kernel bridge-port `isolated` flag: `link.BridgeLinkSetIsolated(true)` (`incus/.../nic_bridged.go` lines 995–1001; LXD line 600).
- **Does it close the bypass?** Yes for source-IP and MAC spoofing on the bridge: packets with any other source address are dropped before they reach the host IP stack or another port. Adding a second IP in the guest therefore gets nothing out.

  **Inference:** together with `ipv4.address` pinned by the provisioner, the source IP orca-proxy sees is a trustworthy VM identity. That holds even for the explicit proxy, where it is the TCP peer address of the CONNECT.
- **Update caveats.**
  - LXD removes old filters *before* applying new ones on a live NIC update (`lxd/lxd/device/nic_bridged.go` lines 996–1000).
  - Incus had the same fail-open behaviour for bridged NIC ACLs ([lxc/incus#4008](https://github.com/lxc/incus/issues/4008), reported on 7.4, milestone 7.5). Current main applies the new filters first and restores the old ones on failure (`incus/.../nic_bridged.go` lines 1402–1439).
  - Set these keys when the instance is created, not on a running NIC, and use Incus ≥ 7.5. **Unverified:** whether the fix was backported to 7.0.x LTS.

---

## 3. Other built-ins

**Short answer:** none of them redirects or proxies egress. Two are useful in a supporting role: an isolated bridge (`ipv4.nat=false`, `ipv4.routing=false`) as a second, non-ACL fail-closed layer, and restricted projects (§4). A "router instance" could keep transparent interception without touching host netfilter, but it is a re-architecture.

- **`proxy` devices.**
  - VMs support NAT mode only: "Only NAT mode is supported for proxies on VM instances" (`incus/internal/server/device/proxy.go` line 181; `lxd/lxd/device/proxy.go` line 172).
  - NAT mode only works host-bound: "Only host-bound proxies can use NAT" (`incus/...` line 227; `lxd/...` line 218).
  - So for a VM it can only forward a host listen address *into* the guest. It cannot create a guest-side listener that tunnels to the host.
  - Docs: "supported for both containers (NAT and non-NAT modes) and VMs (NAT mode only)" (`incus/doc/reference/devices_proxy.md`).
  - Blocked by default in restricted projects (`restricted.devices.proxy` default `block`, `incus/doc/config_options.txt` line 5252; `lxd/doc/metadata.txt` line 4479).
- **Isolated bridge.**
  - `ipv4.nat` (`incus/doc/config_options.txt` line 4026) and `ipv4.routing` "Whether to route traffic in and out of the bridge", default `true` (line 4066; LXD `lxd/doc/metadata.txt` line 3185).
  - With `ipv4.routing=false`, Incus does not set `net.ipv4.ip_forward` and writes a `fwd.<net>` chain whose action is `reject` for `iifname`/`oifname <net>` (`driver_bridge.go` lines 1426–1436; `drivers_nftables.go` lines 138–175; template lines 16–30).
  - The host-local proxy stays reachable because that traffic takes the `input` path. This blocks forwarding regardless of ACL contents. It lives in the same daemon-owned table, so it shares the §1.4 rebuild window.
  - **Inference:** on a host with `ip_forward=0`, the window is closed for forwarding. On a host where other software (Multipass, Docker) has set `ip_forward=1`, it is not.
- **OVN networks.** These have ACLs at both levels, selectors and `allow-stateless`, but no redirect action, and they need OVN/OVS plus an uplink network. Incus also says `features.networks` (per-project networks) "requires the server to be configured for OVN" (`incus/doc/config_options.txt` lines 5036–5042). Too heavy for a single-host dev tool, and it adds nothing to egress redirection.
- **Network forwards.** "Network forwards allow an external IP address (or specific ports on it) to be forwarded to an internal IP address" (`incus/doc/howto/network_forwards.md` lines 4–16). Inbound only.
- **`bridge.external_interfaces`.** "Comma-separated list of unconfigured network interfaces to include in the bridge" (`incus/doc/config_options.txt` line 3874). This is L2 uplinking, the opposite of what Jetty wants.
- **Built-in HTTP proxy or egress gateway.** None for instances. The `core.proxy_http(s)` server keys are the daemon's own outbound proxy (image downloads). I found no instance-egress proxy feature in either repo's docs.
- **"Router instance" pattern** (brief feasibility check, **unverified/inference**):
  - **Topology.** Create `jettybr0` with `ipv4.address=none`, so the host has no L3 presence on it and host netfilter never routes VM traffic. Admin-create a gateway container with one NIC on `jettybr0` and one on a NAT bridge. VMs get static addresses (allowed with `ipv4_filtering` even when DHCP is off, §2), have their default route via the gateway, and use `ipv4_filtering`/`port_isolation`. The gateway's own NIC is not isolated.
  - **What it gains.** The gateway does `nft ... redirect` inside its own netns, which needs no host privilege. That keeps transparent interception.
  - **Cost: credentials move.** orca-proxy (CA, credentials, management API) would have to run inside the gateway. The alternative is accepting the gateway's address as the source IP, which loses per-VM identity, unless PROXY protocol is used, and I did not verify mitmproxy accepts that inbound.
  - **Cost: no host DHCP/DNS** on that bridge. Possible `br_netfilter` interactions are also unverified (see §7).
  - This is a substantially larger change than §7's design.

---

## 4. Privilege model

**Short answer:**

- Full access to the daemon socket (`lxd` group / `incus-admin` group) is root-equivalent by the projects' own statements.
- Restricted projects, reached through `lxd-user`/`incus-user` or a restricted TLS client certificate, cannot change server config. An admin creates the bridge and ACL, and binds the project to that bridge, once.
- A restricted identity can then create VMs only on that bridge, and can set `ipv4.address`, `ipv4_filtering`, `port_isolation` and (Incus) NIC `security.acls` on its own NICs.
- Nothing forces the provisioner to set the NIC-level keys. Only the network-level ACL is out of its reach.
- The proxy process needs **no** privilege.

### 4.1 Admin groups are root-equivalent

- "Local access to LXD through the Unix socket always grants full access to LXD. This includes the ability to attach file system paths or devices to any instance as well as tweak the security features on any instance. Therefore, you should only give such access to users who you'd trust with root access to your system." (`lxd/README.md` lines 88–93; Incus wording is identical at `incus/README.md` lines 61–66.)
- "The root user and all members of the `lxd` group can interact with the local daemon" (`lxd/doc/explanation/security.md` line 52). The Incus equivalent names `incus-admin` (`incus/doc/explanation/security.md` lines 34–36).
- "Members of the `incus-admin` group have full access to Incus … which makes it possible to gain root access to the host system. Using confined projects limits what users can do in Incus, but it also prevents users from gaining root access." (`incus/doc/explanation/projects.md` lines 54–56; LXD lines 67–69.)

### 4.2 `lxd-user` / `incus-user` and restricted projects

- **Incus.**
  - Users in the `incus` group, but not `incus-admin`, get a dynamically created confined project (`incus/doc/explanation/projects.md` lines 64–69; `incus/doc/howto/projects_confine.md` lines 47–58).
  - `incus-user` creates project `user-<uid>` with `restricted=true`, `features.networks=false`, `restricted.networks.access=incusbr-<uid>`, nesting allowed and disk paths limited to the home directory. It also creates bridge `incusbr-<uid>` in the default project (`incus/cmd/incus-user/server.go` lines 156–261).
  - "To modify the project configuration, you must have full access to Incus, which means you must be part of the `incus-admin` group" (`projects_confine.md` line 58).
- **LXD.**
  - The snap's multi-user daemon is enabled with `sudo snap set lxd daemon.user.group=<user_group>` (`lxd/doc/howto/projects_confine.md` lines 261–273; `lxd/doc/explanation/projects.md` lines 72–80).
  - `lxd-user` creates the same kind of project with `restricted.networks.access=lxdbr-<uid>` (`lxd/lxd-user/lxd.go` lines 160, 207–226).
- **Restricted TLS certificate** (alternative to `*-user`): `incus config trust add-certificate <cert> --projects <project> --restricted` (`incus/doc/howto/projects_confine.md` lines 9–30). This needs an HTTPS listener (`core.https_address`).
- **Relevant `restricted.*` keys** (`incus/doc/config_options.txt` lines 5234–5366; `lxd/doc/metadata.txt` lines 4461–4578):
  - `restricted.devices.nic` (default `managed`): NICs must reference a managed `network=`.
  - `restricted.networks.access`: comma list of allowed networks.
  - `restricted.devices.proxy` (default `block`).
  - `restricted.virtual-machines.lowlevel` (default `block`): blocks `raw.qemu`. That matters because `raw.qemu` could otherwise add, for example, a QEMU user-mode NIC that bypasses the bridge entirely (**inference**).
  - `features.networks` (default `false`): with it off, the project uses default-project networks and ACLs (`incus/internal/server/project/project.go` lines 194–230).
- **Enforcement in code.** The NIC check only verifies `network=` and network allow-listing (`incus/internal/server/project/permissions.go` lines 873–897). **No project restriction forces or forbids `security.ipv4_filtering`, `security.port_isolation` or NIC `security.acls`.** So NIC-level keys are whatever the provisioner sets. Only the network-level `security.acls`, owned by the admin, is mandatory for every NIC on that bridge.

### 4.3 Can a restricted user create ACLs, set NIC `security.acls`, set `ipv4_filtering`, or loosen the network?

- **Set `ipv4.address`, `ipv4_filtering`, `ipv6_filtering`, `port_isolation`, NIC `security.acls` on its own instances: yes.** Nothing in `permissions.go` restricts them (§4.2). For NIC ACLs, the ACL is resolved in the network project, which is `default` when `features.networks=false` (`incus/.../nic_bridged.go` lines 610–623). So a restricted user can reference an admin-created ACL by name. **Inference from netfilter semantics:** a NIC-level allow cannot widen the network-level ACL. They are separate base chains and a drop in either is final.
- **Edit or remove the network-level ACL or the network: Incus no.**
  - Network and ACL objects resolve to the effective project, `default` for `features.networks=false` (`incus/cmd/incusd/daemon.go` lines 393–400).
  - A restricted certificate gets only read-only access to inherited default-project networks: "Also allow read-only access to inherited resources" (`incus/internal/server/auth/driver_tls.go` lines 78–93).
- **LXD: probably yes (unverified, from source).** LXD's TLS authorizer has `checkEffectiveProject`: "if the caller has access to any projects that have e.g. features.networks=false, then they can view networks in the default project". The code, however, returns `true` for any entitlement, not just view (`lxd/lxd/auth/drivers/tls.go` lines 203–223, used at 120–130). `networkPut` then only checks `project.NetworkAllowed(...)` (`lxd/lxd/networks.go` lines 1515–1546). So a restricted LXD identity whose project allows `jettybr0` may be able to PATCH `jettybr0`, including `security.acls`. **Verify empirically.** This does not affect the guest-root threat, but it weakens least privilege for the provisioner on LXD.
- **Create new ACLs:** creation authorizes against the *requested* project (`allowPermission(auth.ObjectTypeProject, auth.EntitlementCanCreateNetworkACLs)`, `incus/cmd/incusd/network_acls.go` line 33). The handler then writes the ACL into `NetworkProject(...)`, which is `default` for `features.networks=false` (lines 312–352). **Unverified quirk:** a restricted identity may be able to create (but not later edit) a default-project ACL. It is harmless, because nothing applies it unless an admin attaches it to a network or the identity adds it to its own NIC, which can only narrow. Don't build on it.
- **Bottom line.** ACL and network setup needs admin access once. The provisioner can be a restricted identity (`incus` group via `incus-user`, or a restricted cert).

### 4.4 Can the guest (root inside the VM) touch any of this?

**No.** The only guest↔host API is `/dev/incus/sock` (`/dev/lxd/sock`), which is limited to the requesting instance. Its only write is `PATCH /1.0` for instance state `Ready`/`Started` (`incus/doc/dev-incus.md` lines 90–105). It can be turned off with `security.guestapi=false` (Incus, `dev-incus.md`) or `security.devlxd=false` (LXD, `lxd/doc/dev-lxd.md`). For VMs the agent reaches it over vsock (`lxd/doc/dev-lxd.md`, "Virtual machines" section). One newer LXD feature to keep off: DevLXD **bearer tokens** let guest processes authenticate as an identity (`lxd/doc/dev-lxd.md` "Bearer tokens"). **Unverified** which actions such identities may perform; just don't issue them.

### 4.5 Does the proxy need privilege?

**No (inference).** In the ACL design orca-proxy is an ordinary listener on `<bridge-ip>:<high-port>`. In explicit mode it does not need `SO_ORIGINAL_DST` and writes no rules. `firewall.py`, `orca-proxy-firewall-sync`, its `setcap cap_net_admin,cap_net_raw` install step and the polling loop (`orca-proxy/design.md` lines 156–176, 381–503) all go away. It must start after the bridge exists, or it can't bind the bridge IP (order the unit after `incus.service`).

---

## 5. Explicit-proxy implications

**Short answer:** with HTTPS_PROXY plus egress deny, a tool that ignores proxy env gets a refused connection (fail closed) instead of escaping. Most of Jetty's agent stack honours proxy env natively. Node needs `NODE_USE_ENV_PROXY=1` (Node ≥ 24.5 / 22.21). apt is best configured via `apt.conf`. mitmproxy's regular mode takes the target host from the CONNECT request.

- **Fail-closed by construction.** The only allowed egress is the proxy (§1.5), so a direct connection gets `reject`. Today's design only governs tcp/80,443 and lets everything else out (`orca-proxy/design.md` lines 156–166). Deny-all is stricter: SSH to GitHub, QUIC/HTTP3, other ports and UDP stop working unless the ACL allows them by IP.
- **Node.js.** Built-in proxy support exists. "When Node.js creates the global agent, if the `NODE_USE_ENV_PROXY` environment variable is set to `1` or `--use-env-proxy` is enabled, the global agent will be constructed with `proxyEnv: process.env`". It reads `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`. Added in v24.5.0 / v22.21.0, Stability 1.1 (`nodejs/node` `doc/api/http.md` "Built-in Proxy Support", main at `90d71c11`). The `NODE_USE_ENV_PROXY=1` env var was added in v24.0.0 / v22.21.0 (`doc/api/cli.md`). It is off by default.
- **Claude Code.** "Claude Code respects standard proxy environment variables", in the order `https_proxy`, `HTTPS_PROXY`, `http_proxy`, `HTTP_PROXY`. "Claude Code does not support SOCKS proxies." Custom CA goes in `NODE_EXTRA_CA_CERTS` ([code.claude.com/docs/en/network-config](https://code.claude.com/docs/en/network-config)).
- **Codex CLI.** HTTP clients use reqwest's default builder: `ProxyRouting::TransportDefault => builder` (`codex-rs/http-client/src/client_builder.rs` lines 351–356). reqwest docs: "System proxies are enabled by default", reading `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` ([docs.rs/reqwest 0.12](https://docs.rs/reqwest/0.12/reqwest/)). The WebSocket dialer "resolves HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, and NO_PROXY before opening the socket" (`codex-rs/websocket-client/src/dialer.rs` lines 45–58).
- **git.** `http.proxy`: "Override the HTTP proxy, normally configured using the 'http_proxy', 'https_proxy', and 'all_proxy' environment variables (see curl(1))" (`git/git` `Documentation/config/http.adoc`). SSH remotes are not covered.
- **npm.** `https-proxy`: "If the `HTTPS_PROXY` or `https_proxy` or `HTTP_PROXY` or `http_proxy` environment variables are set, proxy settings will be honored by the underlying `make-fetch-happen` library" (`npm/cli` `workspaces/config/lib/definitions/definitions.js` lines 1021–1031).
- **pip.** "by setting the standard environment-variables `http_proxy`, `https_proxy` and `no_proxy`" (`pypa/pip` `docs/html/user_guide.rst` lines 86–98).
- **apt.** "The environment variable http_proxy is supported for system wide configuration. Proxies specific to APT can be configured via the option Acquire::http::Proxy" (`man apt-transport-http`). HTTPS options "default to the same values specified for Acquire::http" (`man apt-transport-https`). Use `/etc/apt/apt.conf.d/` rather than env, because `sudo` usually resets the environment (**inference**).
- **mitmproxy regular mode.** "Mitmproxy's regular mode is the simplest and the most robust to set up. If your target can be configured to use an HTTP proxy, we recommend you start with this." In regular mode the client tells the proxy the target ([docs.mitmproxy.org/stable/concepts/modes](https://docs.mitmproxy.org/stable/concepts/modes/)), which for HTTPS is the CONNECT authority `host:port`.
  - `ignore_hosts` is "matched against a `host:port` string" (`research/transparent-mitm-passthrough.md` §1.2). orca-proxy's SNI-peek `tls_clienthello` hook still runs on the TLS inside the CONNECT tunnel.
  - **Verify:** what happens when CONNECT host ≠ SNI. In regular mode the upstream is the CONNECT target. orca-proxy matches inject rules by SNI (`orca-proxy/src/orca_proxy/proxy_addon.py` lines 109–153). Credential safety relies on mitmproxy verifying the upstream certificate against the SNI hostname, which is the same reliance as today with a spoofed destination IP.

---

## 6. LXD vs Incus differences that matter here

**Short answer:** Incus is the better fit for this design. It has NIC-level ACLs on bridged NICs (which survive the network re-setup window), address sets, nftables-only operation, stricter restricted-identity network permissions, an Apache-2.0 licence, distro packages, and Zabbly LTS/stable repos. LXD works too, with network-level ACLs only, and is snap-first.

- **Licence and governance.**
  - LXD repo `COPYING` is GNU AGPL v3. "All contributors must sign the Canonical contributor license agreement (CCLA) … contributions are licensed under the project's **AGPL-3.0-only** license" (`lxd/CONTRIBUTING.md` lines 16–24). Client SDKs are Apache-2.0 (`lxd/README.md` line 49).
  - Incus "started as a community fork of Canonical's LXD following Canonical's takeover … free of any CLA and remains released under the Apache 2.0 license" (`incus/README.md` lines 18–25; `incus/COPYING` is Apache 2.0).
- **Packaging.**
  - LXD: "The recommended way to install LXD is its snap package" (`lxd/doc/installing.md` line 23). Alpine, Arch and Gentoo have native packages tracking feature releases; Fedora only has an "unofficial and minimally tested" COPR (`lxd/doc/installing.md` lines 100–140). Snaps auto-refresh by default (line 96), and each refresh is a daemon restart, which hits the §1.4 window.
  - Incus: native packages in Debian 13 (tracking LTS), Ubuntu 24.04+, Fedora, Arch, Alpine, Gentoo and others. Zabbly repos cover Debian 11–13 and Ubuntu 22.04/24.04/26.04 (`incus/doc/installing.md` lines 35–239). Zabbly channels are `lts-6.0`, `lts-7.0`, `stable` and `daily` ([7.0 LTS announcement](https://discuss.linuxcontainers.org/t/incus-7-0-lts-has-been-released/26641)). **Unverified:** the exact Incus version in Ubuntu's archive for this host (kernel 7.0 suggests 26.04).
- **Cadence and LTS.**
  - LXD: LTS every two years following Ubuntu. Currently 5.21 (until June 2029) and 5.0 (until June 2027). Feature releases are 6.x and "not recommended for production use" (`lxd/doc/reference/releases-snap.md`; `lxd/doc/substitutions.yaml` lines 17–18).
  - Incus: 7.0 LTS (released 2026-05, supported until June 2031). 6.0 LTS is security-only through June 2029. Monthly feature releases are supported until the next one (announcements [7.0 LTS](https://discuss.linuxcontainers.org/t/incus-7-0-lts-has-been-released/26641), [7.5](https://discuss.linuxcontainers.org/t/incus-7-5-has-been-released/27273); `incus/doc/support.md` lines 7–18).
- **Feature divergence relevant here.**
  - Bridged-NIC ACLs: Incus only (§1.2).
  - Address sets: Incus only (§1.1).
  - Firewall: Incus nftables-only (≥ 7.0); LXD nftables or xtables (§1.2).
  - ACL and bridged-filter bug fixes landed in Incus 7.4–7.5: fail-open on update [#4008](https://github.com/lxc/incus/issues/4008), "Don't widen ACL rules for the other IP family" ([#4022](https://github.com/lxc/incus/issues/4022); 7.5 changelog), and "Allow DHCPv4 discovery through the forward chain" (7.5). This code is still young; pin ≥ 7.5 or test the LTS.
  - Multi-user daemon: `incus-user` uses the `incus` vs `incus-admin` groups; `lxd-user` uses the snap `daemon.user.group` setting (§4.2).
  - Restricted identities editing default-project networks: Incus no, LXD possibly yes (§4.3).
  - OCI application containers: Incus only (`instance_oci`, `incus/doc/api-extensions.md` line 2520; none in LXD's list). Not needed for VMs.
- **VM agents.** These are functionally equivalent. `lxd-agent`/`incus-agent` must run in the VM for file and exec operations (`lxd/doc/howto/instances_access_files.md` line 13; `incus/doc/howto/instances_access_files.md` line 9). The agent also proxies the guest API over vsock (`lxd/doc/dev-lxd.md`). Neither agent has a network-policy role, and killing it in the guest only loses exec/file convenience (**inference**).

---

## 7. Recommended enforcement design for Jetty

**Use Incus (≥ 7.5, Zabbly `stable`, or 7.0 LTS once the bridged-NIC ACL fixes are confirmed backported) in explicit-proxy mode, with layered daemon-owned filtering. Jetty writes no netfilter rules, has no setcap binary and runs no reconcile loop.**

### 7.1 Mechanisms

1. **Dedicated isolated bridge `jettybr0`:** `ipv4.address=10.77.0.1/24 ipv4.nat=false ipv4.routing=false ipv6.address=none`. No forwarding or NAT means a lost ACL fails mostly closed (§3).
2. **Network-level ACL `jetty-egress`** on `jettybr0`. Egress allows only tcp `10.77.0.1:3128`; ingress allows only tcp/22 from `10.77.0.1` (Orca's SSH). `security.acls.default.egress.action=reject`, `security.acls.default.ingress.action=drop` (§1.5). Mandatory for every NIC on the bridge; only an admin can change it (§4.3).
3. **Per-NIC filters on every VM NIC**, through a profile in the Jetty project:
   - `security.ipv4_filtering=true`, `security.ipv6_filtering=true` (drops all IPv6 since the bridge has none)
   - `security.port_isolation=true`
   - `security.acls=jetty-egress`, `security.acls.default.egress.action=drop`

   Plus a per-VM static `ipv4.address`. These close the IP-spoofing and VM↔VM bypasses (§2). The NIC-level copy of the ACL keeps enforcing during the network re-setup window (§1.4).
4. **orca-proxy in mitmproxy regular (explicit) mode**, listening on `10.77.0.1:3128` as a normal user. VM identity is the TCP source IP, which is now trustworthy because of (3). The rule engine and the SNI-based passthrough/MITM logic stay the same.
5. **Guest configuration** by the provisioner:
   - `/etc/environment`: `HTTP(S)_PROXY=http://10.77.0.1:3128`, `NO_PROXY=localhost,127.0.0.1`, `NODE_USE_ENV_PROXY=1`
   - `apt.conf.d` `Acquire::http::Proxy`
   - Interception CA trust (as today) for MITMed hosts

### 7.2 One-time admin setup (root or `incus-admin`, done once by the host owner)

```
incus network create jettybr0 ipv4.address=10.77.0.1/24 ipv4.nat=false ipv4.routing=false ipv6.address=none
incus network acl create jetty-egress < jetty-egress.yaml        # §1.5
incus network set jettybr0 security.acls=jetty-egress \
  security.acls.default.egress.action=reject security.acls.default.ingress.action=drop
incus project create jetty -c restricted=true -c restricted.networks.access=jettybr0
#   (restricted.devices.nic=managed, restricted.devices.proxy=block,
#    restricted.virtual-machines.lowlevel=block are the defaults; §4.2)
# then EITHER: add the Jetty user to the `incus` group (incus-user) and point its
#   user-<uid> project at jettybr0 (restricted.networks.access=jettybr0),
# OR: incus config set core.https_address=127.0.0.1:8443 and
#   incus config trust add-certificate jetty.crt --projects jetty --restricted
```

`incus-user` projects also allow nesting and disk paths under the user's home by default (§4.2). Review them against Jetty's needs, e.g. workspace mounts need `restricted.devices.disk` and `restricted.devices.disk.paths`.

### 7.3 Runtime privilege

- **orca-proxy:** none (§4.5). It needs systemd ordering after Incus so the bridge IP exists.
- **Provisioning agent:** a restricted Incus identity scoped to the Jetty project (§4.2). It cannot touch the bridge or the network-level ACL. It is trusted to set the NIC-level keys and `ipv4.address` correctly and to register name→IP with orca-proxy, as it is trusted to register VMs today. The guest root user cannot alter any of it (§4.4).

### 7.4 What is lost compared with today's transparent design

- **Tools that ignore proxy env fail closed** instead of being transparently intercepted:
  - Node without `NODE_USE_ENV_PROXY` (or Node < 22.21 / < 24.5) unless the app has its own proxy support
  - anything on raw sockets
  - Docker-in-VM image pulls (they need daemon-level proxy config)
  - git over SSH and any non-HTTP(S) protocol, unless the ACL allows it by IP

  Most of the agent toolchain (Claude Code, Codex, git-https, npm, pip, apt-via-conf) honours the proxy (§5).
- **The non-web default changes.** Today non-80/443 egress is unrestricted (`orca-proxy/design.md` lines 156–166). This design is deny-all. To mimic today exactly, use `ipv4.nat=true`/`ipv4.routing=true` with ACL egress `allow tcp→proxy`, `drop tcp dport 80,443`, default `allow`. That gives up the isolated-bridge layer and makes the §1.4 window fail open for web traffic.
- **Hostname source changes.** The proxy sees the CONNECT host rather than SNI-or-nothing. That is arguably better (it covers no-SNI clients), but it adds the CONNECT-vs-SNI consistency check in §5.
- **Migration from Multipass.** The `orca-ssh-setup` skill must be rewritten for Incus VMs (launch, cloud-init, IP pinning).

### 7.5 Open questions and things to verify empirically

1. Measure the network-level ACL gap during `systemctl restart incus` and `incus network set` with VMs running. Confirm the NIC-level `bridge incus` chains stay in place throughout (§1.4).
2. Check what `nft flush ruleset`, a distro `nftables.service` restart and firewalld reloads do to Incus's tables, and whether anything re-applies them. Decide whether a lightweight `/readyz`-style check is still wanted (read-only; `nft list` needs CAP_NET_ADMIN, so it might have to go through `incus network show`/logs instead).
3. In a VM: adding a secondary IP, changing the MAC, ARP-claiming `10.77.0.1`, and direct connections to the host's other IPs and to other VMs should all fail (§2, §1.5).
4. Coexistence with Multipass (`mpqemubr0`) and Docker on the same host: `br_netfilter` (a related double-conntrack issue on IncusOS: [forum thread](https://discuss.linuxcontainers.org/t/network-acls-with-bridge-networks-dont-work-as-expected/27059)) and Docker's FORWARD-drop policy (`incus/doc/howto/network_bridge_firewalld.md` lines 103–114).
5. Whether the Incus version in the Ubuntu archive or 7.0.x LTS has the bridged-NIC ACL fixes from 7.4/7.5 ([#4008](https://github.com/lxc/incus/issues/4008), [#4022](https://github.com/lxc/incus/issues/4022)). If not, use Zabbly `stable`.
6. With the restricted identity:
   - Confirm it **cannot** `incus network set jettybr0 …` or edit `jetty-egress`.
   - Confirm it **can** set `ipv4.address`/`ipv4_filtering`/NIC `security.acls`.
   - Check the ACL-create quirk (§4.3).
   - If LXD is chosen instead, check whether LXD's restricted identities can edit the network (§4.3).
7. mitmproxy regular mode: confirm upstream certificate verification is against SNI when CONNECT host ≠ SNI, and that the `tls_clienthello` passthrough path behaves as in transparent mode (§5).
8. Residual non-HTTP channels that ACLs cannot close: DNS to the host's dnsmasq is always allowed (§1.2), the same DNS-tunnelling exposure as today. Vsock from guest to host reaches only the Incus agent API (§4.4). Confirm no other vsock listeners exist on the host.
9. If transparent interception is a hard requirement, the only daemon-native route that avoids host netfilter is the router-instance pattern (§3), which needs its own design pass.
