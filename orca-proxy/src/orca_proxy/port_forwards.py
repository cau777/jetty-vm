"""Port Forwards and Reverse Forwards between this computer and a Jetty VM.

A Port Forward is a supervised `ssh -N -L` that exposes a VM port on the
host's 127.0.0.1; a Reverse Forward is a supervised `ssh -N -R` that exposes a
host port on the VM's 127.0.0.1. Both run from the daemon through the
gateway's per-VM SSH port, so neither opens a route through the gateway's
firewall. SSH reaches services the VM binds to localhost only, which is how
most development servers listen. The daemon uses its own key, authorized on
the VM over the LXD socket, so it never depends on the user's SSH agent or key
passphrase.

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
from .repo import reverse_forwards as reverse_forwards_repo
from .repo import vms as vms_repo

log = logging.getLogger("jetty.port_forwards")

READY_TIMEOUT_SECONDS = 15.0
RETRY_MIN_SECONDS = 2.0
RETRY_MAX_SECONDS = 30.0
# Progress lines ssh prints at DEBUG1 that are never a failure reason.
_SSH_CHATTER = ("debug", "Authenticated to ", "Transferred: ", "Bytes per second: ")


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
    reverse: bool = False  # True: host_port is exposed inside the VM on vm_port
    state: str = "starting"  # starting | active | retrying
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def key(self) -> int | tuple[str, int]:
        """Port Forwards own a host port; Reverse Forwards own one VM's port."""
        return (self.vm_name, self.vm_port) if self.reverse else self.host_port

    def to_json(self) -> dict:
        body = {
            "vm_name": self.vm_name,
            "vm_port": self.vm_port,
            "host_port": self.host_port,
            "persistent": self.persistent,
            "state": self.state,
            "error": self.error,
        }
        if not self.reverse:
            body["url"] = f"http://localhost:{self.host_port}"
        return body


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
    """One running `ssh -N`, with its recent stderr kept as the failure reason.

    Reverse Forwards run ssh at DEBUG1, the only level that reports the
    server's answer to `-R`; its success line sets `remote_ready`, and that
    level's chatter is dropped so it never becomes the failure reason.
    """

    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.lines: collections.deque[str] = collections.deque(maxlen=5)
        self.remote_ready = asyncio.Event()
        self._drain = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        async for raw in self.process.stderr:
            line = raw.decode(errors="replace").strip()
            if line.startswith("debug1: remote forward success"):
                self.remote_ready.set()
            elif line and not line.startswith(_SSH_CHATTER):
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
        if forward.reverse:
            spec = ["-o", "LogLevel=DEBUG1", "-R", f"127.0.0.1:{forward.vm_port}:localhost:{forward.host_port}"]
        else:
            spec = ["-o", "LogLevel=ERROR", "-L", f"127.0.0.1:{forward.host_port}:localhost:{forward.vm_port}"]
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
            *spec,
            "-p", str(ssh_port),
            f"ubuntu@{GATEWAY_UP_IP}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        tunnel = _SshTunnel(process)
        try:
            await asyncio.wait_for(self._ready(tunnel, forward), READY_TIMEOUT_SECONDS)
        except TimeoutError:
            await tunnel.stop()
            where = "VM" if forward.reverse else "host"
            raise PortForwardError(f"SSH did not open the {where} port in time") from None
        except BaseException:
            await tunnel.stop()
            raise
        return tunnel

    @staticmethod
    async def _ready(tunnel: _SshTunnel, forward: PortForward) -> None:
        """Return once the forward listens; raise with ssh's reason if it exits first."""
        process = tunnel.process
        if forward.reverse:
            exited = asyncio.create_task(process.wait())
            ready = asyncio.create_task(tunnel.remote_ready.wait())
            try:
                await asyncio.wait({exited, ready}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                exited.cancel()
                ready.cancel()
            if tunnel.remote_ready.is_set() and process.returncode is None:
                return
        else:
            while process.returncode is None:
                try:
                    _reader, writer = await asyncio.open_connection("127.0.0.1", forward.host_port)
                except OSError:
                    await asyncio.sleep(0.2)
                    continue
                writer.close()
                return
        await tunnel.wait()
        raise PortForwardError(tunnel.error or f"SSH exited with status {process.returncode}")


class _Supervisor:
    """Supervises one ssh process per forward, retrying with backoff until stopped."""

    def __init__(self, conn: sqlite3.Connection, connector: Connector):
        self._conn = conn
        self._connector = connector
        self._forwards: dict = {}

    def list(self, vm_name: str | None = None) -> list[dict]:
        return [
            forward.to_json()
            for _key, forward in sorted(self._forwards.items())
            if vm_name is None or forward.vm_name == vm_name
        ]

    async def drop_vm(self, vm_name: str) -> None:
        """Stop a deleted VM's forwards; its stored rows go with the VM (ON DELETE CASCADE)."""
        for forward in [f for f in self._forwards.values() if f.vm_name == vm_name]:
            await self._stop(forward)

    async def shutdown(self) -> None:
        """Stop every tunnel at daemon exit, keeping persistent forwards stored."""
        for forward in list(self._forwards.values()):
            await self._stop(forward)

    def _start(self, forward: PortForward) -> PortForward:
        self._forwards[forward.key] = forward
        forward.task = asyncio.create_task(
            self._supervise(forward), name=f"jetty-forward-{forward.vm_name}-{forward.vm_port}-{forward.host_port}"
        )
        return forward

    async def _stop(self, forward: PortForward) -> None:
        self._forwards.pop(forward.key, None)
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
            arrow = "<-" if forward.reverse else "->"
            log.warning("Forward localhost:%s %s %s:%s: %s", forward.host_port, arrow, forward.vm_name, forward.vm_port, forward.error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)


def _validate_persistent(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValidationFailed("'persistent' must be a boolean", fields={"persistent": "invalid value"})
    return value


class PortForwardManager(_Supervisor):
    """Port Forwards, identified by the host port they listen on."""

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
        persistent = _validate_persistent(persistent)
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
        persistent = _validate_persistent(persistent)
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

    def start_persistent(self) -> None:
        for row in port_forwards_repo.list_all(self._conn):
            if row["host_port"] not in self._forwards:
                self._start(PortForward(row["vm_name"], row["vm_port"], row["host_port"], True))

    def _get(self, vm_name: str, host_port: int) -> PortForward:
        forward = self._forwards.get(host_port)
        if forward is None or forward.vm_name != vm_name:
            raise NotFound(f"No port forward on host port {host_port} for VM '{vm_name}'")
        return forward


class ReverseForwardManager(_Supervisor):
    """Reverse Forwards, identified by the VM and the VM port they listen on.

    The VM side binds as the unprivileged `ubuntu` user, so VM ports start at
    1024. Several VMs may reach the same host port.
    """

    def open(self, vm_name: str, host_port: object, vm_port: object = None, persistent: object = False) -> dict:
        if vms_repo.get(self._conn, vm_name) is None:
            raise NotFound(f"VM '{vm_name}' not found")
        host_port = _validate_port(host_port, "host_port")
        if vm_port is None:
            if host_port < 1024:
                raise ValidationFailed(
                    "Choose a VM port of 1024 or higher for this privileged host port",
                    fields={"vm_port": "required"},
                )
            vm_port = host_port
        vm_port = _validate_port(vm_port, "vm_port", minimum=1024)
        persistent = _validate_persistent(persistent)
        existing = self._forwards.get((vm_name, vm_port))
        if existing is not None:
            raise Conflict(
                f"VM port {vm_port} on {vm_name} already reaches host port {existing.host_port}",
                fields={"vm_port": "in use"},
            )
        if persistent:
            reverse_forwards_repo.put(self._conn, vm_name, vm_port, host_port)
        return self._start(PortForward(vm_name, vm_port, host_port, persistent, reverse=True)).to_json()

    def set_persistent(self, vm_name: str, vm_port: int, persistent: object) -> dict:
        forward = self._get(vm_name, vm_port)
        persistent = _validate_persistent(persistent)
        if persistent:
            reverse_forwards_repo.put(self._conn, vm_name, vm_port, forward.host_port)
        else:
            reverse_forwards_repo.delete(self._conn, vm_name, vm_port)
        forward.persistent = persistent
        return forward.to_json()

    async def close(self, vm_name: str, vm_port: int) -> None:
        forward = self._get(vm_name, vm_port)
        reverse_forwards_repo.delete(self._conn, vm_name, vm_port)
        await self._stop(forward)

    def start_persistent(self) -> None:
        for row in reverse_forwards_repo.list_all(self._conn):
            if (row["vm_name"], row["vm_port"]) not in self._forwards:
                self._start(PortForward(row["vm_name"], row["vm_port"], row["host_port"], True, reverse=True))

    def _get(self, vm_name: str, vm_port: int) -> PortForward:
        forward = self._forwards.get((vm_name, vm_port))
        if forward is None:
            raise NotFound(f"No reverse forward on VM port {vm_port} for VM '{vm_name}'")
        return forward
