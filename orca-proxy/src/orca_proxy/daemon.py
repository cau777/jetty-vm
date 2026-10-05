"""Always-on management service with a separately supervised proxy listener."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from . import config, tunnel
from .app import create_app
from .credential_exec import CredentialCache
from .lxd import socket_accessible
from .network import TUNNEL_HOST, TUNNEL_PORT, host_has_ipv4
from .proxy_addon import OrcaProxyAddon

log = logging.getLogger("jetty.daemon")


def _proxy_state(status: dict, value: str) -> None:
    status["proxy"] = value
    if value != "failed":
        status["proxy_error"] = None


async def _serve_proxy(stop: asyncio.Event, status: dict, credentials: CredentialCache) -> None:
    """Construct DumpMaster inside the running loop and supervise its shutdown."""
    tunnel.install_log_filter()
    tunnel.ensure_keys(config.tunnel_keys_path())
    mode = f"wireguard:{config.tunnel_keys_path()}@{TUNNEL_HOST}:{TUNNEL_PORT}"
    options = Options(mode=[mode], confdir=str(config.mitm_confdir()))
    master = DumpMaster(options, with_termlog=True, with_dumper=False)
    master.addons.add(
        OrcaProxyAddon(
            start_management_api=False,
            credential_cache=credentials,
            state_callback=lambda value: _proxy_state(status, value),
        )
    )
    proxy_task = asyncio.create_task(master.run(), name="jetty-mitmproxy")
    stop_task = asyncio.create_task(stop.wait(), name="jetty-stop-wait")
    try:
        done, _pending = await asyncio.wait({proxy_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            master.shutdown()
            await proxy_task
        else:
            await proxy_task
    finally:
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        if not proxy_task.done():
            master.shutdown()
            await asyncio.gather(proxy_task, return_exceptions=True)


async def _proxy_supervisor(stop: asyncio.Event, status: dict, credentials: CredentialCache) -> None:
    while not stop.is_set():
        if not host_has_ipv4(TUNNEL_HOST):
            status["proxy"] = "waiting_for_network" if socket_accessible() else "not_configured"
            status["proxy_error"] = None
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
            continue

        _proxy_state(status, "starting")
        try:
            await _serve_proxy(stop, status, credentials)
            if stop.is_set():
                break
            status["proxy"] = "stopped"
            status["proxy_error"] = "The proxy listener exited unexpectedly."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("mitmproxy listener failed; retrying")
            status["proxy"] = "failed"
            status["proxy_error"] = str(exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


async def serve() -> None:
    status = {
        "daemon": "starting",
        "proxy": "not_configured",
        "proxy_error": None,
        "tunnel_host": TUNNEL_HOST,
        "tunnel_port": TUNNEL_PORT,
    }
    credentials = CredentialCache()
    app = create_app(credential_cache=credentials, daemon_status=status)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.management_api_port())
    try:
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    status["daemon"] = "running"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    proxy_task = asyncio.create_task(_proxy_supervisor(stop, status, credentials), name="jetty-proxy-supervisor")
    log.info("Jetty management API listening on 127.0.0.1:%s", config.management_api_port())
    try:
        await stop.wait()
    finally:
        status["daemon"] = "stopping"
        stop.set()
        await proxy_task
        await runner.cleanup()
        status["daemon"] = "stopped"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())
