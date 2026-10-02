# PROTOTYPE — gateway-VM egress (research/incus-migration-plan.md, Phase 1 transport gate)

Throwaway. Answers one question: can agent VMs be forced through the **unmodified**
orca-proxy addon, without host iptables writes or privileged helpers, using
`agent VM -> gateway VM -> WireGuard -> host mitmproxy (wireguard mode)`?

Ran on the host's existing **LXD 5.21.8** (same network model as Incus) in an
isolated `jetty-proto` project, with 2 vCPU / 2 GiB VMs (`ubuntu:24.04`), mitmproxy
10.4.2 + mitmproxy-rs 0.6.3 from `orca-proxy/uv.lock`, and the real
`src/orca_proxy/proxy_addon.py` with isolated state (`ORCA_PROXY_HOME`) and a
no-op firewall helper (`firewall-stub.py`).

```
./up.sh      # networks, test orca-proxy, gateway, agents a/b, VM registration, CA
./checks.sh  # gate checks (ends by stopping the proxy and rebooting the gateway)
./up.sh      # restore after checks
./down.sh    # delete project, VMs, networks; stop proxy
```

## Verdict (2026-10-02): the transport gate passes

All 26 checks pass:

- **Identity through one WireGuard peer.** Two agents share one tunnel and the gateway does no source rewriting (no SNAT). The real addon attributed every connection to the right VM, including 10/10 under concurrent traffic. It also saw the original destination IP. This matches mitmproxy-rs 0.6.3's `peers_by_ip` (`src/packet_sources/wireguard.rs:290`), which accepts any inner source address.
- **Policy parity on TLS.**
  - The VM-scoped Allow-with-credential rule injects only for `proto-a`.
  - The same host from `proto-b` passes through without needing the CA.
  - Unmatched traffic passes through.
  - Large MITM responses work.
- **Guest root can't escape or impersonate.**
  - Spoofing b's IP, using an unassigned IP, changing the MAC or claiming the gateway IP all fail. LXD's `security.ipv4_filtering` / `mac_filtering` **work on a bridge with `ipv4.address=none`**; the NIC's `ipv4.address` is accepted there.
  - The host has no IPv4 on the agent bridge.
  - The gateway drops forwarded traffic to RFC1918, link-local and CGNAT ranges. That blocks the host's uplink IP, LAN IP, Multipass bridge and Tailscale.
  - The agent can't reach the gateway's own services.
- **Fail closed.** No direct path opens when:
  - the proxy is down (the direct-443 DROP counter stays at 0)
  - `wg0` is deleted (the unreachable route in table 100 catches it)
  - the gateway is stopped
  - the gateway reboots without its config (`ip_forward=0`, no private IP)
- **Operations.**
  - Host SSH reaches agents via ProxyJump through the gateway.
  - A 100 MB passthrough download at WireGuard MTU 1420 with MSS clamping ran at 28.4 MB/s, the same as the host's direct speed.
- **Privileges.** Host-side, only an unprivileged mitmdump (UDP 51820 on the uplink bridge IP). No sudo, and no host firewall writes by Jetty code. LXD's daemon owns the two bridges and their NAT. All policy netfilter lives inside the gateway VM's kernel.

## Findings to carry into the plan

1. **Plain HTTP (:80) is unlogged and unattributed.** It works, but the addon creates no `connections` row (VM identity is only set in `tls_clienthello`), and `http_requests.connection_id` is `NOT NULL`. This confirms the plan's "existing gaps" item; it is not caused by the new transport.
2. **Agents can reach private addresses on 80/443 through the proxy.** The gateway sends all agent tcp/80,443 into the tunnel *before* its RFC1918 drop. mitmproxy then dials the original destination from the host, so a guest can reach host-local or LAN web services (router admin pages, `10.201.0.1:443`). That matches today's transparent mode, but now it is the only remaining route to the host. Fix either by marking only non-private destinations on the gateway, or with a destination check in the addon.
3. **Agent-to-agent traffic is open on the shared L2** (b:22 reachable from a). Set `security.port_isolation=true` on agent NICs and leave the gateway NIC non-isolated.
4. **mitmproxy leaks WireGuard keys.** On startup mitmdump prints the full client config, **including `PrivateKey`**, to stdout, which becomes the journal under systemd. It stores both private keys in `<confdir>/wireguard.conf` with mode **0664**. Suppress or redact that log line and `chmod 600` the file, or generate the keys outside mitmproxy.
5. **Gateway configuration is ephemeral here.** Production needs it persisted, with boot ordering that enables `ip_forward` only after `wg0`, table 100 and the filter rules are in place. The default (`ip_forward=0`) already fails closed.
6. **Lifecycle quirk.** LXD 5.21 sometimes refuses `start` right after `stop` for a VM ("Error status"), so the lifecycle code must retry.

## Not tested here

- Incus itself. NIC filtering on an address-less bridge needs rechecking on the target Incus version.
- LXD/Incus daemon restart and host reboot (they need sudo or are disruptive).
- HTTP/2, WebSockets, missing SNI / ECH, and long streams.
- QUIC/UDP 443 and DNS (both ungoverned, as today).
- Orca's support for a ProxyJump SSH target.
- Multi-hour stability, and more than two agents.
