from aiohttp import web

from .. import validation
from ..errors import Conflict, NotFound
from ..repo import vms as vms_repo
from . import db_conn, read_json_body, reject_unknown_fields


def _serialize(row) -> dict:
    return {
        "name": row["name"],
        "ip_address": row["ip_address"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def list_vms(request: web.Request) -> web.Response:
    conn = db_conn(request)
    rows = vms_repo.list_all(conn)
    return web.json_response({"vms": [_serialize(r) for r in rows]})


async def get_vm(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = vms_repo.get(conn, name)
    if row is None:
        raise NotFound(f"VM '{name}' not found")
    return web.json_response(_serialize(row))


async def put_vm(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = validation.validate_name(request.match_info["name"])
    body = await read_json_body(request)
    reject_unknown_fields(body, {"ip_address"})
    ip_address = validation.validate_ip_address(body.get("ip_address"))

    if vms_repo.ip_in_use_by_other(conn, ip_address, exclude_name=name):
        raise Conflict(
            f"ip_address '{ip_address}' is already in use by another VM",
            fields={"ip_address": "already in use"},
        )

    row, created = vms_repo.put(conn, name, ip_address)
    status = 201 if created else 200
    return web.json_response(_serialize(row), status=status)


async def delete_vm(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = vms_repo.get(conn, name)
    if row is None:
        raise NotFound(f"VM '{name}' not found")
    if vms_repo.referenced_by_rule(conn, name):
        raise Conflict(f"VM '{name}' is referenced by a Rule; update or delete it first")
    vms_repo.delete(conn, name)
    return web.Response(status=204)
