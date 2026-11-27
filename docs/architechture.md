# Centralized architecture

Status: draft. Open decisions are listed at the end.

## Motivation

- Jetty has too many moving parts today. A new feature starts with "is this
  part of the proxy, the daemon, the tray, the desktop, `jetty-lxd`, or the
  skill?"
- Those parts are tightly coupled, and that coupling forces all of them to be
  written in Python.

What we have today:

| Part | Where it runs | What it owns |
|---|---|---|
| `jetty daemon` | host, user service | aiohttp Management API, LXD calls, jobs, SSH port forwards, SQLite |
| mitmproxy + `proxy_addon.py` | host, **inside the daemon process** | WireGuard listener, TLS interception, rule evaluation, credential injection, request log |
| `jetty tray` + QML desktop | host, separate process | UI that polls about 10 HTTP endpoints |
| `jetty` CLI | host | HTTP client for part of the API |
| `orca-ssh-setup` skill | host agent | `curl` against the HTTP API for rules and credentials, the CLI for everything else |
| `deploy/jetty-lxd` | host | bash copy of the LXD lifecycle that `jetty.py` already does |
| gateway VM | LXD VM | iptables, policy routing, WireGuard client (bash) |

The coupling comes from mitmproxy. The proxy is a Python addon inside the
daemon's event loop, so the rule engine, credential cache, CA and request log
all have to be Python objects in that process. Every other part then grows
around that.

## Trust model

| Zone | Trusted? | How it reaches `jettyd` |
|---|---|---|
| Host (CLI, desktop, the user) | yes | `127.0.0.1:7700`, no auth |
| Gateway VM | yes | `10.201.0.1:7700`, the same API, from `10.201.0.2` with a bearer token |
| Agent VMs | no | never: the gateway drops their traffic to private ranges |

There is one API. On loopback it has no authentication, as the Management API
does today. The `10.201.0.1` listener needs more, because the topology alone
doesn't limit who can reach it. Linux accepts a packet for any of its own
addresses on any interface, so a LAN neighbour, a Docker container or another
LXD instance could reach `10.201.0.1` by routing `10.201.0.0/24` via the host.
That listener therefore:

- accepts TCP connections only from the gateway's static uplink address
  `10.201.0.2`. Replies to that address are routed onto `jettyup0`, so
  another machine can't complete a handshake with a spoofed source.
- requires a bearer token on every call. `jettyd` generates it at
  `SetupGateway` and pushes it into a root-only file in the gateway through
  the LXD file API, the way the WireGuard client key is pushed today.

**Change from today:** the API returns secrets to trusted callers.
`GetCa(include_private_key)` returns the CA key, and `DecideRequest` returns
Credential Values inside header edits. Today no API ever returns a value
(`CONTEXT.md`, `design.md`). The host is trusted and can run the credential
commands anyway, so this only matters for the `10.201.0.1` listener, which is
why that listener has the checks above.

## Idea

- One **central process**, `jettyd`, owns all state and all logic. Nothing
  else stores anything: every other part is a client that knows how to call
  it.
- It speaks one typed protocol over TCP, defined once as a `.proto` file.
  Clients in any language are generated from it.
- The **CLI** is the only way agents reach Jetty. It calls procedures on
  `jettyd` and adds nothing of its own.
- The **desktop client** can be rewritten in another language and uses the
  same API.
- The **gateway VM** runs a small Python program: transparent mitmproxy plus
  a client for `jettyd`. For each new connection, and for each request on an
  intercepted connection, it asks "what do I do with this?" and does what it
  is told.

