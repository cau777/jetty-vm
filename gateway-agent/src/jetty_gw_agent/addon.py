"""Transparent mitmproxy gateway addon that delegates all policy to jettyd."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import grpc
from mitmproxy import ctx, http, tls

from jetty_gw_agent.proto.jetty.v1 import jetty_pb2, jetty_pb2_grpc
from jetty_gw_agent.version import AGENT_VERSION

log = logging.getLogger("jetty.gw_agent")

ECH_EXTENSION_TYPE = 0xFE0D
DEFAULT_DAEMON_ADDR = "10.201.0.1:7700"
DEFAULT_TOKEN_PATH = "/etc/jetty-gateway/token"
DEFAULT_CONFDIR = "/run/jetty-gw-agent"
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
CONN_RPC_TIMEOUT = 2.0
REQ_RPC_TIMEOUT = 15.0


def read_token() -> str:
    env_token = os.environ.get("JETTY_GATEWAY_TOKEN")
    if env_token:
        return env_token.strip()
    token_path = Path(os.environ.get("JETTY_GATEWAY_TOKEN_FILE", DEFAULT_TOKEN_PATH))
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"Empty gateway token at {token_path}")
    return token


def daemon_addr() -> str:
    return os.environ.get("JETTY_DAEMON_ADDR", DEFAULT_DAEMON_ADDR)


def write_ca_pem(confdir: Path, cert_pem: str, key_pem: str) -> Path:
    confdir.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = confdir / "mitmproxy-ca.pem"
    tmp = confdir / "mitmproxy-ca.pem.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cert_pem)
            if not cert_pem.endswith("\n"):
                handle.write("\n")
            handle.write(key_pem)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)
    return target


def bootstrap_gateway(
    *,
    addr: str | None = None,
    token: str | None = None,
    confdir: Path | None = None,
    agent_version: str = AGENT_VERSION,
) -> jetty_pb2.GatewayConfig:
    """Verify agent version via Hello and materialize the Interception CA."""
    target_addr = addr or daemon_addr()
    auth_token = token if token is not None else read_token()
    meta = (("authorization", f"Bearer {auth_token}"),)
    target_confdir = confdir or Path(os.environ.get("JETTY_CA_CONFDIR", DEFAULT_CONFDIR))

    with grpc.insecure_channel(target_addr) as channel:
        stub = jetty_pb2_grpc.JettyStub(channel)
        cfg = stub.Hello(jetty_pb2.GatewayHello(agent_version=agent_version), metadata=meta, timeout=5.0)
        ca = stub.GetCa(jetty_pb2.GetCaRequest(include_private_key=True), metadata=meta, timeout=5.0)
    if not ca.certificate_pem or not ca.private_key_pem:
        raise RuntimeError("jettyd GetCa returned an incomplete Interception CA")
    write_ca_pem(target_confdir, ca.certificate_pem, ca.private_key_pem)
    return cfg


class JettyGatewayAddon:
    """Stateless transparent mitmproxy addon talking to jettyd over gRPC h2c."""

    def __init__(
        self,
        *,
        stub: Any = None,
        token: str | None = None,
        addr: str | None = None,
        confdir: Path | None = None,
        conn_timeout: float = CONN_RPC_TIMEOUT,
        req_timeout: float = REQ_RPC_TIMEOUT,
        skip_bootstrap: bool = False,
    ) -> None:
        self._injected_stub = stub
        self._token = token
        self._addr = addr
        self._confdir = confdir
        self._conn_timeout = conn_timeout
        self._req_timeout = req_timeout
        self._skip_bootstrap = skip_bootstrap
        self._channel: grpc.aio.Channel | None = None
        self._stub: Any = stub
        self._max_body_bytes = DEFAULT_MAX_BODY_BYTES
        self._report_queue: asyncio.Queue[jetty_pb2.GatewayEvent] | None = None
        self._report_task: asyncio.Task | None = None

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        token = self._token if self._token is not None else read_token()
        return (("authorization", f"Bearer {token}"),)

    def _resolve_confdir(self) -> Path:
        if self._confdir is not None:
            return self._confdir
        try:
            if getattr(ctx, "options", None) and ctx.options.confdir:
                return Path(os.path.expanduser(ctx.options.confdir))
        except Exception:
            pass
        return Path(os.environ.get("JETTY_CA_CONFDIR", DEFAULT_CONFDIR))

    def load(self, loader) -> None:
        if self._skip_bootstrap or self._injected_stub is not None:
            return
        cfg = bootstrap_gateway(
            addr=self._addr,
            token=self._token,
            confdir=self._resolve_confdir(),
        )
        if cfg.max_intercept_body_bytes > 0:
            self._max_body_bytes = cfg.max_intercept_body_bytes
        try:
            tlsconfig = ctx.master.addons.get("tlsconfig")
            if tlsconfig is not None:
                tlsconfig.configure({"confdir"})
        except Exception:
            pass

    async def running(self) -> None:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._addr or daemon_addr())
            self._stub = jetty_pb2_grpc.JettyStub(self._channel)
        self._report_queue = asyncio.Queue(maxsize=4096)
        self._report_task = asyncio.create_task(self._report_loop(), name="jetty-report-loop")

    async def done(self) -> None:
        if self._report_task is not None:
            self._report_task.cancel()
            await asyncio.gather(self._report_task, return_exceptions=True)
            self._report_task = None
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    def _get_stub(self) -> Any:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._addr or daemon_addr())
            self._stub = jetty_pb2_grpc.JettyStub(self._channel)
        return self._stub

    async def _report_loop(self) -> None:
        assert self._report_queue is not None
        while True:
            first = await self._report_queue.get()

            async def event_iter():
                yield first
                while True:
                    try:
                        yield self._report_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        # Keep stream open briefly for batching, or yield next
                        item = await self._report_queue.get()
                        yield item

            try:
                await self._get_stub().Report(event_iter(), metadata=self._metadata())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("fail-open report stream interrupted: %s", exc)
                await asyncio.sleep(0.5)

    def _enqueue_report(self, event: jetty_pb2.GatewayEvent) -> None:
        if self._report_queue is None:
            return
        try:
            self._report_queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("dropping gateway report event: queue full")

    async def client_connected(self, client) -> None:
        source_ip = client.peername[0] if client.peername else ""
        dest_ip, dest_port = client.sockname[:2] if client.sockname else ("0.0.0.0", 0)
        client.jetty_allowed = False
        client.jetty_connect_started = time.monotonic()
        try:
            decision = await self._get_stub().ResolveSource(
                jetty_pb2.SourceInfo(source_ip=source_ip, dest_ip=dest_ip, dest_port=int(dest_port)),
                metadata=self._metadata(),
                timeout=self._conn_timeout,
            )
        except Exception as exc:
            log.warning("ResolveSource failed for %s: %s", source_ip, exc)
            client.error = "blocked by jetty: source resolution failed"
            return

        if not decision.allowed:
            client.jetty_connection_id = decision.connection_id or None
            client.error = decision.error_message or "blocked by jetty: source is not a registered VM"
            return

        client.jetty_allowed = True

    async def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        client = data.context.client
        if not getattr(client, "jetty_allowed", False):
            data.ignore_connection = False
            client.jetty_conn_error = "source is not a registered VM"
            client.error = "blocked by jetty: source is not a registered VM"
            return

        sni = data.client_hello.sni or ""
        ech_present = any(ext_type == ECH_EXTENSION_TYPE for ext_type, _ in data.client_hello.extensions)
        source_ip = client.peername[0] if client.peername else ""
        server_addr = data.context.server.address
        dest_ip, dest_port = server_addr[:2] if server_addr else ("0.0.0.0", 443)

        try:
            decision = await self._get_stub().DecideConnection(
                jetty_pb2.ConnectionInfo(
                    source_ip=source_ip,
                    dest_ip=dest_ip,
                    dest_port=int(dest_port),
                    sni=sni,
                    ech_present=ech_present,
                ),
                metadata=self._metadata(),
                timeout=self._conn_timeout,
            )
        except Exception as exc:
            log.warning("DecideConnection failed for %s -> %s: %s", source_ip, sni or dest_ip, exc)
            data.ignore_connection = False
            client.jetty_conn_error = "connection decision failed"
            client.error = "blocked by jetty: connection decision failed"
            return

        client.jetty_connection_id = decision.connection_id or None
        client.jetty_sni = sni or None
        action = decision.WhichOneof("action")
        if action == "passthrough":
            data.ignore_connection = True
            return
        if action == "intercept":
            data.ignore_connection = False
            return

        data.ignore_connection = False
        client.jetty_conn_error = "invalid connection decision"
        client.error = "blocked by jetty: invalid connection decision"

    async def request(self, flow: http.HTTPFlow) -> None:
        client = flow.client_conn
        if not getattr(client, "jetty_allowed", False):
            flow.kill()
            return

        conn_err = getattr(client, "jetty_conn_error", None)
        if conn_err:
            flow.response = http.Response.make(
                502,
                b'{"error":{"code":"gateway_unavailable","message":"connection decision failed"}}',
                {"Content-Type": "application/json"},
            )
            flow.metadata["jetty_reported"] = True
            return

        raw_body = flow.request.raw_content
        if raw_body is not None and len(raw_body) > self._max_body_bytes:
            flow.response = http.Response.make(
                413,
                b'{"error":{"code":"payload_too_large","message":"request body exceeds gateway limit"}}',
                {"Content-Type": "application/json"},
            )
            flow.metadata["jetty_reported"] = True
            return

        body_sha = hashlib.sha256(raw_body or b"").digest()
        sni = getattr(client, "jetty_sni", None)
        hostname = sni or flow.request.pretty_host
        raw_path = flow.request.path
        parsed_path = urlparse(raw_path).path or "/"
        dest_port = (
            flow.server_conn.address[1]
            if getattr(flow, "server_conn", None) and flow.server_conn.address
            else (443 if flow.request.scheme == "https" else 80)
        )
        headers = [jetty_pb2.Header(name=k, value=v) for k, v in flow.request.headers.items(multi=True)]
        req_info = jetty_pb2.RequestInfo(
            connection_id=getattr(client, "jetty_connection_id", None) or "",
            source_ip=client.peername[0] if client.peername else "",
            method=flow.request.method,
            host=hostname,
            path=parsed_path,
            headers=headers,
            body_sha256=body_sha,
            scheme=flow.request.scheme,
            dest_port=int(dest_port),
            host_header=flow.request.host_header or "",
            raw_path=raw_path,
            has_body=raw_body is not None,
        )

        flow.metadata["jetty_started"] = time.monotonic()
        try:
            decision = await self._get_stub().DecideRequest(
                req_info, metadata=self._metadata(), timeout=self._req_timeout
            )
        except Exception as exc:
            log.warning("DecideRequest failed for %s %s: %s", flow.request.method, hostname, exc)
            flow.response = http.Response.make(
                502,
                b'{"error":{"code":"gateway_unavailable","message":"policy decision failed"}}',
                {"Content-Type": "application/json"},
            )
            flow.metadata["jetty_reported"] = True
            return

        flow.metadata["jetty_request_id"] = decision.request_id
        action = decision.WhichOneof("action")
        if action == "respond":
            resp_headers = {h.name: h.value for h in decision.respond.headers}
            flow.response = http.Response.make(
                decision.respond.status_code,
                bytes(decision.respond.body),
                resp_headers,
            )
            flow.metadata["jetty_reported"] = True
            return

        if action == "forward":
            for name in decision.forward.remove_headers:
                flow.request.headers.pop(name, None)
            for hdr in decision.forward.set_headers:
                flow.request.headers[hdr.name] = hdr.value
            return

        flow.response = http.Response.make(
            502,
            b'{"error":{"code":"gateway_unavailable","message":"empty policy decision"}}',
            {"Content-Type": "application/json"},
        )
        flow.metadata["jetty_reported"] = True

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("jetty_reported"):
            return
        req_id = flow.metadata.get("jetty_request_id")
        if not req_id:
            return
        started = flow.metadata.get("jetty_started")
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        status_code = flow.response.status_code if flow.response else 0
        self._enqueue_report(
            jetty_pb2.GatewayEvent(
                request_completed=jetty_pb2.RequestCompleted(
                    request_id=req_id,
                    connection_id=getattr(flow.client_conn, "jetty_connection_id", None) or "",
                    status_code=status_code,
                    has_status=flow.response is not None,
                    status_origin="upstream",
                    latency_ms=latency_ms,
                    has_latency_ms=started is not None,
                )
            )
        )
        flow.metadata["jetty_reported"] = True

    def error(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("jetty_reported"):
            return
        req_id = flow.metadata.get("jetty_request_id")
        if not req_id:
            return
        started = flow.metadata.get("jetty_started")
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        err_msg = str(flow.error.msg) if getattr(flow, "error", None) else "upstream error"
        self._enqueue_report(
            jetty_pb2.GatewayEvent(
                request_completed=jetty_pb2.RequestCompleted(
                    request_id=req_id,
                    connection_id=getattr(flow.client_conn, "jetty_connection_id", None) or "",
                    has_status=False,
                    latency_ms=latency_ms,
                    has_latency_ms=started is not None,
                    error=err_msg,
                )
            )
        )
        flow.metadata["jetty_reported"] = True

    def client_disconnected(self, client) -> None:
        connection_id = getattr(client, "jetty_connection_id", None)
        started = getattr(client, "jetty_connect_started", None)
        if not connection_id or started is None:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        self._enqueue_report(
            jetty_pb2.GatewayEvent(
                connection_closed=jetty_pb2.ConnectionClosed(
                    connection_id=connection_id,
                    duration_ms=duration_ms,
                )
            )
        )
