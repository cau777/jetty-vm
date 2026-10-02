# Unified VM Credential-Proxy

A single-host service that governs outbound HTTP(S) access for registered LXD VMs and selectively injects credentials.

## Language

**Management API**:
The loopback-only interface through which host-side tools and the Web UI manage VMs, Credentials, Rules, and logs. It is not reachable from registered VMs.
_Avoid_: Proxy API, VM API

**Proxy Listener**:
The WireGuard tunnel endpoint (mitmdump's userspace WireGuard mode) that receives forced outbound traffic from registered VMs via the Gateway. It grants no management authority.
_Avoid_: Management API

**Gateway**:
The trusted LXD VM (`jetty-gw`) that is the only route out of the private agent network. It sends agent TCP 80/443 to the Proxy Listener without rewriting source addresses, and NATs other permitted traffic.
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
A policy identified by an immutable unique name that associates a VM selector and exact hostname match with an Allow, Block, or Allow-with-credential action. Every Rule has a globally unique priority; lower numbers are evaluated first.
_Avoid_: Allowlist entry, proxy policy

**Allow-with-credential rule**:
A terminal Rule whose hostname selects TLS interception and whose normalized, segment-boundary-aware path prefix limits which requests receive its configured credential. Requests to other paths on that intercepted hostname remain credential-free.
_Avoid_: Inject rule, Credential rule

**VM selector**:
The Rule field that identifies one or more registered VMs, or uses the exclusive `*` wildcard to include every current and future registered VM.
_Avoid_: Origin, client IP
