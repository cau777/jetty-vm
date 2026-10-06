from aiohttp import web

from .. import validation
from ..credential_exec import CredentialCache
from ..errors import Conflict, NotFound, ValidationFailed
from ..repo import credentials as credentials_repo
from . import db_conn, read_json_body, reject_unknown_fields


def _cache(request: web.Request) -> CredentialCache:
    return request.app["credential_cache"]


def _serialize(row, cache: CredentialCache) -> dict:
    status = cache.get_status(row["name"])
    return {
        "name": row["name"],
        "command": row["command"],
        "ttl_seconds": row["ttl_seconds"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # Safe ephemeral status only (#10) — never the Credential Value,
        # stdout, stderr, or exit text.
        **status,
    }


def _validate_command(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed("'command' must be a non-empty string", fields={"command": "required"})
    return value


def _validate_ttl_seconds(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationFailed(
            "'ttl_seconds' must be a non-negative integer", fields={"ttl_seconds": "must be >= 0"}
        )
    return value


async def list_credentials(request: web.Request) -> web.Response:
    conn = db_conn(request)
    cache = _cache(request)
    rows = credentials_repo.list_all(conn)
    return web.json_response({"credentials": [_serialize(r, cache) for r in rows]})


async def get_credential(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = credentials_repo.get(conn, name)
    if row is None:
        raise NotFound(f"Credential '{name}' not found")
    return web.json_response(_serialize(row, _cache(request)))


async def put_credential(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = validation.validate_name(request.match_info["name"])
    body = await read_json_body(request)
    reject_unknown_fields(body, {"command", "ttl_seconds"})
    command = _validate_command(body.get("command"))
    ttl_seconds = _validate_ttl_seconds(body.get("ttl_seconds"))

    row, created = credentials_repo.put(conn, name, command, ttl_seconds)
    # A successful PUT clears cached value/status and kills any in-flight
    # execution rather than letting a stale command's result linger (#10).
    _cache(request).invalidate(name)
    status = 201 if created else 200
    return web.json_response(_serialize(row, _cache(request)), status=status)


async def refresh_credential(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = credentials_repo.get(conn, name)
    if row is None:
        raise NotFound(f"Credential '{name}' not found")
    # Forget the cached value without executing the command; the next
    # intercepted request that needs it regenerates it.
    _cache(request).invalidate(name)
    return web.json_response(_serialize(row, _cache(request)))


async def delete_credential(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = credentials_repo.get(conn, name)
    if row is None:
        raise NotFound(f"Credential '{name}' not found")
    if credentials_repo.referenced_by_rule(conn, name):
        raise Conflict(f"Credential '{name}' is referenced by a Rule; update or delete it first")
    credentials_repo.delete(conn, name)
    _cache(request).drop(name)
    return web.Response(status=204)
