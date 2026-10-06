"""Deterministic LXD setup and VM lifecycle operations for Jetty."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import time
from pathlib import PurePosixPath
import re
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import ca, config, validation
from .errors import Conflict, NotFound, ValidationFailed
from .lxd import ExecResult, LxdClient, LxdError, LxdNotFound, socket_accessible, socket_path
from .repo import vms as vms_repo
from .ssh_config import write_jetty_config

PROJECT = "jetty"
UP_NET = "jettyup0"
UP_ADDR = "10.201.0.1"
PRIV_NET = "jettypriv0"
PRIV_PREFIX = "10.202.0"
GATEWAY = "jetty-gw"
GATEWAY_UP_IP = "10.201.0.2"
GATEWAY_PRIV_IP = "10.202.0.1"
GATEWAY_IMAGE = "ubuntu:24.04"
AGENT_IMAGE = "ubuntu:24.04"
SSH_PORT_BASE = 2200
TUNNEL_PORT = 51820
_SIZE_RE = re.compile(r"^[1-9][0-9]*(?:[KMGT]i?B)?$")


def gateway_asset(name: str) -> bytes:
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS")) / "orca_proxy" / "deploy" / "gateway"
    else:
        root = Path(__file__).resolve().parents[2] / "deploy" / "gateway"
    return (root / name).read_bytes()


def _validate_ssh_key(value: str) -> str:
    key = value.strip()
    if "\n" in key or "\r" in key or not re.match(r"^(ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+\s+[A-Za-z0-9+/=]+(?:\s+.*)?$", key):
        raise ValidationFailed("Provide one valid SSH public key on a single line", fields={"ssh_public_key": "invalid SSH key"})
    return key


def default_ssh_key(path: str | None = None) -> str:
    candidates = [Path(path)] if path else [
        Path.home() / ".ssh/id_ed25519.pub",
        Path.home() / ".ssh/id_ecdsa.pub",
        Path.home() / ".ssh/id_rsa.pub",
    ]
    for candidate in candidates:
        try:
            return _validate_ssh_key(candidate.read_text(encoding="utf-8"))
        except OSError:
            continue
    raise ValidationFailed(
        "No SSH public key was found. Add ~/.ssh/id_ed25519.pub or paste a public key in setup.",
        fields={"ssh_public_key": "required"},
    )


def _image_source(image: str) -> dict[str, str]:
    remote, separator, alias = image.partition(":")
    if (
        remote != "ubuntu"
        or not separator
        or not alias
        or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", alias)
    ):
        raise ValidationFailed(
            "image must use the ubuntu:<release> form, such as ubuntu:24.04",
            fields={"image": "unsupported image alias"},
        )
    return {
        "type": "image",
        "mode": "pull",
        "protocol": "simplestreams",
        "server": "https://cloud-images.ubuntu.com/releases/",
        "alias": alias,
    }


def _cloud_config(ssh_key: str, *, packages: list[str] | None = None, ca_certificate: str | None = None) -> str:
    lines = ["#cloud-config", "ssh_authorized_keys:", f"  - {json.dumps(ssh_key)}"]
    if packages:
        lines += ["packages:", *(f"  - {json.dumps(package)}" for package in packages)]
    if ca_certificate:
        lines += [
            "write_files:",
            "  - path: /usr/local/share/ca-certificates/jetty-interception-ca.crt",
            "    owner: root:root",
            "    permissions: '0644'",
            "    content: |",
            *(f"      {line}" for line in ca_certificate.rstrip().splitlines()),
            "runcmd:",
            "  - [update-ca-certificates]",
        ]
    return "\n".join(lines) + "\n"


def _cloud_network_config(mac: str, address: str) -> str:
    return "\n".join(
        [
            "version: 2",
            "ethernets:",
            "  lan:",
            f"    match: {{macaddress: {json.dumps(mac)}}}",
            f"    addresses: [{address}/24]",
            f"    routes: [{{to: default, via: {GATEWAY_PRIV_IP}}}]",
            "    nameservers: {addresses: [1.1.1.1, 9.9.9.9]}",
            "",
        ]
    )


def _require_success(result: ExecResult, command: list[str], instance: str) -> None:
    if result.exit_code != 0:
        detail = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
        raise LxdError(f"{instance}: {' '.join(command)} exited {result.exit_code}" + (f": {detail}" if detail else ""))


class JettyLxd:
    """Owns Jetty's project, networks, gateway and registered VMs."""

    def __init__(self, db_conn):
        self.db = db_conn

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "socket": str(socket_path()),
            "lxd_available": False,
            "gateway": "missing",
            "gateway_service": "unknown",
            "tunnel": "unknown",
            "vms": [],
        }
        if not socket_accessible():
            result["message"] = "LXD is unavailable or this login cannot access its socket."
            return result
        try:
            async with LxdClient() as client:
                await client.request("GET", "/1.0", project=None)
                result["lxd_available"] = True
                instances = await self._instances(client)
                result["vms"] = [self._vm_summary(item) for item in instances if item.get("name") != GATEWAY]
                gateway = next((item for item in instances if item.get("name") == GATEWAY), None)
                if gateway is None:
                    return result
                result["gateway"] = gateway.get("status", "unknown").lower()
                if result["gateway"] != "running":
                    return result
                service = await client.execute(GATEWAY, ["systemctl", "is-active", "jetty-gateway.service"])
                result["gateway_service"] = service.stdout.decode(errors="replace").strip() or "inactive"
                handshakes = await client.execute(GATEWAY, ["wg", "show", "wg0", "latest-handshakes"])
                values = [line.split()[-1] for line in handshakes.stdout.decode(errors="replace").splitlines() if line.split()]
                latest = max((int(value) for value in values if value.isdigit()), default=0)
                result["tunnel"] = f"last handshake {max(0, int(time.time()) - latest)}s ago" if latest else "no handshake"
        except (LxdError, OSError) as exc:
            result["message"] = str(exc)
        return result

    async def setup(
        self,
        ssh_key: str | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        report = progress or (lambda _step: None)
        report("validate")
        ssh_key = _validate_ssh_key(ssh_key) if ssh_key else default_ssh_key()
        async with LxdClient() as client:
            report("project")
            await self._ensure_project(client)
            report("profile")
            await self._ensure_profile(client)
            report("networks")
            await self._ensure_network(
                client,
                UP_NET,
                {"ipv4.address": f"{UP_ADDR}/24", "ipv4.nat": "true", "ipv6.address": "none"},
            )
            await self._ensure_network(
                client,
                PRIV_NET,
                {"ipv4.address": "none", "ipv6.address": "none"},
            )

            gateway = await self._instance(client, GATEWAY)
            if gateway is None:
                report("instance")
                user_data = _cloud_config(ssh_key, packages=["wireguard-tools", "iptables"])
                await client.request(
                    "POST",
                    "/1.0/instances",
                    body={
                        "name": GATEWAY,
                        "type": "virtual-machine",
                        "source": _image_source(GATEWAY_IMAGE),
                        "profiles": ["default"],
                        "config": {
                            "limits.cpu": "1",
                            "limits.memory": "1GiB",
                            "cloud-init.user-data": user_data,
                        },
                        "devices": {
                            "root": {"type": "disk", "pool": "default", "path": "/", "size": "8GiB"},
                            "eth0": {"type": "nic", "network": UP_NET, "ipv4.address": GATEWAY_UP_IP},
                            "eth1": {"type": "nic", "network": PRIV_NET},
                        },
                    },
                )
            report("start")
            await self._start(client, GATEWAY)
            report("guest_agent")
            await self._wait_for_agent(client, GATEWAY)
            report("cloud_init")
            result = await client.execute(GATEWAY, ["cloud-init", "status", "--wait"])
            _require_success(result, ["cloud-init", "status", "--wait"], GATEWAY)
            report("configure")
            await self._configure_gateway(client)
        report("ssh_config")
        await self._sync_ssh_config()
        return {"gateway": GATEWAY, "state": "ready"}

    async def create_vm(
        self,
        name: str,
        *,
        cpus: int = 4,
        memory: str = "8GiB",
        disk: str = "40GiB",
        image: str = AGENT_IMAGE,
        ssh_key: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        report = progress or (lambda _step: None)
        report("validate")
        name = validation.validate_name(name)
        if name == GATEWAY:
            raise ValidationFailed("The gateway name is reserved", fields={"name": "reserved"})
        if not isinstance(cpus, int) or cpus < 1 or cpus > 128:
            raise ValidationFailed("cpus must be between 1 and 128", fields={"cpus": "invalid value"})
        if not isinstance(memory, str) or not _SIZE_RE.fullmatch(memory):
            raise ValidationFailed("memory must be a positive size such as 8GiB", fields={"memory": "invalid size"})
        if not isinstance(disk, str) or not _SIZE_RE.fullmatch(disk):
            raise ValidationFailed("disk must be a positive size such as 40GiB", fields={"disk": "invalid size"})
        if not isinstance(image, str):
            raise ValidationFailed(
                "image must use the ubuntu:<release> form, such as ubuntu:24.04",
                fields={"image": "invalid alias"},
            )
        image_source = _image_source(image)
        ssh_key = _validate_ssh_key(ssh_key) if ssh_key else default_ssh_key()

        async with LxdClient() as client:
            if await self._instance(client, GATEWAY) is None:
                raise Conflict("Run Jetty setup before creating a VM")
            if await self._instance(client, name) is not None:
                raise Conflict(f"VM '{name}' already exists")
            ip = await self._next_ip(client)
            ca_row = ca.get(self.db)
            if ca_row is None:
                raise LxdError("Jetty's interception CA is not initialized")
            mac = ":".join(["00", "16", "3e", *(f"{byte:02x}" for byte in secrets.token_bytes(3))])
            # LXD pulls the requested image as part of the instance creation operation.
            report("instance")
            await client.request(
                "POST",
                "/1.0/instances",
                body={
                    "name": name,
                    "type": "virtual-machine",
                    "source": image_source,
                    "profiles": ["default"],
                    "config": {
                        "limits.cpu": str(cpus),
                        "limits.memory": memory,
                        "cloud-init.user-data": _cloud_config(
                            ssh_key, ca_certificate=ca_row["certificate_pem"]
                        ),
                        "cloud-init.network-config": _cloud_network_config(mac, ip),
                    },
                    "devices": {
                        "root": {"type": "disk", "pool": "default", "path": "/", "size": disk},
                        "eth0": {
                            "type": "nic",
                            "network": PRIV_NET,
                            "hwaddr": mac,
                            "ipv4.address": ip,
                            "security.ipv4_filtering": "true",
                            "security.ipv6_filtering": "true",
                            "security.mac_filtering": "true",
                            "security.port_isolation": "true",
                        },
                    },
                },
            )
            report("register")
            vms_repo.put(self.db, name, ip)
            await self._sync_ssh_config()
            report("start")
            await self._start(client, name)
            report("guest_agent")
            await self._wait_for_agent(client, name)
            report("cloud_init")
            cloud_init = await client.execute(name, ["cloud-init", "status", "--wait"])
            _require_success(cloud_init, ["cloud-init", "status", "--wait"], name)
            report("ready")
            instance = await self._instance(client, name)
            result = {"name": name, "ip_address": ip, "status": (instance or {}).get("status", "Running")}
        return result

    async def delete_vm(self, name: str) -> None:
        name = validation.validate_name(name)
        if name == GATEWAY:
            raise Conflict("The Jetty gateway cannot be deleted as an agent VM")
        if vms_repo.referenced_by_rule(self.db, name):
            raise Conflict(f"VM '{name}' is referenced by a Rule; update or delete it first")
        async with LxdClient() as client:
            instance = await self._instance(client, name)
            if instance is not None:
                if instance.get("status") != "Stopped":
                    await client.request(
                        "PUT",
                        f"/1.0/instances/{quote(name, safe='')}/state",
                        body={"action": "stop", "force": True, "timeout": -1},
                    )
                await client.request(
                    "DELETE", f"/1.0/instances/{quote(name, safe='')}"
                )
        existing = vms_repo.get(self.db, name)
        if existing is not None:
            vms_repo.delete(self.db, name)
        await self._sync_ssh_config()

    async def vm_action(self, name: str, action: str) -> dict[str, Any]:
        name = validation.validate_name(name)
        if name == GATEWAY:
            raise Conflict("Use Jetty setup to manage the gateway")
        if action not in {"start", "stop", "restart"}:
            raise ValidationFailed("action must be start, stop, or restart", fields={"action": "invalid value"})
        async with LxdClient() as client:
            instance = await self._instance(client, name)
            if instance is None:
                raise NotFound(f"VM '{name}' not found")
            status = instance.get("status")
            if action == "start" or (action == "restart" and status == "Stopped"):
                await self._start(client, name)
            elif action == "stop" and status == "Stopped":
                pass
            else:
                await client.request(
                    "PUT",
                    f"/1.0/instances/{quote(name, safe='')}/state",
                    body={"action": action, "force": True, "timeout": 60},
                )
            instance = await self._instance(client, name)
            return {"name": name, "status": (instance or {}).get("status", "unknown")}

    async def prepare_port_forward(self, name: str, public_key: str) -> tuple[int, str]:
        """Authorize Jetty's forwarding key on a running VM and return (gateway SSH port, host key).

        The host key comes over the LXD socket, so the SSH connection that
        follows can verify it strictly even after a VM is recreated.
        """
        row = vms_repo.get(self.db, name)
        if row is None:
            raise NotFound(f"VM '{name}' not found")
        script = (
            'set -e; d=/home/ubuntu/.ssh; f="$d/authorized_keys"; '
            'install -d -m 700 -o ubuntu -g ubuntu "$d"; touch "$f"; '
            'grep -qxF "$JETTY_KEY" "$f" || printf "%s\\n" "$JETTY_KEY" >> "$f"; '
            'chown ubuntu:ubuntu "$f"; chmod 600 "$f"; '
            "cat /etc/ssh/ssh_host_ed25519_key.pub"
        )
        async with LxdClient() as client:
            instance = await self._instance(client, name)
            if instance is None:
                raise NotFound(f"VM '{name}' not found")
            if instance.get("status") != "Running":
                raise LxdError(f"VM '{name}' is not running")
            result = await client.execute(name, ["sh", "-c", script], environment={"JETTY_KEY": public_key})
        _require_success(result, ["authorize Jetty port forwarding key"], name)
        host_key = " ".join(result.stdout.decode(errors="replace").split()[:2])
        octet = int(ipaddress.ip_address(row["ip_address"])) % 256
        return SSH_PORT_BASE + octet, host_key

    async def execute_vm(self, name: str, command: list[str]) -> ExecResult:
        name = validation.validate_name(name)
        if name == GATEWAY:
            raise Conflict("The gateway is managed by Jetty")
        if not command or not all(isinstance(item, str) for item in command):
            raise ValidationFailed("command must be a non-empty list of strings", fields={"command": "invalid value"})
        async with LxdClient() as client:
            if await self._instance(client, name) is None:
                raise NotFound(f"VM '{name}' not found")
            return await client.execute(
                name,
                command,
                user=1000,
                group=1000,
                cwd="/home/ubuntu",
                environment={
                    "HOME": "/home/ubuntu",
                    "USER": "ubuntu",
                    "LOGNAME": "ubuntu",
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )

    async def upload_vm_file(
        self,
        name: str,
        guest_path: str,
        content: bytes,
        *,
        mode: int = 0o644,
        owner: str = "ubuntu",
    ) -> None:
        name = validation.validate_name(name)
        if name == GATEWAY:
            raise Conflict("The gateway is managed by Jetty")
        guest = PurePosixPath(guest_path)
        if not guest.is_absolute() or ".." in guest.parts or guest_path in {"", "/"}:
            raise ValidationFailed("path must be an absolute guest file path", fields={"path": "invalid path"})
        if owner not in {"ubuntu", "root"}:
            raise ValidationFailed("owner must be ubuntu or root", fields={"owner": "invalid value"})
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise ValidationFailed("mode must be a file mode from 0000 to 0777", fields={"mode": "invalid mode"})
        uid = 1000 if owner == "ubuntu" else 0
        async with LxdClient() as client:
            if await self._instance(client, name) is None:
                raise NotFound(f"VM '{name}' not found")
            parent = str(guest.parent)
            mkdir = await client.execute(name, ["install", "-d", "-m", "0755", parent])
            _require_success(mkdir, ["install", "-d", "-m", "0755", parent], name)
            await client.put_file(name, str(guest), content, mode, uid=uid, gid=uid)

    async def list_vms(self) -> list[dict[str, Any]]:
        async with LxdClient() as client:
            instances = await self._instances(client)
        return [self._vm_summary(item) for item in instances if item.get("name") != GATEWAY]

    async def ssh_config(self, names: list[str] | None = None) -> str:
        vms = await self.list_vms()
        if names:
            selected = [vm for name in names for vm in vms if vm["name"] == name]
            missing = sorted(set(names) - {vm["name"] for vm in selected})
            if missing:
                raise NotFound(f"VM(s) not found: {', '.join(missing)}")
        else:
            selected = vms
        lines = [f"Host {GATEWAY}", f"  HostName {GATEWAY_UP_IP}", "  User ubuntu", ""]
        for vm in selected:
            address = vm.get("ip_address")
            if not address:
                continue
            octet = int(ipaddress.ip_address(address)) % 256
            lines += [
                f"Host {vm['name']}",
                f"  HostName {GATEWAY_UP_IP}",
                f"  Port {SSH_PORT_BASE + octet}",
                "  User ubuntu",
                f"  HostKeyAlias {vm['name']}",
                "",
            ]
        return "\n".join(lines)

    async def _sync_ssh_config(self) -> None:
        write_jetty_config(await self.ssh_config())

    async def _ensure_project(self, client: LxdClient) -> None:
        projects = await client.request("GET", "/1.0/projects?recursion=1", project=None)
        existing = {item.get("name") for item in projects if isinstance(item, dict)} if isinstance(projects, list) else set()
        if PROJECT not in existing:
            await client.request(
                "POST",
                "/1.0/projects",
                project=None,
                body={
                    "name": PROJECT,
                    "description": "Jetty coding agent virtual machines",
                    "config": {"features.images": "false", "features.profiles": "true"},
                },
            )

    async def _ensure_profile(self, client: LxdClient) -> None:
        profile = await client.request("GET", "/1.0/profiles/default")
        devices = profile.get("devices") or {}
        if "root" not in devices:
            devices["root"] = {"type": "disk", "pool": "default", "path": "/"}
            profile["devices"] = devices
            await client.request("PUT", "/1.0/profiles/default", body=profile)

    async def _ensure_network(self, client: LxdClient, name: str, desired: dict[str, str]) -> None:
        try:
            await client.request("GET", f"/1.0/networks/{quote(name, safe='')}")
        except LxdNotFound:
            await client.request(
                "POST",
                "/1.0/networks",
                body={
                    "name": name,
                    "type": "bridge",
                    "description": f"Jetty {name} network",
                    "config": desired,
                },
            )

    async def _instances(self, client: LxdClient) -> list[dict[str, Any]]:
        result = await client.request("GET", "/1.0/instances?recursion=1")
        return result if isinstance(result, list) else []

    async def _instance(self, client: LxdClient, name: str) -> dict[str, Any] | None:
        try:
            return await client.request("GET", f"/1.0/instances/{quote(name, safe='')}")
        except LxdNotFound:
            return None

    @staticmethod
    def _vm_summary(instance: dict[str, Any]) -> dict[str, Any]:
        devices = instance.get("expanded_devices") or instance.get("devices") or {}
        address = (devices.get("eth0") or {}).get("ipv4.address")
        return {"name": instance.get("name"), "ip_address": address, "status": instance.get("status", "unknown")}

    async def _next_ip(self, client: LxdClient) -> str:
        used = {
            item["ip_address"]
            for item in (self._vm_summary(vm) for vm in await self._instances(client))
            if item.get("ip_address")
        }
        for octet in range(11, 251):
            address = f"{PRIV_PREFIX}.{octet}"
            if address not in used and not vms_repo.ip_in_use_by_other(self.db, address, exclude_name=""):
                return address
        raise Conflict(f"No free agent addresses remain in {PRIV_PREFIX}.0/24")

    async def _start(self, client: LxdClient, name: str) -> None:
        instance = await self._instance(client, name)
        if instance and instance.get("status") == "Running":
            return
        await client.request(
            "PUT",
            f"/1.0/instances/{quote(name, safe='')}/state",
            body={"action": "start", "timeout": 60},
        )

    async def _wait_for_agent(self, client: LxdClient, name: str) -> None:
        """Wait up to five minutes for the VM's LXD guest agent to answer exec."""
        deadline = time.monotonic() + 300
        last_error: LxdError | None = None
        while time.monotonic() < deadline:
            try:
                async with asyncio.timeout(min(15, deadline - time.monotonic())):
                    result = await client.execute(name, ["true"])
                if result.exit_code == 0:
                    return
                last_error = LxdError(f"guest agent readiness command exited {result.exit_code}")
            except LxdError as exc:
                last_error = exc
            except TimeoutError:
                last_error = LxdError("guest agent readiness command timed out")
            await asyncio.sleep(2)
        detail = f": {last_error}" if last_error else ""
        raise LxdError(f"{name}: LXD guest agent did not become available within five minutes{detail}")

    async def _configure_gateway(self, client: LxdClient) -> None:
        instance = await self._instance(client, GATEWAY)
        if not instance:
            raise LxdError("LXD did not return the Jetty gateway after creating it")
        config_values = instance.get("config") or {}
        up_mac = config_values.get("volatile.eth0.hwaddr")
        private_mac = config_values.get("volatile.eth1.hwaddr")
        if not up_mac or not private_mac:
            raise LxdError("LXD did not assign network MAC addresses to the Jetty gateway")
        mkdir = await client.execute(
            GATEWAY, ["install", "-d", "-m", "700", "/etc/jetty-gateway"]
        )
        _require_success(mkdir, ["install", "-d", "-m", "700", "/etc/jetty-gateway"], GATEWAY)
        keys = config.tunnel_keys_path().read_text(encoding="utf-8")
        key_data = json.loads(keys)
        import mitmproxy_rs

        server_pub = mitmproxy_rs.pubkey(key_data["server_key"])
        env = (
            f"UP_MAC={up_mac}\nPRIV_MAC={private_mac}\nPRIV_ADDR={GATEWAY_PRIV_IP}/24\n"
            f"SERVER_PUB={server_pub}\nENDPOINT={UP_ADDR}:{TUNNEL_PORT}\nHOST_ADDR={UP_ADDR}\n"
            f"SSH_PORT_BASE={SSH_PORT_BASE}\n"
        )
        await client.put_file(GATEWAY, "/etc/jetty-gateway/client.key", (key_data["client_key"] + "\n").encode(), 0o600)
        await client.put_file(GATEWAY, "/etc/jetty-gateway/env", env.encode(), 0o600)
        await client.put_file(GATEWAY, "/usr/local/sbin/jetty-gateway-up", gateway_asset("jetty-gateway-up"), 0o755)
        await client.put_file(GATEWAY, "/etc/systemd/system/jetty-gateway.service", gateway_asset("jetty-gateway.service"), 0o644)
        for command in (
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "jetty-gateway.service"],
            ["systemctl", "restart", "jetty-gateway.service"],
        ):
            result = await client.execute(GATEWAY, command)
            _require_success(result, command, GATEWAY)
