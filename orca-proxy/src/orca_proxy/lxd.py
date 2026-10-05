"""Small asynchronous client for LXD's local Unix-socket REST API.

Local socket access is intentionally used here: a user in the ``lxd`` group
has the same effective authority as root, which is already the trust boundary
for Jetty's host-side management service.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector

_DEFAULT_PROJECT = object()


class LxdError(RuntimeError):
    """A local LXD API request failed."""


class LxdNotFound(LxdError):
    """The requested LXD resource does not exist."""


def socket_path() -> Path:
    """Return the same socket path order used by the LXD Go client."""
    configured = os.environ.get("LXD_SOCKET")
    if configured:
        return Path(configured)
    lxd_dir = os.environ.get("LXD_DIR")
    if lxd_dir:
        return Path(lxd_dir) / "unix.socket"
    snap = Path("/var/snap/lxd/common/lxd/unix.socket")
    if snap.exists() and os.access(snap, os.W_OK):
        return snap
    return Path("/var/lib/lxd/unix.socket")


def socket_accessible(path: Path | None = None) -> bool:
    path = path or socket_path()
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISSOCK(info.st_mode) and os.access(path, os.R_OK | os.W_OK)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class LxdClient:
    """Use LXD over its local Unix socket and wait for background operations."""

    def __init__(self, project: str = "jetty", path: Path | None = None):
        self.project = project
        self.path = path or socket_path()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "LxdClient":
        if not socket_accessible(self.path):
            raise LxdError(
                f"LXD socket is not accessible at {self.path}. Install LXD and sign in again "
                "after adding your account to the lxd group."
            )
        timeout = ClientTimeout(total=900, sock_connect=15, sock_read=60)
        self._session = ClientSession(connector=UnixConnector(path=str(self.path)), timeout=timeout)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def _project_path(path: str, project: str | None) -> str:
        if project is None:
            return path
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}project={quote(project, safe='')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | None, bytes, str]:
        if self._session is None:
            raise LxdError("LXD client is not open")
        url = "http://unix.socket" + path
        try:
            async with self._session.request(method, url, json=json_body, data=data, headers=headers) as response:
                raw = await response.read()
                content_type = response.headers.get("Content-Type", "")
                payload = None
                if "json" in content_type:
                    try:
                        payload = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                if response.status >= 400 or (payload and payload.get("type") == "error"):
                    message = payload.get("error") if payload else raw.decode(errors="replace")
                    error = f"LXD {method} {path}: {message or response.reason}"
                    if response.status == 404:
                        raise LxdNotFound(error)
                    raise LxdError(error)
                return response.status, payload, raw, content_type
        except (ClientError, OSError, asyncio.TimeoutError) as exc:
            raise LxdError(f"LXD {method} {path}: {exc}") from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        project: str | None | object = _DEFAULT_PROJECT,
        wait: bool = True,
    ) -> Any:
        active_project = self.project if project is _DEFAULT_PROJECT else project
        request_path = self._project_path(path, active_project)  # type: ignore[arg-type]
        _, envelope, _, _ = await self._request(method, request_path, json_body=body)
        if not envelope:
            raise LxdError(f"LXD {method} {request_path} returned an invalid response")
        if envelope.get("type") == "async":
            if not wait:
                return envelope
            operation = envelope.get("operation") or envelope.get("metadata", {}).get("id")
            if not operation:
                raise LxdError(f"LXD {method} {request_path} did not return an operation URL")
            return await self.wait_operation(operation)
        if envelope.get("type") != "sync":
            raise LxdError(f"LXD {method} {request_path} returned an unknown response")
        return envelope.get("metadata")

    async def wait_operation(self, operation: str) -> dict:
        if not operation.startswith("/1.0/"):
            operation = "/1.0/operations/" + quote(operation, safe="")
        deadline = asyncio.get_running_loop().time() + 900
        while True:
            wait_path = operation + ("&" if "?" in operation else "?") + "timeout=30"
            _, envelope, _, _ = await self._request("GET", wait_path)
            if not envelope or envelope.get("type") == "error":
                raise LxdError(f"LXD operation {operation} returned an invalid response")
            metadata = envelope.get("metadata") or {}
            code = int(metadata.get("status_code", 0))
            if code in (200, 112, 400, 401):
                if code != 200:
                    raise LxdError(metadata.get("err") or f"LXD operation failed ({code})")
                return metadata.get("metadata") or {}
            if asyncio.get_running_loop().time() >= deadline:
                raise LxdError(f"LXD operation timed out: {operation}")

    async def get_file(self, instance: str, guest_path: str) -> bytes:
        path = f"/1.0/instances/{quote(instance, safe='')}/files?path={quote(guest_path, safe='/')}"
        _, _, content, _ = await self._request("GET", self._project_path(path, self.project))
        return content

    async def put_file(
        self,
        instance: str,
        guest_path: str,
        content: bytes,
        mode: int = 0o644,
        *,
        uid: int = 0,
        gid: int = 0,
    ) -> None:
        path = f"/1.0/instances/{quote(instance, safe='')}/files?path={quote(guest_path, safe='/')}"
        path = self._project_path(path, self.project)
        await self._request(
            "POST",
            path,
            data=content,
            headers={
                "Content-Type": "application/octet-stream",
                "X-LXD-uid": str(uid),
                "X-LXD-gid": str(gid),
                "X-LXD-mode": f"{mode:o}",
            },
        )

    async def delete_file(self, instance: str, guest_path: str) -> None:
        path = f"/1.0/instances/{quote(instance, safe='')}/files?path={quote(guest_path, safe='/')}"
        await self._request("DELETE", self._project_path(path, self.project))

    async def execute(
        self,
        instance: str,
        command: list[str],
        *,
        user: int = 0,
        group: int = 0,
        cwd: str = "/root",
        environment: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run a noninteractive command and collect its recorded output."""
        path = f"/1.0/instances/{quote(instance, safe='')}/exec"
        payload: dict[str, Any] = {
            "command": command,
            "interactive": False,
            "record-output": True,
            "user": user,
            "group": group,
            "cwd": cwd,
        }
        if environment:
            payload["environment"] = environment
        envelope = await self.request("POST", path, body=payload, wait=False)
        operation = envelope.get("operation") or envelope.get("metadata", {}).get("id")
        if not operation:
            raise LxdError(f"LXD exec for {instance} did not return an operation URL")
        operation_result = await self.wait_operation(operation)
        output_files = operation_result.get("output", {})
        if not isinstance(output_files, dict):
            output_files = {}
        stdout = bytearray()
        stderr = bytearray()
        for stream_id, target in (("1", stdout), ("2", stderr)):
            output_path = output_files.get(stream_id)
            if not isinstance(output_path, str) or not output_path:
                continue
            filename = output_path.rsplit("/", 1)[-1]
            output_endpoint = (
                f"/1.0/instances/{quote(instance, safe='')}/logs/exec-output/"
                f"{quote(filename, safe='')}"
            )
            _, _, data, _ = await self._request(
                "GET", self._project_path(output_endpoint, self.project)
            )
            target.extend(data)
            await self._request(
                "DELETE", self._project_path(output_endpoint, self.project)
            )
        return ExecResult(
            int(operation_result.get("return", 0)),
            bytes(stdout),
            bytes(stderr),
        )
