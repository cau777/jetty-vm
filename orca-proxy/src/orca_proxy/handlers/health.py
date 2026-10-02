from aiohttp import web

from .. import config, tunnel


async def readyz(request: web.Request) -> web.Response:
    app = request.app
    checks = {
        "migrations": app.get("migrations_applied", False),
        "ca_materialized": app.get("ca_materialized", False),
        # Valid, owner-only WireGuard keys for the gateway tunnel. Whether the
        # gateway VM itself is up is the lifecycle tool's concern
        # (deploy/jetty-lxd status); the proxy never talks to LXD.
        "tunnel_keys": tunnel.keys_ok(config.tunnel_keys_path()),
    }
    ready = all(checks.values())
    return web.json_response({"ready": ready, "checks": checks}, status=200 if ready else 503)
