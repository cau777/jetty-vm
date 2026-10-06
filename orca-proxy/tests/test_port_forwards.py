import asyncio
import socket

import pytest

from orca_proxy import port_forwards
from orca_proxy.app import create_app
from orca_proxy.repo import port_forwards as port_forwards_repo


class FakeTunnel:
    def __init__(self):
        self.error = None
        self.exited = asyncio.Event()
        self.stopped = False

    async def wait(self):
        await self.exited.wait()
        return 255

    async def stop(self):
        self.stopped = True
        self.exited.set()


class FakeConnector:
    def __init__(self):
        self.tunnels = []
        self.failure = None

    async def __call__(self, forward):
        if self.failure:
            raise port_forwards.PortForwardError(self.failure)
        tunnel = FakeTunnel()
        self.tunnels.append(tunnel)
        return tunnel


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def connector():
    return FakeConnector()


@pytest.fixture
def app(tmp_path, monkeypatch, connector):
    monkeypatch.setenv("ORCA_PROXY_HOME", str(tmp_path))
    return create_app(port_forward_connector=connector)


@pytest.fixture
async def client(app, aiohttp_client):
    client = await aiohttp_client(app)
    resp = await client.put("/api/v1/vms/dev-vm", json={"ip_address": "10.202.0.21"})
    assert resp.status == 201
    return client


async def _until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_open_one_time_forward_becomes_active_and_closes(app, client, connector):
    port = _free_port()
    resp = await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": port})
    body = await resp.json()
    assert resp.status == 201
    assert body["host_port"] == port
    assert body["persistent"] is False
    assert body["url"] == f"http://localhost:{port}"

    await _until(lambda: app["port_forwards"].list()[0]["state"] == "active")
    assert port_forwards_repo.list_all(app["db"]) == []

    resp = await client.delete(f"/api/v1/jetty/vms/dev-vm/ports/{port}")
    assert resp.status == 204
    assert connector.tunnels[0].stopped
    assert (await (await client.get("/api/v1/jetty/ports")).json()) == {"ports": []}


async def test_persistent_forward_is_stored_and_reopened_on_startup(app, client, connector, aiohttp_client):
    host_port = _free_port()
    resp = await client.post(
        "/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 3000, "host_port": host_port, "persistent": True}
    )
    assert resp.status == 201
    assert [row["host_port"] for row in port_forwards_repo.list_all(app["db"])] == [host_port]

    await app["port_forwards"].shutdown()
    assert app["port_forwards"].list() == []
    app["port_forwards"].start_persistent()
    assert app["port_forwards"].list()[0]["vm_port"] == 3000
    await _until(lambda: len(connector.tunnels) == 2)


async def test_toggle_persistence(app, client):
    host_port = _free_port()
    await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 5173, "host_port": host_port})
    resp = await client.patch(f"/api/v1/jetty/vms/dev-vm/ports/{host_port}", json={"persistent": True})
    assert (await resp.json())["persistent"] is True
    assert len(port_forwards_repo.list_all(app["db"])) == 1
    await client.patch(f"/api/v1/jetty/vms/dev-vm/ports/{host_port}", json={"persistent": False})
    assert port_forwards_repo.list_all(app["db"]) == []


async def test_host_port_conflicts_are_rejected(client):
    host_port = _free_port()
    await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 3000, "host_port": host_port})
    resp = await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 4000, "host_port": host_port})
    assert resp.status == 409

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        resp = await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 3000, "host_port": taken})
        assert resp.status == 409
        assert "already in use" in (await resp.json())["error"]["message"]


async def test_invalid_ports_and_unknown_vm(client):
    resp = await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 80})
    assert resp.status == 422
    resp = await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 3000, "host_port": 80})
    assert resp.status == 422
    resp = await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 70000})
    assert resp.status == 422
    resp = await client.post("/api/v1/jetty/vms/missing/ports", json={"vm_port": 3000})
    assert resp.status == 404


async def test_failed_connection_is_reported_and_retried(app, client, connector, monkeypatch):
    monkeypatch.setattr(port_forwards, "RETRY_MIN_SECONDS", 0.01)
    connector.failure = "VM 'dev-vm' is not running"
    await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": _free_port()})
    await _until(lambda: app["port_forwards"].list()[0]["state"] == "retrying")
    assert app["port_forwards"].list()[0]["error"] == "VM 'dev-vm' is not running"

    connector.failure = None
    await _until(lambda: app["port_forwards"].list()[0]["state"] == "active")
    assert app["port_forwards"].list()[0]["error"] is None


async def test_deleting_vm_stops_and_forgets_its_forwards(app, client, connector):
    host_port = _free_port()
    await client.post("/api/v1/jetty/vms/dev-vm/ports", json={"vm_port": 3000, "host_port": host_port, "persistent": True})
    await _until(lambda: len(connector.tunnels) == 1)
    resp = await client.delete("/api/v1/vms/dev-vm")
    assert resp.status == 204
    assert app["port_forwards"].list() == []
    assert port_forwards_repo.list_all(app["db"]) == []
    assert connector.tunnels[0].stopped
