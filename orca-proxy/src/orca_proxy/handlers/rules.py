from aiohttp import web

from .. import validation
from ..errors import Conflict, NotFound, ValidationFailed
from ..repo import credentials as credentials_repo
from ..repo import rules as rules_repo
from ..repo import vms as vms_repo
from . import db_conn, read_json_body, reject_unknown_fields

_serialize = rules_repo.to_dict


def _validate_priority(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationFailed(
            "'priority' must be a non-negative integer", fields={"priority": "must be >= 0"}
        )
    return value


async def list_rules(request: web.Request) -> web.Response:
    conn = db_conn(request)
    rows = rules_repo.list_all(conn)
    return web.json_response({"rules": [_serialize(r) for r in rows]})


async def get_rule(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = rules_repo.get(conn, name)
    if row is None:
        raise NotFound(f"Rule '{name}' not found")
    return web.json_response(_serialize(row))


async def put_rule(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = validation.validate_name(request.match_info["name"])
    body = await read_json_body(request)
    reject_unknown_fields(body, {"priority", "vm_selector", "hostname", "action"})

    priority = _validate_priority(body.get("priority"))
    hostname = validation.validate_hostname(body.get("hostname"))
    vm_selector = validation.validate_vm_selector(body.get("vm_selector"), vms_repo.all_names(conn))
    action = validation.validate_rule_action(body.get("action"), credentials_repo.all_names(conn))
    if hostname == "@aws" and action["type"] == "allow_with_credential" and action["injection"]["type"] != "aws_sigv4":
        raise ValidationFailed("The AWS endpoint selector requires AWS SigV4 injection", fields={"action": "use AWS SigV4"})

    if rules_repo.priority_in_use_by_other(conn, priority, exclude_name=name):
        raise Conflict(
            f"priority {priority} is already in use by another Rule",
            fields={"priority": "already in use"},
        )

    row, created = rules_repo.put(conn, name, priority, vm_selector, hostname, action)
    status = 201 if created else 200
    return web.json_response(_serialize(row), status=status)


async def delete_rule(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = request.match_info["name"]
    row = rules_repo.get(conn, name)
    if row is None:
        raise NotFound(f"Rule '{name}' not found")
    rules_repo.delete(conn, name)
    return web.Response(status=204)
