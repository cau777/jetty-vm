import asyncio
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from . import ca, config, db, request_log
from .credential_exec import CredentialCache
from .errors import error_middleware
from .firewall import FirewallSync
from .handlers import ca as ca_handlers
from .handlers import credentials as credential_handlers
from .handlers import health as health_handlers
from .handlers import requests_api
from .handlers import rules as rule_handlers
from .handlers import vms as vm_handlers
from .repo import vms as vms_repo


def create_app(credential_cache: CredentialCache | None = None) -> web.Application:
    """Build the aiohttp application.

    Decoupled from any specific entrypoint — a later slice starts this from
    within the mitmproxy addon's own asyncio loop instead of a standalone
    web.run_app(), per the design spec's language/stack decision (#4).

    `credential_cache` lets the caller share one `CredentialCache` instance
    with another component in the same process (the mitmdump addon, per
    proxy_addon.py's `running()`) — the Management API's credential status
    endpoint and PUT-triggered cache invalidation are only meaningful if
    they observe/act on the same in-memory state the interception path
    actually executes commands against. Defaults to a fresh instance for
    standalone use (`__main__.py`, tests).
    """
    app = web.Application(middlewares=[error_middleware])

    conn = db.connect(config.db_path())
    db.migrate(conn)
    app["db"] = conn
    app["migrations_applied"] = True

    ca_row = ca.ensure_generated(conn)
    ca.materialize(ca_row, config.ca_cert_path())
    app["ca_materialized"] = True

    app["credential_cache"] = credential_cache if credential_cache is not None else CredentialCache()

    requests_conn = request_log.connect(config.requests_db_path())
    app["request_log"] = request_log.RequestLog(requests_conn)

    firewall_sync = FirewallSync(config.firewall_sync_script_path(), config.bridge_interface(), config.proxy_port())
    app["firewall_sync"] = firewall_sync
    # Reconcile once at startup against whatever VMs are already registered
    # (e.g. after a restart) — not just future create/delete events — so
    # /readyz reflects reality immediately rather than reporting a stale
    # "nothing to sync" true from an empty in-memory status.
    firewall_sync.reconcile(vms=[(row["name"], row["ip_address"]) for row in vms_repo.list_all(conn)])

    async def maintain_firewall(_app: web.Application):
        # Multipass's system service reports started before its asynchronous
        # bridge/firewall initialization finishes. It can therefore delete
        # our parent-chain hooks just after the one-shot sync above. Retry
        # rapidly through that boot window, then keep checking for later
        # Multipass/firewall reloads. The helper only mutates when its
        # integrity check finds drift.
        task = asyncio.create_task(
            firewall_sync.maintain(lambda: [(row["name"], row["ip_address"]) for row in vms_repo.list_all(conn)]),
            name="orca-proxy-firewall-maintenance",
        )
        _app["firewall_maintenance_task"] = task
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(maintain_firewall)

    app.add_routes(
        [
            web.get("/readyz", health_handlers.readyz),
            web.get("/api/v1/ca", ca_handlers.get_ca),
            web.get("/api/v1/vms", vm_handlers.list_vms),
            web.get("/api/v1/vms/{name}", vm_handlers.get_vm),
            web.put("/api/v1/vms/{name}", vm_handlers.put_vm),
            web.delete("/api/v1/vms/{name}", vm_handlers.delete_vm),
            web.get("/api/v1/credentials", credential_handlers.list_credentials),
            web.get("/api/v1/credentials/{name}", credential_handlers.get_credential),
            web.put("/api/v1/credentials/{name}", credential_handlers.put_credential),
            web.delete("/api/v1/credentials/{name}", credential_handlers.delete_credential),
            web.get("/api/v1/rules", rule_handlers.list_rules),
            web.get("/api/v1/rules/{name}", rule_handlers.get_rule),
            web.put("/api/v1/rules/{name}", rule_handlers.put_rule),
            web.delete("/api/v1/rules/{name}", rule_handlers.delete_rule),
            web.get("/api/v1/requests", requests_api.list_requests),
            web.get("/api/v1/requests/{id}", requests_api.get_request),
        ]
    )

    # Web UI (#15): dependency-free static assets, served directly (no
    # build step, no separate frontend server) — the same aiohttp process
    # already serving the Management API. index.html's relative
    # <link>/<script> paths and app.js's relative fetch() calls all resolve
    # against "/", so the whole directory is mounted there.
    static_dir = Path(__file__).parent / "static"

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(static_dir / "index.html")

    app.router.add_get("/", index)
    app.router.add_static("/", static_dir, name="static")

    async def close_db(_app: web.Application) -> None:
        conn.close()
        requests_conn.close()

    app.on_cleanup.append(close_db)
    return app
