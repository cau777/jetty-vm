"""Exercise both provisioning paths, including the container storage/profile differences."""
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from orca_proxy import cli, jetty
from orca_proxy.errors import ValidationFailed
from orca_proxy.lxd import ExecResult, LxdError, LxdNotFound

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test"


class FakeLxd:
    def __init__(self, driver):
        self.driver = driver
        self.created = None
        self.profile = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def request(self, method, path, *, body=None, **kwargs):
        if path == "/1.0/profiles/container" and method == "GET":
            raise LxdNotFound("missing")
        if path == "/1.0/profiles" and method == "POST":
            self.profile = body
        elif path == "/1.0/storage-pools/default":
            return {"driver": self.driver}
        elif path == "/1.0/instances" and method == "POST":
            self.created = body
        else:
            raise AssertionError((method, path))

    async def execute(self, name, command):
        assert self.created is not None
        assert command == ["cloud-init", "status", "--wait"]
        return ExecResult(0, b"done", b"")


@pytest.mark.parametrize("instance_type,driver", [(None, "dir"), ("container", "dir"), ("container", "zfs")])
async def test_provisioning_preserves_network_and_bootstrap(monkeypatch, instance_type, driver):
    client = FakeLxd(driver)
    monkeypatch.setattr(jetty, "LxdClient", lambda: client)
    monkeypatch.setattr(jetty.ca, "get", lambda _: {"certificate_pem": "test public CA"})
    registrations = []
    monkeypatch.setattr(jetty.vms_repo, "put", lambda _db, name, ip: registrations.append((name, ip)))
    service = jetty.JettyLxd(None)
    service._instance = AsyncMock(side_effect=[{"name": jetty.GATEWAY}, None, {"status": "Running"}])
    service._next_ip = AsyncMock(return_value="10.202.0.21")
    service._ensure_profile = AsyncMock()
    service._sync_ssh_config = AsyncMock()
    service._start = AsyncMock()
    service._wait_for_agent = AsyncMock()
    kwargs = {"instance_type": instance_type} if instance_type else {}
    result = await service.create_vm("test-agent", ssh_key=KEY, **kwargs)
    expected_type = instance_type or "virtual-machine"
    assert result["instance_type"] == expected_type
    assert registrations == [("test-agent", "10.202.0.21")]
    body = client.created
    assert body["type"] == expected_type
    nic = body["devices"]["eth0"]
    assert nic["network"] == jetty.PRIV_NET
    assert nic["ipv4.address"] == "10.202.0.21"
    for key in ("security.ipv4_filtering", "security.ipv6_filtering", "security.mac_filtering", "security.port_isolation"):
        assert nic[key] == "true"
    assert "via: 10.202.0.1" in body["config"]["cloud-init.network-config"]
    assert KEY in body["config"]["cloud-init.user-data"]
    assert "test public CA" in body["config"]["cloud-init.user-data"]
    if instance_type == "container":
        assert "match: {name: eth0}" in body["config"]["cloud-init.network-config"]
        assert nic["name"] == "eth0"
        assert body["profiles"] == ["container"]
        assert body["config"]["security.privileged"] == "false"
        assert client.profile["config"] == {}
        service._ensure_profile.assert_not_awaited()
    else:
        assert body["profiles"] == ["default"]
        service._ensure_profile.assert_awaited_once()
    assert ("size" in body["devices"]["root"]) == (expected_type != "container" or driver != "dir")


@pytest.mark.parametrize("value", ["docker", "vm", None, 42, {}])
async def test_invalid_instance_type_rejected_before_lxd(value):
    with pytest.raises(ValidationFailed, match="instance_type"):
        await jetty.JettyLxd(None).create_vm("test-agent", instance_type=value, ssh_key=KEY)


@pytest.mark.parametrize("lxd_error", [None, LxdError("instance deletion failed")])
async def test_delete_vm_removes_rules_after_lxd_deletion(client, monkeypatch, lxd_error):
    lxd = MagicMock()
    lxd.__aenter__.return_value = lxd
    lxd.request = AsyncMock(side_effect=[{"status": "Stopped"}, lxd_error])
    monkeypatch.setattr(jetty, "LxdClient", lambda: lxd)
    monkeypatch.setattr(jetty.JettyLxd, "_sync_ssh_config", AsyncMock())
    vm = "/api/v1/vms/test-agent"
    rule = "/api/v1/rules/allow-github?scope=test-agent"
    assert (await client.put(vm, json={"ip_address": "10.202.0.21"})).status == 201
    response = await client.put(
        rule, json={"priority": 10, "hostname": "github.com", "action": {"type": "allow"}}
    )
    assert response.status == 201

    response = await client.delete("/api/v1/jetty/vms/test-agent")
    assert response.status == (503 if lxd_error else 204)
    assert lxd.request.await_args_list == [
        call("GET", "/1.0/instances/test-agent"),
        call("DELETE", "/1.0/instances/test-agent"),
    ]
    expected_status = 200 if lxd_error else 404
    assert (await client.get(vm)).status == expected_status
    assert (await client.get(rule)).status == expected_status


@pytest.mark.parametrize("option,expected", [([], "virtual-machine"), (["--type", "container"], "container")])
def test_cli_sends_instance_type(monkeypatch, option, expected):
    calls = []
    monkeypatch.setattr(cli, "_api", lambda method, path, body: calls.append(body) or {})
    monkeypatch.setattr(cli, "_wait_for_job", lambda _: {"name": "test-agent", "status": "Running", "ip_address": "10.202.0.21"})
    cli._vm(cli._parser().parse_args(["vm", "create", "test-agent", *option]))
    assert calls[0]["instance_type"] == expected
