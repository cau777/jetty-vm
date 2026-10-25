"""Opt-in live checks: JETTY_LIVE_TESTS=1 pytest tests/e2e/test_lxd_instances.py.

Requires an existing, healthy Jetty gateway and proxy at JETTY_MANAGEMENT_URL
(default localhost:8080), LXD socket access, and the host's default SSH key.
Creates and removes only its own two guests, Credential and Rule. The shared
gateway/proxy and existing guests are never stopped or reconfigured.
"""
import json
import os
import secrets
import subprocess
import urllib.request

import pytest

from orca_proxy import db, jetty
from orca_proxy.lxd import LxdClient

pytestmark = pytest.mark.skipif(os.environ.get("JETTY_LIVE_TESTS") != "1", reason="requires live Jetty/LXD")


async def test_vm_and_container_networking(tmp_path, monkeypatch):
    base = os.environ.get("JETTY_MANAGEMENT_URL", "http://127.0.0.1:8080").rstrip("/") + "/api/v1"

    def api(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    suffix = secrets.token_hex(4)
    container, vm = f"jetty-check-ct-{suffix}", f"jetty-check-vm-{suffix}"
    credential, rule = f"check-{suffix}", f"check-{suffix}"
    conn = db.connect(tmp_path / "state.sqlite")
    db.migrate(conn)
    # Use only the live public CA; no production database or private keys are read.
    certificate = api("GET", "/ca")["certificate_pem"]
    monkeypatch.setattr(jetty.ca, "get", lambda _: {"certificate_pem": certificate})
    original_put = jetty.vms_repo.put

    def register(conn, name, ip):
        original_put(conn, name, ip)
        api("PUT", f"/vms/{name}", {"ip_address": ip})

    monkeypatch.setattr(jetty.vms_repo, "put", register)
    service = jetty.JettyLxd(conn)

    async def skip_host_ssh_config():
        pass

    monkeypatch.setattr(service, "_sync_ssh_config", skip_host_ssh_config)
    created, registered_credential, registered_rule = [], False, False
    try:
        # Leave the VM type unspecified to exercise backward-compatible defaults.
        for name, kwargs in [(vm, {}), (container, {"instance_type": "container"})]:
            created.append(name)
            result = await service.create_vm(name, cpus=1, memory="1GiB", disk="10GiB", **kwargs)
            assert result["instance_type"] == kwargs.get("instance_type", "virtual-machine")
        summaries = {item["name"]: item for item in await service.list_vms()}
        vm_ip = summaries[vm]["ip_address"]
        assert summaries[container]["instance_type"] == "container"
        api("PUT", f"/credentials/{credential}", {"command": "printf jetty-check-sentinel", "ttl_seconds": 0})
        registered_credential = True
        api("PUT", f"/vms/{container}/rules/{rule}", {
            "priority": 1, "hostname": "postman-echo.com",
            "action": {"type": "allow_with_credential", "credential": credential,
                       "path_prefix": "/headers", "injection": {"type": "bearer"}},
        })
        registered_rule = True
        async with LxdClient() as client:
            async def run(name, command):
                result = await client.execute(name, ["bash", "-c", command])
                assert result.exit_code == 0, result.stderr.decode(errors="replace")
                return result.stdout.decode()

            for name in (vm, container):
                assert await run(name, "curl -fsS -m 20 -o /dev/null -w '%{http_code}' https://example.com") == "200"
                assert await run(name, "curl -fsS -m 20 -o /dev/null -w '%{http_code}' http://example.com") == "200"
                echo = await run(name, "curl -fsS -m 20 https://postman-echo.com/headers")
                assert ("Bearer jetty-check-sentinel" in echo) == (name == container)
            assert (await run(container, "cat /proc/self/uid_map")).split()[1] != "0"
            # Run as guest root: source spoofing and direct routes must not bypass the proxy.
            script = f"ip addr add {vm_ip}/32 dev eth0; trap 'ip addr del {vm_ip}/32 dev eth0' EXIT; if curl -sS -m 5 --interface {vm_ip} https://example.com >/dev/null 2>&1; then exit 1; fi"
            await run(container, script)
            await run(container, "mac=$(cat /sys/class/net/eth0/address); trap 'ip link set eth0 address \"$mac\"' EXIT; ip link set eth0 address 00:16:3e:de:ad:01; if curl -sS -m 5 https://example.com >/dev/null 2>&1; then exit 1; fi")
            for address, port in [(vm_ip, 22), ("10.202.0.1", 22), ("10.201.0.1", 22), ("10.201.0.1", 8080), ("10.201.0.1", 443)]:
                await run(container, f"if timeout 2 bash -c '</dev/tcp/{address}/{port}' 2>/dev/null; then exit 1; fi")
            await run(container, "if curl -6 -sS -m 5 https://example.com >/dev/null 2>&1; then exit 1; fi")
            assert await run(container, "curl -fsS -m 20 -o /dev/null -w '%{http_code}' https://example.com") == "200"
            # Reboot only our container, then verify persisted network/bootstrap.
            await service.vm_action(container, "restart")
            await service._wait_for_agent(client, container)
            await run(container, "cloud-init status --wait")
            assert await run(container, "curl -fsS -m 20 -o /dev/null -w '%{http_code}' https://example.com") == "200"

        ssh_config = tmp_path / "ssh-config"
        ssh_config.write_text(await service.ssh_config() + f"\nHost *\n  StrictHostKeyChecking accept-new\n  UserKnownHostsFile {tmp_path / 'known_hosts'}\n  LogLevel ERROR\n")
        for name in (vm, container):
            probe = subprocess.run(["ssh", "-F", str(ssh_config), "-o", "ConnectTimeout=10", name, "echo ssh-ok"], capture_output=True, text=True, timeout=30)
            assert probe.returncode == 0, probe.stderr
            assert probe.stdout.strip() == "ssh-ok"
    finally:
        if registered_rule:
            api("DELETE", f"/vms/{container}/rules/{rule}")
        if registered_credential:
            api("DELETE", f"/credentials/{credential}")
        for name in reversed(created):
            await service.delete_vm(name)
            api("DELETE", f"/vms/{name}")
        conn.close()
