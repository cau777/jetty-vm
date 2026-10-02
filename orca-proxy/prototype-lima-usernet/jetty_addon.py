"""PROTOTYPE — throwaway. Stand-in for orca-proxy's rule engine in explicit mode.

The patched user-mode network sends every guest 80/443 connection here as
`CONNECT <original-dst>` with `Proxy-Authorization: Basic <vm>:x`. This addon
shows the three behaviours orca-proxy needs on that path: per-VM identity,
SNI passthrough for unmatched hosts, and MITM + credential injection for a
matched host/path.
"""

import base64

from mitmproxy import ctx, http, tls

# One Allow-with-credential rule: host + path prefix -> injected bearer token.
INJECT_HOST = "postman-echo.com"
INJECT_PATH = "/headers"
INJECT_TOKEN = "PROTOTYPE-HOST-HELD-SECRET"

_vm_by_client: dict[str, str | None] = {}


def _log(msg: str) -> None:
    ctx.log.warn(f"[jetty] {msg}")


def http_connect(flow: http.HTTPFlow) -> None:
    auth = flow.request.headers.get("Proxy-Authorization", "")
    vm = None
    if auth.startswith("Basic "):
        vm = base64.b64decode(auth[6:]).decode().split(":", 1)[0]
    _vm_by_client[flow.client_conn.id] = vm
    _log(f"CONNECT vm={vm} dst={flow.request.host}:{flow.request.port} peer={flow.client_conn.peername}")


def tls_clienthello(data: tls.ClientHelloData) -> None:
    vm = _vm_by_client.get(data.context.client.id)
    sni = data.client_hello.sni
    if sni != INJECT_HOST:
        data.ignore_connection = True
        _log(f"PASSTHROUGH vm={vm} sni={sni}")
    else:
        _log(f"INTERCEPT vm={vm} sni={sni}")


def request(flow: http.HTTPFlow) -> None:
    vm = _vm_by_client.get(flow.client_conn.id)
    injected = flow.request.pretty_host == INJECT_HOST and flow.request.path.startswith(INJECT_PATH)
    if injected:
        flow.request.headers["Authorization"] = f"Bearer {INJECT_TOKEN}"
    _log(f"REQUEST vm={vm} {flow.request.method} {flow.request.pretty_url} injected={injected}")
