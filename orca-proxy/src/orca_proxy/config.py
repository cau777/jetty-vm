import os
from pathlib import Path


def data_dir() -> Path:
    """Resolve the app's data directory.

    Defaults to ~/.orca-proxy (the runtime path fixed by the design spec's
    service-installation decision). Overridable via ORCA_PROXY_HOME so tests
    can isolate each run in a temp directory.
    """
    raw = os.environ.get("ORCA_PROXY_HOME")
    path = Path(raw) if raw else Path.home() / ".orca-proxy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "state.sqlite"


def requests_db_path() -> Path:
    return data_dir() / "requests.sqlite"


def management_api_port() -> int:
    return int(os.environ.get("ORCA_PROXY_MANAGEMENT_PORT", "8080"))


def tunnel_keys_path() -> Path:
    """WireGuard key file shared by mitmdump's wireguard mode (passed as the
    mode's data argument in deploy/orca-proxy.service) and the gateway VM
    setup (tunnel.py). Mode 0600; never under mitm-confdir, where mitmproxy
    would create its own world-readable one.
    """
    return data_dir() / "wireguard.json"


def mitm_confdir() -> Path:
    """mitmproxy's own --set confdir path (deploy/orca-proxy.service) — a
    subdirectory of data_dir(), not data_dir() itself, so ca_cert_path()
    can materialize into the exact spot mitmproxy's CertStore looks for its
    signing CA.
    """
    path = data_dir() / "mitm-confdir"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ca_cert_path() -> Path:
    """Where the combined CA cert+key PEM is materialized.

    Must be <confdir>/mitmproxy-ca.pem exactly — mitmproxy's own
    CertStore.from_store() (basename="mitmproxy", mitmproxy's
    CONF_BASENAME) looks for precisely that filename inside --set confdir.
    Get this wrong (e.g. a different filename or directory) and mitmdump
    silently auto-generates its own unrelated CA on first run instead of
    loading this one — every intercepted handshake then fails cert
    validation inside the VM, since the Provisioning Agent installs *this*
    CA into the VM's trust store, not mitmproxy's auto-generated one.
    """
    return mitm_confdir() / "mitmproxy-ca.pem"
