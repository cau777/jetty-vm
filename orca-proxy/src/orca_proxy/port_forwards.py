"""Port Forwards: host loopback ports tunneled to a service inside a Jetty VM.

Each forward is a supervised `ssh -N -L` from the daemon through the
gateway's per-VM SSH port. SSH reaches services the VM binds to localhost
only, which is how most development servers listen. The daemon uses its own
key, authorized on the VM over the LXD socket, so it never depends on the
user's SSH agent or key passphrase. Forwards bind 127.0.0.1 only.

One-time forwards live in memory until closed or the daemon stops;
persistent forwards are stored in SQLite and reopened when the daemon starts.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import socket
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import config
from .errors import Conflict, NotFound, ValidationFailed
from .jetty import GATEWAY_UP_IP
from .repo import port_forwards as port_forwards_repo
from .repo import vms as vms_repo

log = logging.getLogger("jetty.port_forwards")

READY_TIMEOUT_SECONDS = 15.0
RETRY_MIN_SECONDS = 2.0
RETRY_MAX_SECONDS = 30.0


class Tunnel(Protocol):
    error: str | None

    async def wait(self) -> int: ...

    async def stop(self) -> None: ...


@dataclass
class PortForward:
    vm_name: str
    vm_port: int
    host_port: int
    persistent: bool
    state: str = "starting"  # starting | active | retrying
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_json(self) -> dict:
        return {
            "vm_name": self.vm_name,
            "vm_port": self.vm_port,
            "host_port": self.host_port,
            "persistent": self.persistent,
            "state": self.state,
            "error": self.error,
            "url": f"http://localhost:{self.host_port}",
        }


class PortForwardError(Exception):
    pass


Connector = Callable[[PortForward], Awaitable[Tunnel]]
Preparer = Callable[[str, str], Awaitable[tuple[int, str]]]


def _validate_port(value: object, field_name: str, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= 65535:
        raise ValidationFailed(
            f"'{field_name}' must be a port between {minimum} and 65535",
            fields={field_name: "invalid port"},
        )
    return value


def _host_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class _SshTunnel:
    """One running `ssh -N -L`, with its recent stderr kept as the failure reason."""

    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.lines: collections.deque[str] = collections.deque(maxlen=5)
        self._drain = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        async for raw in self.process.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                self.lines.append(line)

    @property
    def error(self) -> str | None:
        return self.lines[-1] if self.lines else None

    async def wait(self) -> int:
        code = await self.process.wait()
        await asyncio.gather(self._drain, return_exceptions=True)
        return code

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await asyncio.gather(self._drain, return_exceptions=True)


class SshConnector:
    """Open forwards with Jetty's own key, verifying the VM's host key strictly."""

    def __init__(self, prepare: Preparer, directory: Path | None = None):
        self._prepare = prepare
        self._directory = directory
        self._key_lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        path = self._directory or config.data_dir() / "port-forwards"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    async def _key(self) -> Path:
        key = self.directory / "id_ed25519"
        async with self._key_lock:
            if not key.exists():
                process = await asyncio.create_subprocess_exec(
                    "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jetty-port-forward", "-f", str(key),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _out, err = await process.communicate()
                if process.returncode != 0:
                    raise PortForwardError(f"Could not create Jetty's forwarding key: {err.decode().strip()}")
        return key

    async def __call__(self, forward: PortForward) -> Tunnel:
        key = await self._key()
        public_key = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        ssh_port, host_key = await self._prepare(forward.vm_name, public_key)
        known_hosts = self.directory / f"{forward.vm_name}.known_hosts"
        known_hosts.write_text(f"{forward.vm_name} {host_key}\n", encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            "ssh", "-F", "/dev/null", "-N", "-T",
            "-i", str(key),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"HostKeyAlias={forward.vm_name}",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "LogLevel=ERROR",
            "-p", str(ssh_port),
            "-L", f"127.0.0.1:{forward.host_port}:localhost:{forward.vm_port}",
            f"ubuntu@{GATEWAY_UP_IP}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        tunnel = _SshTunnel(process)
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT_SECONDS
        while True:
            if process.returncode is not None:
                await tunnel.wait()
                raise PortForwardError(tunnel.error or f"SSH exited with status {process.returncode}")
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", forward.host_port)
            except OSError:
                if asyncio.get_running_loop().time() > deadline:
                    await tunnel.stop()
                    raise PortForwardError("SSH did not open the host port in time") from None
                await asyncio.sleep(0.2)
                continue
            writer.close()
            return tunnel


class PortForwardManager:
    def __init__(self, conn: sqlite3.Connection, connector: Connector):
        self._conn = conn
        self._connector = connector
        self._forwards: dict[int, PortForward] = {}

    def list(self, vm_name: str | None = None) -> list[dict]:
        return [
            forward.to_json()
            for port, forward in sorted(self._forwards.items())
            if vm_name is None or forward.vm_name == vm_name
        ]

    def open(self, vm_name: str, vm_port: object, host_port: object = None, persistent: object = False) -> dict:
        if vms_repo.get(self._conn, vm_name) is None:
            raise NotFound(f"VM '{vm_name}' not found")
        vm_port = _validate_port(vm_port, "vm_port")
        if host_port is None:
            if vm_port < 1024:
                raise ValidationFailed(
                    "Choose a host port of 1024 or higher for this privileged VM port",
                    fields={"host_port": "required"},
                )
            host_port = vm_port
        host_port = _validate_port(host_port, "host_port", minimum=1024)
        if not isinstance(persistent, bool):
            raise ValidationFailed("'persistent' must be a boolean", fields={"persistent": "invalid value"})
        existing = self._forwards.get(host_port)
        if existing is not None:
            raise Conflict(
                f"Host port {host_port} already forwards to {existing.vm_name}:{existing.vm_port}",
                fields={"host_port": "in use"},
            )
        if not _host_port_free(host_port):
            raise Conflict(f"Host port {host_port} is already in use on this computer", fields={"host_port": "in use"})
        if persistent:
            port_forwards_repo.put(self._conn, host_port, vm_name, vm_port)
        return self._start(PortForward(vm_name, vm_port, host_port, persistent)).to_json()

    def set_persistent(self, vm_name: str, host_port: int, persistent: object) -> dict:
        forward = self._get(vm_name, host_port)
        if not isinstance(persistent, bool):
            raise ValidationFailed("'persistent' must be a boolean", fields={"persistent": "invalid value"})
        if persistent:
            port_forwards_repo.put(self._conn, host_port, vm_name, forward.vm_port)
        else:
            port_forwards_repo.delete(self._conn, host_port)
        forward.persistent = persistent
        return forward.to_json()

    async def close(self, vm_name: str, host_port: int) -> None:
        forward = self._get(vm_name, host_port)
        port_forwards_repo.delete(self._conn, host_port)
        await self._stop(forward)

    async def drop_vm(self, vm_name: str) -> None:
        """Stop a deleted VM's forwards; its stored rows go with the VM (ON DELETE CASCADE)."""
        for forward in [f for f in self._forwards.values() if f.vm_name == vm_name]:
            await self._stop(forward)

    def start_persistent(self) -> None:
        for row in port_forwards_repo.list_all(self._conn):
            if row["host_port"] not in self._forwards:
                self._start(PortForward(row["vm_name"], row["vm_port"], row["host_port"], True))

    async def shutdown(self) -> None:
        """Stop every tunnel at daemon exit, keeping persistent forwards stored."""
        for forward in list(self._forwards.values()):
            await self._stop(forward)

    def _get(self, vm_name: str, host_port: int) -> PortForward:
        forward = self._forwards.get(host_port)
        if forward is None or forward.vm_name != vm_name:
            raise NotFound(f"No port forward on host port {host_port} for VM '{vm_name}'")
        return forward

    def _start(self, forward: PortForward) -> PortForward:
        self._forwards[forward.host_port] = forward
        forward.task = asyncio.create_task(
            self._supervise(forward), name=f"jetty-port-forward-{forward.host_port}"
        )
        return forward

    async def _stop(self, forward: PortForward) -> None:
        self._forwards.pop(forward.host_port, None)
        if forward.task is not None:
            forward.task.cancel()
            await asyncio.gather(forward.task, return_exceptions=True)

    async def _supervise(self, forward: PortForward) -> None:
        delay = RETRY_MIN_SECONDS
        while True:
            forward.state = "starting"
            try:
                tunnel = await self._connector(forward)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                forward.error = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            else:
                forward.state = "active"
                forward.error = None
                delay = RETRY_MIN_SECONDS
                try:
                    code = await tunnel.wait()
                finally:
                    await tunnel.stop()
                forward.error = tunnel.error or f"SSH exited with status {code}"
            forward.state = "retrying"
            log.warning("Port forward %s -> %s:%s: %s", forward.host_port, forward.vm_name, forward.vm_port, forward.error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
