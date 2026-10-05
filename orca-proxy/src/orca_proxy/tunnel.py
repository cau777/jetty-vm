"""WireGuard tunnel keys for the gateway-VM transport.

Agent VMs have no route to the Internet except through the Jetty gateway VM,
which sends their TCP 80/443 into a WireGuard tunnel that ends in mitmdump's
userspace WireGuard mode (`--mode wireguard:<keys>@<host>:<port>`). The
gateway does no SNAT, so the addon still sees each VM's own source address.

mitmproxy would generate this key file itself, but world-readable (0664) and
it then logs the client config, private key included. Jetty creates the file
0600 before the listener starts and filters that log line out
(`install_log_filter`).
"""

import json
import logging
import os
import sys
from pathlib import Path

import mitmproxy_rs

from . import config


def ensure_keys(path: Path) -> dict:
    """Create the key file if missing; always (re)tighten it to 0600."""
    if not path.exists():
        keys = {"server_key": mitmproxy_rs.genkey(), "client_key": mitmproxy_rs.genkey()}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(keys, f, indent=4)
    os.chmod(path, 0o600)
    return load_keys(path)


def load_keys(path: Path) -> dict:
    keys = json.loads(path.read_text())
    # Raises on malformed keys, which /readyz reports as not ready.
    mitmproxy_rs.pubkey(keys["server_key"])
    mitmproxy_rs.pubkey(keys["client_key"])
    return keys


def keys_ok(path: Path) -> bool:
    try:
        load_keys(path)
    except Exception:
        return False
    return path.stat().st_mode & 0o077 == 0


class _DropPrivateKey(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "PrivateKey" not in record.getMessage()


def install_log_filter() -> None:
    logging.getLogger("mitmproxy.proxy.mode_servers").addFilter(_DropPrivateKey())


def main(argv: list[str]) -> int:
    """`ensure`: create/tighten the key file before the listener starts.
    `gateway`: print the gateway's material as JSON (client private key and
    server public key), read by deploy/jetty-lxd as the same host user. It is
    never served over the Management API.
    """
    path = config.tunnel_keys_path()
    if argv == ["ensure"]:
        ensure_keys(path)
        return 0
    if argv == ["gateway"]:
        keys = ensure_keys(path)
        json.dump(
            {"client_key": keys["client_key"], "server_public_key": mitmproxy_rs.pubkey(keys["server_key"])},
            sys.stdout,
        )
        return 0
    print("usage: python -m orca_proxy.tunnel ensure|gateway", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
