"""Cross-language integration test: Python jetty-gw-agent <-> Go jettyd over h2c gRPC."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from mitmproxy import http

from jetty_gw_agent.addon import ECH_EXTENSION_TYPE, JettyGatewayAddon, bootstrap_gateway
from jetty_gw_agent.proto.jetty.v1 import jetty_pb2, jetty_pb2_grpc
from jetty_gw_agent.version import AGENT_VERSION

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"jettyd did not open port {port} in time")


@pytest.fixture()
def running_jettyd(tmp_path: Path):
    bin_path = tmp_path / "jetty"
    subprocess.run(
        ["/usr/local/go/bin/go", "build", "-o", str(bin_path), "./cmd/jetty"],
        cwd=ROOT,
        check=True,
    )
    port = _free_port()
    data_dir = tmp_path / "home"
    data_dir.mkdir()
    env = {
        **os.environ,
        "ORCA_PROXY_HOME": str(data_dir),
        "JETTY_LISTEN_PORT": str(port),
        "JETTY_ADDR": f"127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [str(bin_path), "daemon"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_port(port)
        yield bin_path, f"127.0.0.1:{port}", env, data_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.asyncio
async def test_gateway_agent_talks_to_real_go_jettyd(running_jettyd, tmp_path: Path):
    bin_path, addr, env, _data_dir = running_jettyd

    def cli(*args: str) -> str:
        res = subprocess.run([str(bin_path), *args], env=env, check=True, capture_output=True, text=True)
        return res.stdout

    # Seed VM, Credential, and Rules via Go CLI
    cli("vm", "put", "agent-a", "10.202.0.11")
    cli("credential", "put", "e2e-cred", "--command", "printf real-go-secret", "--ttl", "60")
    cli(
        "rule", "put", "inject-api", "--vm", "agent-a",
        "--priority", "10", "--hostname", "postman-echo.com",
        "--action", "allow_with_credential", "--credential", "e2e-cred",
        "--path-prefix", "/headers", "--injection", "bearer",
    )
    cli(
        "rule", "put", "block-evil", "--global",
        "--priority", "20", "--hostname", "evil.example", "--action", "block",
    )

    # 1. Bootstrap Hello + GetCa (loopback has no bearer requirement; addon still sends metadata)
    confdir = tmp_path / "mitm-conf"
    cfg = bootstrap_gateway(addr=addr, token="ignored-on-loopback", confdir=confdir, agent_version=AGENT_VERSION)
    assert cfg.expected_version == AGENT_VERSION
    ca_pem = confdir / "mitmproxy-ca.pem"
    assert ca_pem.is_file()
    assert "BEGIN CERTIFICATE" in ca_pem.read_text()
    assert "BEGIN PRIVATE KEY" in ca_pem.read_text()

    # 2. Version mismatch rejected by Go jettyd
    with pytest.raises(grpc.RpcError) as exc_info:
        bootstrap_gateway(addr=addr, token="ignored", confdir=confdir, agent_version="0.0.1")
    assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    # 3. Exercise full addon flow against Go jettyd
    addon = JettyGatewayAddon(addr=addr, token="ignored", confdir=confdir, skip_bootstrap=True)
    await addon.running()
    try:
        # Unknown VM blocked at ResolveSource
        bad_client = SimpleNamespace(peername=("10.202.0.99", 1000), sockname=("1.1.1.1", 443), error=None)
        await addon.client_connected(bad_client)
        assert bad_client.jetty_allowed is False
        assert bad_client.error is not None

        # Registered VM allowed
        client = SimpleNamespace(peername=("10.202.0.11", 1001), sockname=("54.1.2.3", 443), error=None)
        await addon.client_connected(client)
        assert client.jetty_allowed is True

        # Passthrough for example.com
        pass_data = SimpleNamespace(
            context=SimpleNamespace(client=client, server=SimpleNamespace(address=("93.184.216.34", 443))),
            client_hello=SimpleNamespace(sni="example.com", extensions=[]),
            ignore_connection=False,
        )
        await addon.tls_clienthello(pass_data)
        assert pass_data.ignore_connection is True

        # Intercept + credential injection for postman-echo.com/headers
        cred_client = SimpleNamespace(peername=("10.202.0.11", 1002), sockname=("54.1.2.3", 443), error=None)
        await addon.client_connected(cred_client)
        cred_data = SimpleNamespace(
            context=SimpleNamespace(client=cred_client, server=SimpleNamespace(address=("54.1.2.3", 443))),
            client_hello=SimpleNamespace(sni="postman-echo.com", extensions=[(ECH_EXTENSION_TYPE, b"x")]),
            ignore_connection=False,
        )
        await addon.tls_clienthello(cred_data)
        assert cred_data.ignore_connection is False

        flow = http.HTTPFlow(cred_client, SimpleNamespace(address=("54.1.2.3", 443)))
        flow.request = http.Request.make("GET", "https://postman-echo.com/headers", b"", {"Authorization": "Bearer placeholder"})
        await addon.request(flow)
        assert flow.request.headers["Authorization"] == "Bearer real-go-secret"

        # Upstream response & disconnect reported via bidirectional/client stream
        flow.response = http.Response.make(200, b"{}")
        addon.response(flow)
        addon.client_disconnected(cred_client)
        await asyncio.sleep(0.15)
    finally:
        await addon.done()

    # Verify CLI --json log output reflects persisted connection and redacted headers
    logs_json = json.loads(cli("--json", "log", "list", "--vm", "agent-a"))
    assert len(logs_json["connections"]) >= 2
    cred_conn = next(c for c in logs_json["connections"] if c["destination_hostname"] == "postman-echo.com")
    detail = json.loads(cli("--json", "log", "show", str(cred_conn["id"])))
    assert detail["http_requests"][0]["status"] == 200
    auth_hdr = next(h for h in detail["http_requests"][0]["headers"] if h["name"].lower() == "authorization")
    assert auth_hdr["redacted"] is True
    assert "e2e-cred" in auth_hdr["value"]
