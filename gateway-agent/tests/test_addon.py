"""Unit tests for jetty-gw-agent addon hooks, fail-closed guarantees, and bootstrap."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from mitmproxy import http

from jetty_gw_agent.addon import ECH_EXTENSION_TYPE, JettyGatewayAddon, write_ca_pem
from jetty_gw_agent.proto.jetty.v1 import jetty_pb2


def _make_client(peer_ip: str = "10.202.0.11", dest_ip: str = "93.184.216.34", dest_port: int = 443):
    return SimpleNamespace(
        peername=(peer_ip, 51234),
        sockname=(dest_ip, dest_port),
        error=None,
    )


def _make_tls_data(client, sni: str | None = "example.com", ech: bool = False):
    extensions = [(ECH_EXTENSION_TYPE, b"ech")] if ech else []
    return SimpleNamespace(
        context=SimpleNamespace(
            client=client,
            server=SimpleNamespace(address=(client.sockname[0], client.sockname[1])),
        ),
        client_hello=SimpleNamespace(sni=sni, extensions=extensions),
        ignore_connection=False,
    )


def _make_flow(client, method: str = "GET", url: str = "https://api.github.com/repos/a/b", body: bytes = b"", headers: dict | None = None):
    flow = http.HTTPFlow(client, SimpleNamespace(address=("140.82.121.6", 443)))
    flow.request = http.Request.make(method, url, body, headers or {})
    return flow


def test_write_ca_pem_atomic_0600(tmp_path: Path):
    target = write_ca_pem(tmp_path / "conf", "CERT", "KEY")
    assert target.read_text() == "CERT\nKEY"
    assert (target.stat().st_mode & 0o777) == 0o600


async def test_client_connected_allowed_and_blocked():
    stub = MagicMock()
    stub.ResolveSource = AsyncMock(return_value=jetty_pb2.SourceDecision(allowed=True, vm_name="dev"))
    addon = JettyGatewayAddon(stub=stub, token="tok", skip_bootstrap=True)
    client = _make_client("10.202.0.11")
    await addon.client_connected(client)
    assert client.jetty_allowed is True
    assert client.error is None

    stub.ResolveSource = AsyncMock(
        return_value=jetty_pb2.SourceDecision(
            allowed=False, connection_id="9", error_message="blocked by jetty: unknown"
        )
    )
    bad = _make_client("10.202.0.99")
    await addon.client_connected(bad)
    assert bad.jetty_allowed is False
    assert bad.error == "blocked by jetty: unknown"
    assert bad.jetty_connection_id == "9"


async def test_client_connected_rpc_error_fails_closed():
    stub = MagicMock()
    stub.ResolveSource = AsyncMock(side_effect=TimeoutError("rpc timed out"))
    addon = JettyGatewayAddon(stub=stub, token="tok", skip_bootstrap=True)
    client = _make_client()
    await addon.client_connected(client)
    assert client.jetty_allowed is False
    assert "failed" in client.error


async def test_tls_clienthello_passthrough_intercept_and_fail_closed():
    stub = MagicMock()
    addon = JettyGatewayAddon(stub=stub, token="tok", skip_bootstrap=True)

    client = _make_client()
    client.jetty_allowed = True
    stub.DecideConnection = AsyncMock(
        return_value=jetty_pb2.ConnectionDecision(connection_id="42", passthrough=jetty_pb2.Passthrough())
    )
    data = _make_tls_data(client, sni="example.com", ech=True)
    await addon.tls_clienthello(data)
    assert data.ignore_connection is True
    assert client.jetty_connection_id == "42"
    call_req = stub.DecideConnection.call_args.args[0]
    assert call_req.ech_present is True
    assert call_req.sni == "example.com"

    client2 = _make_client()
    client2.jetty_allowed = True
    stub.DecideConnection = AsyncMock(
        return_value=jetty_pb2.ConnectionDecision(connection_id="43", intercept=jetty_pb2.Intercept())
    )
    data2 = _make_tls_data(client2, sni="api.github.com")
    await addon.tls_clienthello(data2)
    assert data2.ignore_connection is False
    assert client2.jetty_sni == "api.github.com"

    # Fail closed on RPC timeout
    client3 = _make_client()
    client3.jetty_allowed = True
    stub.DecideConnection = AsyncMock(side_effect=TimeoutError("deadline exceeded"))
    data3 = _make_tls_data(client3, sni="api.github.com")
    await addon.tls_clienthello(data3)
    assert data3.ignore_connection is False
    assert client3.jetty_conn_error == "connection decision failed"


async def test_request_forward_header_edits_and_body_sha256():
    stub = MagicMock()
    stub.DecideRequest = AsyncMock(
        return_value=jetty_pb2.RequestDecision(
            request_id="req-1",
            forward=jetty_pb2.Forward(
                remove_headers=["X-Remove"],
                set_headers=[jetty_pb2.Header(name="Authorization", value="Bearer real-secret")],
            ),
        )
    )
    addon = JettyGatewayAddon(stub=stub, token="tok", skip_bootstrap=True)
    client = _make_client()
    client.jetty_allowed = True
    client.jetty_connection_id = "10"
    client.jetty_sni = "api.github.com"

    flow = _make_flow(
        client,
        method="POST",
        url="https://api.github.com/repos/a/b?page=1",
        body=b"payload-bytes",
        headers={"Authorization": "Bearer placeholder", "X-Remove": "drop-me"},
    )
    await addon.request(flow)
    req_arg = stub.DecideRequest.call_args.args[0]
    assert req_arg.body_sha256 == hashlib.sha256(b"payload-bytes").digest()
    assert req_arg.path == "/repos/a/b"
    assert req_arg.raw_path == "/repos/a/b?page=1"
    assert flow.request.headers["Authorization"] == "Bearer real-secret"
    assert "X-Remove" not in flow.request.headers


async def test_request_synthesize_and_fail_closed_502_and_size_limit():
    stub = MagicMock()
    stub.DecideRequest = AsyncMock(
        return_value=jetty_pb2.RequestDecision(
            request_id="req-2",
            respond=jetty_pb2.Synthesize(
                status_code=403,
                body=b"blocked",
                headers=[jetty_pb2.Header(name="Content-Type", value="text/plain")],
            ),
        )
    )
    addon = JettyGatewayAddon(stub=stub, token="tok", skip_bootstrap=True)
    client = _make_client()
    client.jetty_allowed = True
    client.jetty_connection_id = "11"
    client.jetty_sni = "api.github.com"

    flow = _make_flow(client)
    await addon.request(flow)
    assert flow.response.status_code == 403
    assert flow.response.content == b"blocked"

    # RPC error -> 502
    stub.DecideRequest = AsyncMock(side_effect=RuntimeError("boom"))
    flow2 = _make_flow(client)
    await addon.request(flow2)
    assert flow2.response.status_code == 502

    # Body size limit -> 413
    addon._max_body_bytes = 4
    flow3 = _make_flow(client, method="POST", body=b"12345")
    await addon.request(flow3)
    assert flow3.response.status_code == 413


async def test_report_queues_response_and_close_events():
    stub = MagicMock()
    reported: list[jetty_pb2.GatewayEvent] = []

    async def fake_report(stream, metadata=None):
        async for item in stream:
            reported.append(item)
            if len(reported) >= 2:
                break
        return jetty_pb2.Empty()

    stub.Report = fake_report
    addon = JettyGatewayAddon(stub=stub, token="tok", skip_bootstrap=True)
    await addon.running()

    client = _make_client()
    client.jetty_allowed = True
    client.jetty_connection_id = "77"
    client.jetty_connect_started = 10.0
    flow = _make_flow(client)
    flow.metadata["jetty_request_id"] = "req-77"
    flow.metadata["jetty_started"] = 10.0
    flow.response = http.Response.make(401, b"unauth")

    addon.response(flow)
    addon.client_disconnected(client)
    await asyncio.sleep(0.05)
    await addon.done()

    assert len(reported) == 2
    assert reported[0].request_completed.status_code == 401
    assert reported[1].connection_closed.connection_id == "77"