```text
 jetty CLI ──┐   127.0.0.1:7700         ┌──────────┐    10.201.0.1:7700     ┌─────────────────────────┐
 desktop ────┼─────────────────────────▶│ jettyd   │◀───────────────────────│ gateway VM              │
 (skill uses │                          │ (host)   │   same API             │  iptables REDIRECT       │
  the CLI)   ┘                          │          │                        │  mitmproxy transparent   │
                                        │ SQLite   │                        │  jetty-gw-agent (Python) │
                                        │ LXD API  │                        └───────────▲─────────────┘
                                        │ cred exec│                                    │ tcp/80,443
                                        │ CA       │                          agent VMs ┘
                                        │ ssh -L/-R│
                                        └──────────┘
```

## Components

### `jettyd`, the central process

It is the only process that writes state. It owns:

- `state.sqlite` and `requests.sqlite`, with the schemas and migrations as they
  are today.
- The rule engine. `rule_engine.py` is already pure and I/O-free, so it ports
  over as-is, with its tests as the spec.
- Credential execution and the single-flight TTL cache (`credential_exec.py`).
  Credential commands keep running on the host, as the user. That is how
  interactive provider logins stay on the host.
- The Interception CA. It is generated and stored here, and the gateway
  fetches it at startup.
- LXD lifecycle through the Unix-socket REST API (LXD's own interface, not
  ours): gateway setup, VM create, delete, start, stop, exec and upload. This
  replaces both `jetty.py` and `deploy/jetty-lxd`.
- Jobs. Long operations become server-streaming RPCs that send step events,
  so clients stop polling `/jobs/{id}`.
- Port and reverse forwards (the `ssh -N -L/-R` supervisor).
- `~/.ssh/jetty_config` generation.

