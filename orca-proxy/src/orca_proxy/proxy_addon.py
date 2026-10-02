"""The mitmproxy addon: transparent SNI-based routing + selective MITM +
credential injection (design tickets #3/#4/#5), wired to the rule engine,
Credential execution/caching, and request logging built in earlier slices.

Run via `mitmdump --mode wireguard:<keys>@<host>:<port> -s proxy_addon.py`.
Agent VMs reach this process only through the Jetty gateway VM, which sends
their TCP 80/443 down that WireGuard tunnel without rewriting source
addresses (deploy/jetty-lxd, deploy/gateway/). mitmproxy's WireGuard mode
hands each stream to its transparent layer with the VM's own source address
as the peer and the original destination as the server address, so this
addon never talks TCP/TLS itself; it only makes policy decisions and does
the SQLite/exec work around them.

Also starts the Management API (app.create_app()) on mitmdump's own asyncio
loop via the running()/done() addon hooks, satisfying #4's "same process"
decision — `mitmdump -s proxy_addon.py` is the entire deployed service, one
process, no separate aiohttp process to run or supervise. The embedded
aiohttp app opens its own state.sqlite/requests.sqlite connections separate
from this addon's own (below) — a small, deliberate duplication rather than
threading one set of connections through two independently-hookable
lifecycles; SQLite's WAL mode makes concurrent connections to the same file
safe, so nothing correctness-sensitive depends on avoiding it.
"""

import base64
import time
from urllib.parse import parse_qsl, urlparse

from aiohttp import web
from mitmproxy import http, tls

# Absolute imports, not package-relative: mitmdump loads this file as a
# standalone script (`mitmdump -s proxy_addon.py`), which does not preserve
# package context for relative imports. This resolves correctly as long as
# mitmdump runs inside the same locked venv orca-proxy is installed into
# (`uv run mitmdump -s ...`), per #4/#12's deployment model.
from orca_proxy import config, db, request_log, rule_engine, tunnel
from orca_proxy.app import create_app
from orca_proxy.credential_exec import CredentialCache, CredentialExecutionError
from orca_proxy.redaction import redact_headers
from orca_proxy.repo import credentials as credentials_repo
from orca_proxy.repo import rules as rules_repo
from orca_proxy.repo import vms as vms_repo

ECH_EXTENSION_TYPE = 0xFE0D  # RFC 9849 §6.2 / IANA-assigned codepoint 65037, per research/ech-missing-sni-containment.md

BLOCK_MESSAGE = b"blocked by orca-proxy policy"

# Connection outcome for a source address no registered VM owns. Every agent
# on the private network can reach the tunnel, so an unregistered one must
# not fall through to wildcard rules (and their credentials) as "".
BLOCK_UNKNOWN_VM = "block_unknown_vm"
PLAINTEXT_CREDENTIAL_MESSAGE = b"blocked by orca-proxy: credential rules only apply over TLS"


