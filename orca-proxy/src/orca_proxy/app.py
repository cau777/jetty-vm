from aiohttp import web

from . import ca, config, db, request_log, tunnel
from .credential_exec import CredentialCache
from .errors import error_middleware
from .handlers import ca as ca_handlers
from .handlers import credentials as credential_handlers
from .handlers import health as health_handlers
from .handlers import jetty as jetty_handlers
from .handlers import requests_api
from .handlers import rules as rule_handlers
from .handlers import system as system_handlers
from .handlers import vms as vm_handlers
from .jobs import JobManager


def create_app(
    credential_cache: CredentialCache | None = None,
    daemon_status: dict | None = None,
) -> web.Application:
    """Build the aiohttp application.

    The desktop daemon starts this before the WireGuard listener so setup is
    available while the Jetty network is being created. The legacy addon mode
    can still host it from mitmproxy's event loop; `__main__` remains useful
    for local API development.

    `credential_cache` lets the caller share cache state with the interception
    path when both run in one process. It defaults to a fresh instance for
    standalone development.
    """
    app = web.Application(client_max_size=128 * 1024 * 1024, middlewares=[error_middleware])

    conn = db.connect(config.db_path())
    db.migrate(conn)
    app["db"] = conn
    app["migrations_applied"] = True

    ca_row = ca.ensure_generated(conn)
    ca.materialize(ca_row, config.ca_cert_path())
    app["ca_materialized"] = True

    app["credential_cache"] = credential_cache if credential_cache is not None else CredentialCache()
    if daemon_status is not None:
        app["daemon_status"] = daemon_status

    requests_conn = request_log.connect(config.requests_db_path())
    app["request_log"] = request_log.RequestLog(requests_conn)
    app["jobs"] = JobManager()

    # The WireGuard keys the tunnel listener uses (tunnel.py). Create them in
    # standalone development as well as in the desktop daemon.
    try:
        tunnel.ensure_keys(config.tunnel_keys_path())
    except Exception:
        pass

    app.add_routes(
        [
            web.get("/readyz", health_handlers.readyz),
            web.get("/api/v1/status", system_handlers.status),
            web.get("/api/v1/jetty/status", jetty_handlers.status),
            web.post("/api/v1/jetty/setup", jetty_handlers.setup),
            web.get("/api/v1/jetty/jobs/{job_id}", jetty_handlers.get_job),
            web.get("/api/v1/jetty/vms", jetty_handlers.list_vms),
            web.post("/api/v1/jetty/vms", jetty_handlers.create_vm),
            web.delete("/api/v1/jetty/vms/{name}", jetty_handlers.delete_vm),
            web.post("/api/v1/jetty/vms/{name}/action", jetty_handlers.vm_action),
            web.post("/api/v1/jetty/vms/{name}/exec", jetty_handlers.vm_exec),
            web.post("/api/v1/jetty/vms/{name}/files", jetty_handlers.vm_upload),
            web.post("/api/v1/jetty/vms/{name}/handoff", jetty_handlers.vm_handoff),
            web.get("/api/v1/jetty/ssh-config", jetty_handlers.ssh_config),
            web.get("/api/v1/ca", ca_handlers.get_ca),
            web.get("/api/v1/vms", vm_handlers.list_vms),
            web.get("/api/v1/vms/{name}", vm_handlers.get_vm),
            web.put("/api/v1/vms/{name}", vm_handlers.put_vm),
            web.delete("/api/v1/vms/{name}", vm_handlers.delete_vm),
            web.get("/api/v1/credentials", credential_handlers.list_credentials),
            web.get("/api/v1/credentials/{name}", credential_handlers.get_credential),
            web.put("/api/v1/credentials/{name}", credential_handlers.put_credential),
            web.delete("/api/v1/credentials/{name}", credential_handlers.delete_credential),
            web.post("/api/v1/credentials/{name}/refresh", credential_handlers.refresh_credential),
            web.get("/api/v1/rules", rule_handlers.list_rules),
            web.get("/api/v1/rules/{name}", rule_handlers.get_rule),
            web.put("/api/v1/rules/{name}", rule_handlers.put_rule),
            web.delete("/api/v1/rules/{name}", rule_handlers.delete_rule),
            web.get("/api/v1/requests", requests_api.list_requests),
            web.get("/api/v1/requests/{id}", requests_api.get_request),
        ]
    )

    async def close_db(_app: web.Application) -> None:
        await app["jobs"].close()
        conn.close()
        requests_conn.close()

    app.on_cleanup.append(close_db)
    return app
