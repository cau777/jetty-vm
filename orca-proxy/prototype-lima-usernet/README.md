# PROTOTYPE — Lima user-mode network with transparent egress redirect

Throwaway. Answers one question: can a VM's 80/443 traffic be forced through a
host-side proxy *transparently* (no proxy env in the guest), without host
iptables writes or a privileged helper, by letting an unprivileged user-mode
network stack decide where each guest connection goes?

How it works:

- Lima 2.2.0 is rebuilt against a patched gvisor-tap-vsock v0.8.9 (`gvisor-tap-vsock-v0.8.9.patch`).
- The guest's only NIC is a Lima `user-v2` network served by that patched stack.
- Every guest TCP connection already ends in this host process. The patch sends 80/443 to an explicit mitmproxy as `CONNECT <original-dst>`, tagged with the VM identity.
- It refuses anything that would NAT to host loopback, and drops UDP 80/443 (QUIC).
- `jetty_addon.py` is a stand-in for orca-proxy's rule engine. It does not use the real addon.

```
./run.sh        # downloads Go/Lima/QEMU debs into ~/.cache/jetty-lima-proto, builds, boots, runs checks.sh
./run.sh down
```

The work dir must have a short path: Lima's socket paths hit `UNIX_PATH_MAX`.

## Verdict (2026-10-02): works

All 12 `checks.sh` checks passed:

- HTTPS and HTTP go through the proxy with no proxy env in the guest. Boot-time snapd and apt traffic was captured too.
- The credential is injected only on the matching host and path.
- Guest root adding a second IP, or changing MAC and IP, still goes through the proxy.
- SNI-less HTTPS to a bare IP is still forced through the proxy.
- The guest can't reach host loopback through the gateway NAT (orca-proxy's Management API on :8080, sshd on :22).
- Non-web TCP still flows, matching today's semantics.
- There is no IPv6 path.
- With the proxy down, connections fail rather than going direct.
- The usernet process runs as the user with all capability sets at 0.

## Findings

- **Stock Lima `user-v2` exposes all of the host's loopback services to the guest.** It NATs the gateway IP (`.2`) to host `127.0.0.1` (`pkg/networks/usernet/gvproxy.go`). This is from reading the source. The empirical check against unpatched Lima was interrupted by a host crash, an unrelated NVIDIA Xid 62 that happened before the VM booted.
- **`plain: true` is required.** Otherwise Lima mounts the host home directory into the guest.
- **One usernet process per network name gives an unspoofable VM identity**, but that process reads `JETTY_*` from whatever environment first started it.
- **Cost of this route:** maintaining forks of Lima and gvisor-tap-vsock; QEMU only on Linux; host SSH goes through a forwarded localhost port.