class OrcaProxyAddon:
    def __init__(self) -> None:
        # Deliberately no I/O here — mitmproxy constructs `addons = [...]`
        # at *import* time, which would otherwise open real database
        # connections (against the default ~/.orca-proxy!) on a bare
        # `import orca_proxy.proxy_addon`, including from test collection.
        # Real setup happens in load(), which mitmproxy's AddonManager
        # calls once when the addon is actually registered.
        self._state_conn = None
        self._requests_conn = None
        self._log = None
        self._credentials = CredentialCache()
        self._api_runner: web.AppRunner | None = None

    def load(self, loader) -> None:
        # Before mitmdump starts its WireGuard listener (running()): owner-only
        # keys, and never log the client private key mitmproxy prints.
        tunnel.install_log_filter()
        tunnel.ensure_keys(config.tunnel_keys_path())
        self._state_conn = db.connect(config.db_path())
        # Idempotent — safe whether or not the aiohttp process already
        # migrated this database (mitmdump and the Management API are
        # independent OS processes that may start in either order).
        db.migrate(self._state_conn)
        self._requests_conn = request_log.connect(config.requests_db_path())
        self._log = request_log.RequestLog(self._requests_conn)

    async def running(self) -> None:
        # Fires once mitmproxy's own proxy server is up — this is where the
        # embedded Management API starts, on the same already-running loop.
        self._api_runner = web.AppRunner(create_app(credential_cache=self._credentials))
        await self._api_runner.setup()
        site = web.TCPSite(self._api_runner, "127.0.0.1", config.management_api_port())
        await site.start()

    async def done(self) -> None:
        if self._api_runner is not None:
            await self._api_runner.cleanup()

    # --- shared lookups (fresh-read each time — simplest correct v1
    # behavior; the DB is local SQLite, so this isn't the bottleneck it
    # would be against a network-hop store) ---

    def _rules(self) -> list[dict]:
        return [rules_repo.to_dict(r) for r in rules_repo.list_all(self._state_conn)]

    def _vm_name_for_ip(self, ip: str | None) -> str | None:
        if ip is None:
            return None
        for row in vms_repo.list_all(self._state_conn):
            if row["ip_address"] == ip:
                return row["name"]
        return None

    def _rule_snapshot(self, rules: list[dict], name: str | None) -> dict | None:
        if name is None:
            return None
        for rule in rules:
            if rule["name"] == name:
                return {"name": rule["name"], "priority": rule["priority"], "action_type": rule["action"]["type"]}
        return None

    # --- connection-level: tls_clienthello (design ticket #5 steps 1-4, #14's intercepted/outcome split) ---

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        client = data.context.client
        sni = data.client_hello.sni
        sni_present = sni is not None
        ech_present = any(ext_type == ECH_EXTENSION_TYPE for ext_type, _ in data.client_hello.extensions)

        vm_ip = client.peername[0] if client.peername else None
        vm_name = self._vm_name_for_ip(vm_ip)

        if vm_name is None:
            # client_connected() already refused this source; never let it
            # reach rule evaluation as "" (which wildcard selectors match).
            data.ignore_connection = False
            client.error = "blocked by orca-proxy: source is not a registered VM"
            return

        rules = self._rules()
        # No-SNI collapses matching to a value no hostname Rule can ever
        # equal, per the ECH/missing-SNI survey (research/ech-missing-sni-containment.md)
        # — this deterministically falls through to default-Allow rather
        # than crashing or guessing.
        match_hostname = sni if sni_present else ""

        decision = rule_engine.evaluate_connection(rules, vm_name or "", match_hostname)

        server_address = data.context.server.address
        destination_ip, destination_port = server_address if server_address else ("0.0.0.0", 0)

        connection_id = self._log.log_connection(
            started_at=db.now_iso(),
            vm_name=vm_name,
            destination_ip=destination_ip,
            destination_port=destination_port,
            destination_hostname=sni,
            sni_present=sni_present,
            ech_present=ech_present,
            duration_ms=None,
            intercepted=decision.intercepted,
            outcome=decision.outcome,
            matched_rule=self._rule_snapshot(rules, decision.matched_rule),
            intercepted_by_rule=self._rule_snapshot(rules, decision.intercepted_by_rule),
        )

        client.orca_connection_id = connection_id
        client.orca_vm_name = vm_name
        client.orca_connect_started = time.monotonic()
        # Per-request matching (request(), below) must key off *this* value,
        # not the client-supplied Host header — otherwise a VM can open a
        # TLS connection whose SNI matches one Allow-with-credential rule
        # then send an HTTP request with a different Host, causing that
        # request to match a *different* rule while still riding the first
        # rule's already-established upstream TLS session — leaking that
        # second rule's credential to the first rule's real upstream host.
        client.orca_sni = sni if sni_present else None

        if decision.intercepted:
            return  # proceed with normal certificate-swap MITM

        if decision.outcome == rule_engine.BLOCK_RULE:
            # Killing the connection here (mitmproxy's own Block addon uses
            # the same client.error mechanism) happens before any
            # interception certificate is ever issued.
            client.error = "blocked by orca-proxy policy"
            return

        # allow_default / allow_rule: pass the raw bytes through untouched,
        # zero CA trust required (research/transparent-mitm-passthrough.md).
        data.ignore_connection = True

    def client_connected(self, client) -> None:
        # The only hook where client.error kills the connection outright (in
        # tls_clienthello it is ignored and the flow goes on to MITM). Every
        # agent can reach the tunnel, so a source no registered VM owns is
        # refused here, before TLS or HTTP of any kind, and logged.
        vm_ip = client.peername[0] if client.peername else None
        vm_name = self._vm_name_for_ip(vm_ip)
        client.orca_vm_name = vm_name
        if vm_name is not None:
            return
        destination_ip, destination_port = client.sockname if client.sockname else ("0.0.0.0", 0)
        client.orca_connection_id = self._log.log_connection(
            started_at=db.now_iso(),
            vm_name=vm_ip or "unknown",
            destination_ip=destination_ip,
            destination_port=destination_port,
            destination_hostname=None,
            sni_present=False,
            ech_present=False,
            duration_ms=None,
            intercepted=False,
            outcome=BLOCK_UNKNOWN_VM,
            matched_rule=None,
            intercepted_by_rule=None,
        )
        client.orca_connect_started = time.monotonic()
        client.error = "blocked by orca-proxy: source is not a registered VM"

    def client_disconnected(self, client) -> None:
        connection_id = getattr(client, "orca_connection_id", None)
        started = getattr(client, "orca_connect_started", None)
        if connection_id is not None and started is not None:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._requests_conn.execute(
                "UPDATE connections SET duration_ms = ? WHERE id = ?", (duration_ms, connection_id)
            )

    # --- per-request: request/response (design ticket #5 steps 4-8) ---

    async def request(self, flow: http.HTTPFlow) -> None:
        client = flow.client_conn
        vm_name = getattr(client, "orca_vm_name", None)
        if vm_name is None:
            # Unregistered source (client_connected() normally refuses it).
            flow.kill()
            return
        # Plain HTTP never passes tls_clienthello, so it has no connection row.
        plaintext = getattr(client, "orca_connection_id", None) is None
        # Use the SNI that authorized interception at tls_clienthello time,
        # not flow.request.pretty_host (the client-supplied Host header) —
        # the latter is attacker-controlled and can diverge from the SNI on
        # an already-intercepted connection, letting one rule's credential
        # get sent to a different rule's real upstream host. Only an
        # SNI-less connection (already collapsed to default-Allow at the
        # connection level, so never actually intercepted) falls back.
        hostname = getattr(client, "orca_sni", None) or flow.request.pretty_host
        raw_path = flow.request.path
        path = urlparse(raw_path).path
        query_keys = [k for k, _ in parse_qsl(urlparse(raw_path).query)]

        rules = self._rules()
        decision = rule_engine.evaluate_request(rules, vm_name, hostname, path)

        flow.metadata["orca_started"] = time.monotonic()
        flow.metadata["orca_query_keys"] = query_keys
        flow.metadata["orca_decision"] = decision
        flow.metadata["orca_injected_header"] = None
        flow.metadata["orca_credential_name"] = None

        if decision.outcome == rule_engine.BLOCK_RULE:
            flow.response = http.Response.make(403, BLOCK_MESSAGE, {"Content-Type": "text/plain"})
            self._log_request(flow, status=403, status_origin="proxy")
            flow.metadata["orca_logged"] = True
            return

        if decision.outcome == rule_engine.ALLOW_CREDENTIAL and plaintext:
            # The Host header is the only hostname on plain HTTP and the guest
            # chooses it, along with the destination IP: injecting here would
            # send the credential wherever the guest likes, in cleartext.
            flow.response = http.Response.make(403, PLAINTEXT_CREDENTIAL_MESSAGE, {"Content-Type": "text/plain"})
            return

        if decision.outcome == rule_engine.ALLOW_CREDENTIAL:
            credential_row = credentials_repo.get(self._state_conn, decision.credential)
            try:
                value = await self._credentials.get_value(
                    decision.credential, credential_row["command"], credential_row["ttl_seconds"]
                )
            except CredentialExecutionError:
                flow.response = http.Response.make(
                    502,
                    b'{"error":{"code":"credential_unavailable","message":"credential command failed"}}',
                    {"Content-Type": "application/json"},
                )
                self._log_request(flow, status=502, status_origin="proxy")
                flow.metadata["orca_logged"] = True
                return

            injection = decision.injection
            header_name = "Authorization"
            if injection["type"] == "bearer":
                flow.request.headers[header_name] = f"Bearer {value}"
            elif injection["type"] == "basic":
                basic = base64.b64encode(f"{injection['username']}:{value}".encode()).decode()
                flow.request.headers[header_name] = f"Basic {basic}"
            flow.metadata["orca_injected_header"] = header_name
            flow.metadata["orca_credential_name"] = decision.credential

        # allow_default / allow_rule / successfully-injected allow_credential:
        # forward to the real upstream; response() logs once status is known.

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("orca_logged"):
            return
        status = flow.response.status_code if flow.response else None
        self._log_request(flow, status=status, status_origin="upstream")
        flow.metadata["orca_logged"] = True

    def error(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("orca_logged"):
            return
        self._log_request(flow, status=None, status_origin=None)
        flow.metadata["orca_logged"] = True

    def _log_request(self, flow: http.HTTPFlow, status: int | None, status_origin: str | None) -> None:
        client = flow.client_conn
        connection_id = getattr(client, "orca_connection_id", None)
        if connection_id is None:
            return

        decision: rule_engine.RequestDecision = flow.metadata["orca_decision"]
        started = flow.metadata.get("orca_started")
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else None

        injected_header = flow.metadata.get("orca_injected_header")
        credential_name = flow.metadata.get("orca_credential_name")
        headers = redact_headers(
            list(flow.request.headers.items(multi=True)), injected_header, credential_name
        )

        rules = self._rules()
        self._log.log_http_request(
            connection_id=connection_id,
            occurred_at=db.now_iso(),
            method=flow.request.method,
            path=urlparse(flow.request.path).path,
            query_keys=flow.metadata.get("orca_query_keys", []),
            status=status,
            status_origin=status_origin,
            latency_ms=latency_ms,
            outcome=decision.outcome,
            matched_rule=self._rule_snapshot(rules, decision.matched_rule),
            matched_credential=credential_name,
            trace=[
                {
                    "rule_name": t.rule_name,
                    "priority": t.priority,
                    "action_type": t.action_type,
                    "result": t.result,
                }
                for t in decision.trace
            ],
            headers=headers,
        )


addons = [OrcaProxyAddon()]
