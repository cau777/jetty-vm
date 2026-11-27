# Unified VM Credential-Proxy

A single-host service that governs outbound HTTP(S) access for registered LXD VMs and selectively injects credentials.

## Language

**Management API**:
The ConnectRPC/gRPC h2c interface (`jettyd` on `127.0.0.1:7700` for host clients, and `10.201.0.1:7700` with peer-IP and bearer-token auth for the Gateway) through which the `jetty` CLI, `jetty-desktop`, and `jetty-gw-agent` interact with Jetty. It is not reachable from registered VMs.
_Avoid_: Proxy API, VM API

**Gateway Agent**:
The transparent `mitmproxy` service (`jetty-gw-agent`) running inside the Gateway VM. It intercepts redirected TCP 80/443 from registered VMs via local `iptables REDIRECT` and asks `jettyd` for per-connection and per-request decisions over `10.201.0.1:7700`. It stores no policy, credentials, or logs.
_Avoid_: Host proxy, WireGuard listener

**Gateway**:
The trusted LXD VM (`jetty-gw`) that is the only route out of the private agent network. It redirects agent TCP 80/443 to `jetty-gw-agent` locally without rewriting source addresses, and NATs other permitted non-private traffic.
_Avoid_: Router, proxy VM

**VM**:
A registered LXD agent guest, identified by its immutable unique instance name, whose outbound HTTP(S) traffic is governed by the service. A VM is a traffic source, not a management client.
_Avoid_: Client

**Provisioning Agent**:
The trusted host-side agent that calls the Management API while provisioning and configuring VMs.
_Avoid_: Provisioning Client

**Untrusted Agent**:
A coding agent running with sudo inside a registered VM. It has no management authority or direct access to host-held credentials.
_Avoid_: VM agent, Provisioning Agent

**Credential**:
A bash command identified by an immutable unique name that produces a Credential Value. The command owns acquisition and refresh; the proxy only TTL-caches the value in memory.
_Avoid_: Stored secret, Provider credential

**Credential Value**:
The validated secret emitted by a Credential command and held only in memory for injection into matching requests.
_Avoid_: Command output, Token

**Quick Add**:
A Web UI template that creates an ordinary Credential with a tested, prefilled bash command loaded from the application's template catalog. Compatibility tests exercise that same command string; Quick Add adds no provider-specific runtime behavior.
_Avoid_: Built-in Credential, Provider Credential

**Interception CA**:
The service-wide signing identity used to mint certificates for Allow-with-credential connections. Registered VMs trust its public certificate system-wide; its private key remains on the host.
_Avoid_: VM CA, mitmproxy CA

**Rule**:
A policy belonging to one VM or to the global scope, associating a hostname match (exact, wildcard subdomains, or AWS endpoints) with an Allow, Block, or Allow-with-credential action. Names and priorities are unique within each scope; a VM’s rules run first, followed by global rules, with lower priorities first within each scope.
_Avoid_: Allowlist entry, proxy policy

**AWS endpoint selector**:
A hostname match selecting AWS endpoint domains across regions and partitions, including global services. An Allow-with-credential Rule using this selector authenticates AWS API requests with its Credential; IAM permissions govern which operations are allowed.
_Avoid_: All-host wildcard, AWS permissions

**Allow-with-credential rule**:
A terminal Rule whose hostname selects TLS interception and whose normalized, segment-boundary-aware path prefix limits which requests receive its configured credential. Requests to other paths on that intercepted hostname remain credential-free.
_Avoid_: Inject rule, Credential rule

**VM selector**:
The Rule field that identifies exactly one registered VM, or includes every current and future registered VM for a global Rule.
_Avoid_: Origin, client IP

**Port Forward**:
A host loopback port tunneled to a port inside one VM, identified by its host port. One-time forwards last until closed or the daemon stops; persistent forwards reopen whenever the daemon starts. A Port Forward is host ingress into a VM and is independent of Rules, which govern VM egress.
_Avoid_: Port mapping, exposed port

**Reverse Forward**:
A host loopback port made reachable on a loopback port inside one VM, identified by that VM and VM port. It is carried over the host's SSH connection to the VM, so the Gateway still drops VM traffic to host and private addresses. Persistence works as for a Port Forward. It is independent of Rules and grants the Untrusted Agent whatever access the host service grants.
_Avoid_: Host share, remote forward