It runs as a systemd user service, as `jetty-daemon.service` does now, and
listens on `127.0.0.1:7700` and `10.201.0.1:7700`. The second address exists
only once the Jetty uplink bridge does, so `jettyd` binds it when it appears,
as the daemon waits for the tunnel address today. The peer-address and token
checks from [Trust model](#trust-model) apply to that listener only.

### `jetty` CLI

A thin client. Every subcommand maps to one RPC, and `--json` prints the
response message as-is. To replace the skill's `curl` calls, the CLI needs
commands for everything the HTTP API covers today:

```text
jetty status
jetty gateway setup
jetty vm create|delete|start|stop|exec|upload|list|ip ...
jetty vm port open|close|list ...
jetty vm reverse-port open|close|list ...
jetty rule list [--vm NAME | --global]
jetty rule put NAME (--vm NAME | --global) --file rule.json    # or flags
jetty rule delete NAME (--vm NAME | --global)
jetty credential list|get|put|delete|refresh ...
jetty ca show
jetty log list [--vm NAME] [--decision ...] [--follow]
jetty log show ID
```

The CLI and the daemon are one static Go binary (`jetty daemon` starts the
server). That replaces the AppImage and the Python runtime on the host.

### Desktop client (`jetty-desktop`)

A [Wails](https://wails.io) app: a Go backend with a TypeScript web frontend,
rendered in the system webview (WebKitGTK on Linux).

- The Go side imports the same generated `connect-go` client as the CLI and
  calls `jettyd` on `127.0.0.1:7700`. The frontend calls only the Go
  functions Wails binds for it, so there is one protocol client in the
  codebase.
- `Watch` and job event streams are relayed to the frontend as Wails events,
  replacing today's polling of about 10 endpoints.
- The dense screens (log inspector, rule editor drawer, VM dashboard) are
  ordinary web UI. None of the QML carries over.
- It is a separate binary from `jetty`, because Wails needs cgo and WebKitGTK
  and would break the static CLI-and-daemon build. It ships as its own
  package.
- Ubuntu 24.04 ships `webkit2gtk-4.1`, which Wails v2 uses through a build
  tag (`webkit2_41`, to confirm). Check whether Wails v3 is stable before
  starting.

There is no tray. Closing the window quits the app, and egress keeps working
because egress never depended on the UI.

### Gateway agent (`jetty-gw-agent`)

A Python program in the gateway VM, about 300 lines, running mitmproxy in
`transparent` mode with one addon. It is today's `proxy_addon.py` with every
decision replaced by an RPC:

- `load`: read the token, call `GetCa` and write the CA into mitmproxy's
  confdir, then call `Hello` to check versions.
- `client_connected`: call `ResolveSource`. If no registered VM owns the
  source IP, set `client.error`. This is the only hook where `client.error`
  kills a connection outright, and the SNI isn't known yet here.
- `tls_clienthello`: call `DecideConnection` with the SNI. If the answer is
  passthrough, set `ignore_connection`. Otherwise continue to MITM.
  mitmproxy ignores `client.error` in this hook, so Block can't refuse the
  connection here. As today, a Block match is intercepted and every request
  on it gets a 403, which is why Block needs the CA installed in the VMs.
- `request`: call `DecideRequest`. Then forward unchanged, forward with the
  header edits it was given (the injected credential), or answer with the
  synthesized 403 or 502. Plain HTTP (port 80) never reaches
  `tls_clienthello`, so each port 80 request is decided here from its `Host`
  header alone, as it is today. Credential matches on port 80 are refused
  with 403.
- `response`, `error`, `client_disconnected`: report events with `Report`.
  Reporting is fail-open, as logging is today.

It keeps no rules, credentials or logs. The only state it has is in-flight
connection IDs and the CA it fetched.

`jetty-gateway-up` stays bash. WireGuard and the policy routing are replaced
by a local redirect:

```bash
# agent web traffic to public addresses → local mitmproxy instead of wg0
iptables -t nat -N JETTY_WEB
for net in "${PRIVATE[@]}"; do iptables -t nat -A JETTY_WEB -d "$net" -j RETURN; done
iptables -t nat -A JETTY_WEB -p tcp -j REDIRECT --to-ports 8080
iptables -t nat -A PREROUTING -i "$PRIV" -p tcp -m multiport --dports 80,443 -j JETTY_WEB

# Redirected packets go through INPUT, which today ends with
# `-A INPUT -i "$PRIV" -j DROP`. Accept only connections the REDIRECT
# produced, before that DROP: agents can't open the listener directly.
iptables -A INPUT -i "$PRIV" -p tcp --dport 8080 -m conntrack --ctstate DNAT -j ACCEPT
```

Agent traffic to private ranges, including `10.201.0.1`, is still dropped in
`FORWARD` as it is today, so agents can't reach `jettyd`. Because agents can
reach mitmproxy only through the redirect, its original destination is always
a public address, and that's the only address it dials.

## Protocol

I recommend gRPC with protobuf, served through ConnectRPC. A Connect server
speaks gRPC, gRPC-Web and a JSON-over-HTTP variant from the same handlers.
The CLI, the desktop's Go side and `jettyd` all use the generated
`connect-go` code. `curl` still works for debugging single-request calls,
without keeping a hand-written HTTP API. Streaming calls need a gRPC or
Connect client (`grpcurl`, `buf curl`).

One service, grouped by concern. The gateway's calls are ordinary RPCs on it:

```proto
service Jetty {
  // status and UI
  rpc GetStatus(...)          returns (Status);
  rpc Watch(WatchRequest)     returns (stream Event);     // replaces UI polling

  // VMs and gateway
  rpc ListVms / GetVm / CreateVm(...) returns (stream JobEvent);
  rpc DeleteVm / VmAction / ExecVm(stream) / UploadToVm(stream) ...
  rpc SetupGateway(...)       returns (stream JobEvent);
  rpc OpenPort / ClosePort / ListPorts / OpenReversePort / ...

  // policy
  rpc ListRules / PutRule / DeleteRule ...
  rpc ListCredentials / GetCredential / PutCredential / DeleteCredential / RefreshCredential ...
  rpc GetCa(GetCaRequest)     returns (Ca);               // include_private_key for the gateway

  // logs
  rpc ListRequests / GetRequest / TailRequests(stream) ...

  // traffic decisions, called by the gateway agent
  rpc Hello(GatewayHello)              returns (GatewayConfig);  // version check
  rpc ResolveSource(SourceInfo)        returns (SourceDecision); // client_connected: registered VM?
  rpc DecideConnection(ConnectionInfo) returns (ConnectionDecision);  // tls_clienthello
  rpc DecideRequest(RequestInfo)       returns (RequestDecision);
  rpc Report(stream GatewayEvent)      returns (Empty);
}

message ConnectionInfo {
  string source_ip = 1;      // agent VM address, used to look up the VM
  string dest_ip = 2;
  uint32 dest_port = 3;
  string sni = 4;            // empty if absent
  bool ech_present = 5;
}
message ConnectionDecision {
  string connection_id = 1;  // jettyd's log row ID, echoed back on later calls
  // Block can't refuse the connection in tls_clienthello, so it is an
  // intercept whose requests all get 403 from DecideRequest.
  oneof action { Passthrough passthrough = 2; Intercept intercept = 3; }
}
message RequestInfo {
  string connection_id = 1;  // empty for plain HTTP; jettyd then decides by source_ip + host
  string source_ip = 7;
  string method = 2;
  string host = 3;
  string path = 4;
  repeated Header headers = 5;  // for AWS signing; jettyd redacts before logging
  bytes body_sha256 = 6;        // SigV4 signs the payload hash, not the body
}
message RequestDecision {
  oneof action {
    Forward forward = 1;           // header set/remove edits, may be empty
    Synthesize respond = 2;        // 403 block, 403 plaintext-credential, 502 credential_unavailable
  }
  string request_id = 3;
}
```

The gateway sends the body hash, not the body. Every injection type today
edits only headers, and SigV4 needs only `method`, URL, headers and the
payload hash (`aws_signing.py` already sets `X-Amz-Content-SHA256`).

## Transport

Everything is plain TCP on port 7700, with no TLS:

- **Host clients:** `127.0.0.1:7700`, no auth.
- **Gateway:** `10.201.0.1:7700`, the host's address on `jettyup0`. It
  accepts only peer `10.201.0.2` with the gateway token (see
  [Trust model](#trust-model)).
- **HTTP/2 without TLS (h2c).** `Watch`, job events, `TailRequests` and the
  gateway's Python gRPC client need HTTP/2, and bidirectional streams such as
  `ExecVm` don't work over HTTP/1.1 at all. Go's `net/http` serves h2c only
  when configured (`http.Server.Protocols` with unencrypted HTTP/2, or
  `golang.org/x/net/http2/h2c`).
- **No WireGuard.** It exists only to carry agent traffic into the host's
  mitmproxy while keeping the source IP. With MITM in the gateway, mitmproxy
  sees agent source IPs directly on `jettypriv0`. That removes `tunnel.py`,
  the key file, the log-line filter, MSS clamping and the policy-routing
  table.
- **Host check.** `jettyd` rejects requests whose `Host` header is not
  `127.0.0.1:7700` or `10.201.0.1:7700`, and sends no CORS headers. Without
  this, a web page in the user's browser could reach the unauthenticated
  loopback API through DNS rebinding. Today's aiohttp API has the same
  exposure.

## What stays the same

- **Agents get no authority.** They can't reach `jettyd` and never see
  Credential Values or the CA key.
- **Fail closed.** If `DecideConnection` errors or times out (about 2 s), the
  gateway kills the connection. If `DecideRequest` fails, it answers 502. If
  `jettyd` or the gateway is down, there is no web egress, which keeps today's
  guarantee. `ip_forward=0` until `jetty-gateway-up` completes still applies.
- **Credential Values are cached only in `jettyd`.** The gateway sees a value
  only in the header edit for the request it is forwarding.

One thing improves: mitmproxy parses untrusted agent TLS and HTTP inside the
gateway VM instead of on the host, where the user is in the root-equivalent
`lxd` group.

## Performance

- Each TLS connection costs two RPCs (`ResolveSource`, `DecideConnection`)
  on a virtual bridge, with sub-millisecond latency. Passthrough bytes no
  longer go through a host process, but they still cross the host kernel
  through `jettyup0` and LXD's NAT.
- Intercepted traffic adds one RPC per HTTP request.
- **The gateway now carries all agent web bytes in Python.** Today it is sized
  at 1 vCPU and 512 MiB (`jetty.py`), and that work runs on the host. Under
  this design, mitmproxy's relay in the gateway handles every passthrough
  byte: package installs, image pulls, and intercepted S3 or Bedrock bodies,
  which are buffered whole for the SigV4 hash. Step 1 of the migration
  measures throughput with several agents downloading at once and sizes the
  gateway from that. Intercepted requests get a body size limit.
- If that turns out to be too slow, `jettyd` can return a
  `cache_for_connection` hint so that requests on one connection reuse a
  decision that does not depend on the path. Measure before adding it.

## Compromises I am willing to make

- Remove the HTTP Management API. The skill moves to the CLI, and Connect's
  JSON mode covers debugging.
- Remove the tray.
- Proposed: remove `deploy/jetty-lxd`. `jettyd` owns LXD, and the e2e checks
  call the CLI.
- Proposed: drop the source-install compatibility mode (`mitmdump -s
  proxy_addon.py` with the embedded API).

## Migration plan

The order puts the riskiest change first and keeps the language swap until
the protocol is stable.

1. **Write the `.proto` and add the traffic-decision RPCs to the existing
   Python daemon,** bound on both addresses. Build `jetty-gw-agent` and move
   MITM into the gateway behind a flag. This proves the hard parts:
   per-request RPC, fail closed, and source IPs without WireGuard. Run
   `tests/e2e/lxd-gateway-checks.sh` against it, plus new checks that:
   - agents can't open the gateway's mitmproxy listener directly;
   - a host other than `10.201.0.2`, or a call without the token, is
     refused on `10.201.0.1:7700`;
   - throughput with several agents downloading at once is acceptable, and
     what gateway size that needs.
2. **Remove mitmproxy from the host** and delete WireGuard.
3. **Add the rest of the service** next to the HTTP API. Move the CLI over,
   then move the skill from `curl` to the CLI.
4. **Remove the HTTP API and the tray.**
5. **Rewrite `jettyd` and the CLI in Go** against the same proto. Use the
   Python test suite (rule engine, validation, redaction, credential cache)
   as golden cases, and add contract tests that run against both
   implementations.
6. **Rewrite the desktop client in Wails.** Until then, the QML desktop can
   keep working against the new API through a generated Python client.

After step 4, every part talks only through the proto, so steps 5 and 6 can be
done in any order, or never.

## Decisions

1. **`jettyd` and the CLI are Go.** LXD's official client is Go
   (`canonical/lxd/client`, including exec and file streaming),
   `connect-go` is first-party, the CLI and daemon fit in one static binary,
   and the standard library covers the CA. Rust was considered: it works, but
   it needs a hand-written LXD client, and its Connect support is less
   established. Its main advantage, sharing a backend with a Tauri desktop,
   doesn't apply here.
2. **The desktop is Wails.** It shares the Go client with the CLI and uses web
   UI for the dense screens. Fyne and Gio were considered: they are pure Go
   and static, but rich tables and editors take much more work in them.
   Tauri would add Rust as a second backend language.

## Open decisions

1. **Gateway agent upgrades.** `jettyd` pushes the agent at `SetupGateway` and
   refuses `Hello` on a version mismatch. Should a mismatch trigger an
   automatic re-push, or require `jetty gateway setup`?
2. **mitmproxy in the gateway.** Install it from pinned wheels at gateway setup
   (needs gateway egress at setup time) or bake it into a Jetty gateway image.
