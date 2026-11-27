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
| Host (CLI, desktop, the user) | yes | `127.0.0.1:7700` |
| Gateway VM | yes | `10.201.0.1:7700`, the same API |
| Agent VMs | no | never: the gateway drops their traffic to private ranges |

There is one API with no authentication. Whoever can reach it is trusted, and
the network topology decides who can reach it. That's how the loopback
Management API works today; this extends it by one bridge.

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
as the daemon waits for the tunnel address today.

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

If `jettyd` is written in Go, the CLI and the daemon can be one static binary
(`jetty daemon` starts the server). That replaces the AppImage and the Python
runtime on the host.

### Desktop client

A separate app that uses the same API. It subscribes to a `Watch` stream
instead of polling. There is no tray. Closing the window quits the app, and
egress keeps working because egress never depended on the UI.

### Gateway agent (`jetty-gw-agent`)

A Python program in the gateway VM, about 300 lines, running mitmproxy in
`transparent` mode with one addon. It is today's `proxy_addon.py` with every
decision replaced by an RPC:

- `load`: call `GetCa` and write the CA into mitmproxy's confdir, then call
  `Hello` to check versions.
- `client_connected` / `tls_clienthello`: call `DecideConnection`. If the
  answer is passthrough, set `ignore_connection`. If it is block, kill the
  connection. If it is intercept, continue to MITM.
- `request`: call `DecideRequest`. Then forward unchanged, forward with the
  header edits it was given (the injected credential), or answer with the
  synthesized 403 or 502.
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
```

Agent traffic to private ranges, including `10.201.0.1`, is still dropped in
`FORWARD` as it is today, so agents can't reach `jettyd`. mitmproxy only dials
the original destination, which the redirect limits to public addresses.

## Protocol

I recommend gRPC with protobuf, served through ConnectRPC. A Connect server
speaks gRPC, gRPC-Web and a JSON-over-HTTP variant from the same handlers.
That leaves the desktop free to be a web-tech app, and `curl` still works for
debugging, without keeping a hand-written HTTP API.

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
  rpc DecideConnection(ConnectionInfo) returns (ConnectionDecision);
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
  oneof action { Passthrough passthrough = 2; Block block = 3; Intercept intercept = 4; }
}
message RequestInfo {
  string connection_id = 1;
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

Everything is plain TCP on port 7700, with no auth and no TLS:

- **Host clients:** `127.0.0.1:7700`.
- **Gateway:** `10.201.0.1:7700`, the host's address on `jettyup0`. Only the
  gateway is attached to that bridge, and the address is not on the LAN.
- **No WireGuard.** It exists only to carry agent traffic into the host's
  mitmproxy while keeping the source IP. With MITM in the gateway, mitmproxy
  sees agent source IPs directly on `jettypriv0`. That removes `tunnel.py`,
  the key file, the log-line filter, MSS clamping and the policy-routing
  table.

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

- Passthrough traffic (most of it) costs one RPC per TCP connection, on a
  virtual bridge, with sub-millisecond latency. Its bytes no longer cross the
  host.
- Intercepted traffic adds one RPC per HTTP request.
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
   `tests/e2e/lxd-gateway-checks.sh` against it.
2. **Remove mitmproxy from the host** and delete WireGuard.
3. **Add the rest of the service** next to the HTTP API. Move the CLI over,
   then move the skill from `curl` to the CLI.
4. **Remove the HTTP API and the tray.**
5. **Rewrite `jettyd` in the chosen language** against the same proto. Use
   the Python test suite (rule engine, validation, redaction, credential
   cache) as golden cases, and add contract tests that run against both
   implementations.
6. **Rewrite the desktop client.**

After step 4, every part talks only through the proto, so steps 5 and 6 can be
done in any order, or never.

## Open decisions

1. **Language for `jettyd`.** I recommend Go. LXD's own client is Go
   (`canonical/lxd/client`), gRPC/Connect support is mature, CLI and daemon fit
   in one static binary, and Go's crypto library covers the CA. Rust also works
   but needs more code for LXD.
2. **Desktop toolkit.** Tauri (TS over Connect-Web) or a Go-native toolkit.
   This depends on how much of the current QML is worth keeping.
3. **Gateway agent upgrades.** `jettyd` pushes the agent at `SetupGateway` and
   refuses `Hello` on a version mismatch. Should a mismatch trigger an
   automatic re-push, or require `jetty gateway setup`?
4. **mitmproxy in the gateway.** Install it from pinned wheels at gateway setup
   (needs gateway egress at setup time) or bake it into a Jetty gateway image.
