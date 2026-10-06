"""The Jetty command line, service entry point and appimage setup dispatcher."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config


def _base_url() -> str:
    override = os.environ.get("JETTY_MANAGEMENT_URL")
    return override or f"http://127.0.0.1:{config.management_api_port()}"


def _api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        _base_url() + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            details = json.loads(payload)
            message = details.get("error", {}).get("message", str(exc))
        except (json.JSONDecodeError, AttributeError):
            message = payload.decode(errors="replace") or str(exc)
        raise RuntimeError(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Jetty's daemon is unavailable at {_base_url()}. Start it with `systemctl --user start jetty-daemon.service`."
        ) from exc
    return json.loads(payload) if payload else {}


def _wait_for_job(job: dict) -> dict:
    """Follow an asynchronous Jetty operation so CLI commands remain blocking."""
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Jetty returned an invalid operation job")
    last_step = None
    while True:
        state = job.get("state")
        if state == "done":
            result = job.get("result")
            return result if isinstance(result, dict) else {}
        if state in {"failed", "cancelled"}:
            error = job.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(message or f"Jetty operation {state}")
        current = job.get("current_step")
        if current != last_step:
            labels = {step.get("key"): step.get("label") for step in job.get("steps", [])}
            label = labels.get(current)
            if label:
                print(label, file=sys.stderr)
            last_step = current
        time.sleep(0.8)
        job = _api("GET", f"/api/v1/jetty/jobs/{urllib.parse.quote(job_id, safe='')}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jetty", description="Jetty desktop app and VM management CLI")
    parser.add_argument("--version", action="version", version=f"Jetty {installed_version()}")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("daemon", help="run the background management service")
    modes.add_parser("tray", help="run the desktop tray and management window")
    modes.add_parser("setup", help="install this AppImage for the current user")
    modes.add_parser("update", help="download and install the latest verified release")
    modes.add_parser("status", help="show daemon, proxy, gateway and VM status")

    gateway = modes.add_parser("gateway", help="manage the Jetty gateway")
    gateway_modes = gateway.add_subparsers(dest="gateway_mode", required=True)
    gateway_setup = gateway_modes.add_parser("setup", help="create or repair Jetty networks and gateway")
    gateway_setup.add_argument("--ssh-key", help="path to an SSH public key")

    vm = modes.add_parser("vm", help="manage agent virtual machines")
    vm_modes = vm.add_subparsers(dest="vm_mode", required=True)
    create = vm_modes.add_parser("create", help="create and register an agent VM")
    create.add_argument("name")
    create.add_argument("--cpus", type=int, default=4)
    create.add_argument("--memory", default="8GiB")
    create.add_argument("--disk", default="40GiB")
    create.add_argument("--image", default="ubuntu:24.04")
    create.add_argument("--ssh-key", help="path to an SSH public key")
    for action in ("delete", "start", "stop", "restart", "ip"):
        action_parser = vm_modes.add_parser(action)
        action_parser.add_argument("name")
    vm_modes.add_parser("list")
    vm_exec = vm_modes.add_parser("exec", help="run a command as ubuntu inside a VM")
    vm_exec.add_argument("name")
    vm_exec.add_argument("command", nargs=argparse.REMAINDER)
    vm_upload = vm_modes.add_parser("upload", help="copy a host file to an agent VM (reads stdin when --file is omitted)")
    vm_upload.add_argument("name")
    vm_upload.add_argument("guest_path")
    vm_upload.add_argument("--file", help="host file to upload; stdin is used by default")
    vm_upload.add_argument("--mode", type=lambda value: int(value, 8), default=0o644, help="guest file mode in octal")
    vm_upload.add_argument("--owner", choices=("ubuntu", "root"), default="ubuntu")
    vm_port = vm_modes.add_parser("port", help="forward VM ports to localhost on this computer")
    port_modes = vm_port.add_subparsers(dest="port_mode", required=True)
    port_open = port_modes.add_parser("open", help="forward a VM port; one-time unless --persistent")
    port_open.add_argument("name")
    port_open.add_argument("vm_port", type=int)
    port_open.add_argument("--host-port", type=int, help="localhost port on this computer (default: the VM port)")
    port_open.add_argument("--persistent", action="store_true", help="reopen whenever Jetty starts")
    port_list = port_modes.add_parser("list", help="list port forwards")
    port_list.add_argument("name", nargs="?")
    port_close = port_modes.add_parser("close", help="stop a port forward")
    port_close.add_argument("name")
    port_close.add_argument("host_port", type=int)

    ssh = modes.add_parser("ssh-config", help="print SSH entries for the gateway and agent VMs")
    ssh.add_argument("names", nargs="*")
    tunnel = modes.add_parser("tunnel", help="print gateway WireGuard material")
    tunnel.add_argument("tunnel_mode", choices=("gateway",))
    return parser


def _read_ssh_key(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not read SSH public key {path}: {exc}") from exc


def _status() -> int:
    daemon = _api("GET", "/api/v1/status")
    jetty = _api("GET", "/api/v1/jetty/status")
    print(f"daemon: {daemon.get('daemon', 'unknown')}")
    print(f"proxy: {daemon.get('proxy', 'unknown')}" + (f" ({daemon['proxy_error']})" if daemon.get("proxy_error") else ""))
    print(f"lxd: {'available' if jetty.get('lxd_available') else 'unavailable'}")
    print(f"gateway: {jetty.get('gateway', 'unknown')}")
    print(f"gateway service: {jetty.get('gateway_service', 'unknown')}")
    print(f"tunnel: {jetty.get('tunnel', 'unknown')}")
    for vm in jetty.get("vms", []):
        print(f"{vm.get('name')}\t{vm.get('ip_address') or '—'}\t{vm.get('status', 'unknown')}")
    if jetty.get("message"):
        print(jetty["message"])
    return 0


def _vm(args) -> int:
    encoded_name = urllib.parse.quote(getattr(args, "name", None) or "", safe="")
    if args.vm_mode == "create":
        body = {
            "name": args.name,
            "cpus": args.cpus,
            "memory": args.memory,
            "disk": args.disk,
            "image": args.image,
        }
        ssh_key = _read_ssh_key(args.ssh_key)
        if ssh_key:
            body["ssh_public_key"] = ssh_key
        vm = _wait_for_job(_api("POST", "/api/v1/jetty/vms", body))
        print(f"{vm['name']} is {vm['status']} at {vm['ip_address']}")
        return 0
    if args.vm_mode == "list":
        result = _api("GET", "/api/v1/jetty/vms")
        for vm in result.get("vms", []):
            print(f"{vm.get('name')}\t{vm.get('ip_address') or '—'}\t{vm.get('status', 'unknown')}")
        return 0
    if args.vm_mode == "ip":
        result = _api("GET", "/api/v1/jetty/vms")
        for vm in result.get("vms", []):
            if vm.get("name") == args.name:
                print(vm.get("ip_address", ""))
                return 0
        raise RuntimeError(f"VM '{args.name}' was not found")
    if args.vm_mode == "delete":
        _api("DELETE", f"/api/v1/jetty/vms/{encoded_name}")
        print(f"{args.name} deleted")
        return 0
    if args.vm_mode in {"start", "stop", "restart"}:
        response = _api("POST", f"/api/v1/jetty/vms/{encoded_name}/action", {"action": args.vm_mode})
        print(f"{args.name}: {response.get('status', 'unknown')}")
        return 0
    if args.vm_mode == "exec":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise RuntimeError("Usage: jetty vm exec NAME -- COMMAND [ARGUMENTS…]")
        response = _api("POST", f"/api/v1/jetty/vms/{encoded_name}/exec", {"command": command})
        sys.stdout.buffer.write(base64.b64decode(response.get("stdout_b64", "")))
        sys.stderr.buffer.write(base64.b64decode(response.get("stderr_b64", "")))
        return int(response.get("exit_code", 0))
    if args.vm_mode == "upload":
        content = Path(args.file).expanduser().read_bytes() if args.file else sys.stdin.buffer.read()
        body = {
            "path": args.guest_path,
            "content_b64": base64.b64encode(content).decode("ascii"),
            "mode": args.mode,
            "owner": args.owner,
        }
        response = _api("POST", f"/api/v1/jetty/vms/{encoded_name}/files", body)
        print(f"Uploaded {response['bytes_written']} bytes to {args.name}:{response['path']}")
        return 0
    if args.vm_mode == "port":
        return _vm_port(args, encoded_name)
    return 2


def _vm_port(args, encoded_name: str) -> int:
    if args.port_mode == "open":
        body: dict = {"vm_port": args.vm_port, "persistent": args.persistent}
        if args.host_port is not None:
            body["host_port"] = args.host_port
        forward = _api("POST", f"/api/v1/jetty/vms/{encoded_name}/ports", body)
        kind = "persistent" if forward["persistent"] else "one-time"
        print(f"{forward['url']} -> {args.name}:{forward['vm_port']} ({kind})")
        return 0
    if args.port_mode == "list":
        query = f"?vm={encoded_name}" if args.name else ""
        for forward in _api("GET", f"/api/v1/jetty/ports{query}").get("ports", []):
            kind = "persistent" if forward["persistent"] else "one-time"
            line = f"{forward['url']}\t{forward['vm_name']}:{forward['vm_port']}\t{kind}\t{forward['state']}"
            print(line + (f"\t{forward['error']}" if forward.get("error") else ""))
        return 0
    if args.port_mode == "close":
        _api("DELETE", f"/api/v1/jetty/vms/{encoded_name}/ports/{args.host_port}")
        print(f"Closed localhost:{args.host_port}")
        return 0
    return 2


def installed_version() -> str:
    env_version = os.environ.get("JETTY_RELEASE_VERSION")
    if env_version:
        return env_version
    if getattr(sys, "frozen", False):
        version_path = Path(getattr(sys, "_MEIPASS")) / "orca_proxy" / "VERSION"
    else:
        version_path = Path(__file__).resolve().parents[3] / "VERSION"
    try:
        return version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "development"


def _update() -> int:
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("The published Jetty AppImage currently supports x86_64 Linux only.")
    repository = os.environ.get("JETTY_REPOSITORY", "cau777/jetty-vm")
    headers = {"User-Agent": "Jetty-Desktop-Updater"}
    url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read the latest Jetty release: {exc}") from exc
    tag = release.get("tag_name", "")
    if tag == installed_version():
        print(f"Jetty {tag} is already current.")
        return 0
    assets = {asset.get("name"): asset.get("browser_download_url") for asset in release.get("assets", [])}
    image_name = "jetty-x86_64.AppImage"
    checksum_name = image_name + ".sha256"
    if image_name not in assets or checksum_name not in assets:
        raise RuntimeError(f"Release {tag} does not contain the expected AppImage and checksum.")

    target = Path.home() / ".local/bin/jetty"
    if not target.is_file():
        raise RuntimeError("Jetty is not installed. Run setup from the downloaded AppImage first.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".jetty-update-", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    digest = hashlib.sha256()
    try:
        image_request = urllib.request.Request(assets[image_name], headers=headers)
        with urllib.request.urlopen(image_request, timeout=60) as response, temp.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        checksum_request = urllib.request.Request(assets[checksum_name], headers=headers)
        with urllib.request.urlopen(checksum_request, timeout=30) as response:
            expected = response.read().decode().split()[0]
        if digest.hexdigest().lower() != expected.lower():
            raise RuntimeError("Jetty AppImage checksum verification failed; installed files were left unchanged.")
        temp.chmod(0o755)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "restart", "jetty-daemon.service"], check=False)
    if _restart_tray(target):
        print(f"Updated Jetty to {tag} and restarted the tray.")
    else:
        print(f"Updated Jetty to {tag}.")
    return 0


def _tray_pids(proc: Path = Path("/proc")) -> list[int]:
    """Find this user's `jetty tray` processes, including the AppImage runtime wrapping each one."""
    pids = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 2 and argv[1] == b"tray" and Path(os.fsdecode(argv[0])).name == "jetty":
            pids.append(int(entry.name))
    return pids


