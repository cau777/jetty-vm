from aiohttp import web

from . import config
from .app import create_app

if __name__ == "__main__":
    # Standalone API entrypoint for local development. The desktop daemon
    # starts this app before supervising the proxy listener.
    #
    # Loopback-only — the Management API requires no authentication because
    # nothing beyond the local host can reach it (design ticket #6).
    web.run_app(create_app(), host="127.0.0.1", port=config.management_api_port())
