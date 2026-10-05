from aiohttp import web

from ..lxd import socket_accessible, socket_path
from ..network import TUNNEL_HOST, host_has_ipv4


async def status(request: web.Request) -> web.Response:
    daemon = request.app.get("daemon_status")
    proxy = daemon.get("proxy", "stopped") if daemon else "stopped"
    lxd_available = socket_accessible()
    network_ready = host_has_ipv4(TUNNEL_HOST)
    if not lxd_available:
        state = "not_configured"
    elif not network_ready:
        state = "waiting_for_network"
    elif proxy == "running":
        state = "running"
    else:
        state = proxy
    return web.json_response(
        {
            "state": state,
            "daemon": daemon.get("daemon", "running") if daemon else "standalone",
            "proxy": proxy,
            "proxy_error": daemon.get("proxy_error") if daemon else None,
            "lxd_available": lxd_available,
            "lxd_socket": str(socket_path()),
            "network_ready": network_ready,
        }
    )