def _restart_tray(target: Path) -> bool:
    """Replace a running tray with the updated binary; the old one keeps the previous UI loaded."""
    pids = _tray_pids()
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in pids):
        time.sleep(0.1)
    subprocess.Popen(
        [str(target), "tray"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "daemon":
        from .daemon import main as daemon_main

        daemon_main()
        return 0
    if args.mode == "tray":
        from .tray import main as tray_main

        return tray_main()
    if args.mode == "setup":
        from .setup import main as setup_main

        return setup_main()
    if args.mode == "update":
        try:
            return _update()
        except (OSError, RuntimeError) as exc:
            print(f"Jetty update failed: {exc}", file=sys.stderr)
            return 1
    try:
        if args.mode == "status":
            return _status()
        if args.mode == "gateway":
            body = {}
            key = _read_ssh_key(args.ssh_key)
            if key:
                body["ssh_public_key"] = key
            result = _wait_for_job(_api("POST", "/api/v1/jetty/setup", body))
            print(f"Gateway {result.get('gateway')} is {result.get('state')}.")
            return 0
        if args.mode == "vm":
            return _vm(args)
        if args.mode == "ssh-config":
            query = urllib.parse.urlencode([("name", name) for name in args.names])
            result = _api("GET", "/api/v1/jetty/ssh-config" + (f"?{query}" if query else ""))
            sys.stdout.write(result.get("config", ""))
            return 0
        if args.mode == "tunnel":
            from . import tunnel

            return tunnel.main([args.tunnel_mode])
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"jetty: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
