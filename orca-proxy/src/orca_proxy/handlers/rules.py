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


def _requested_scope(request: web.Request) -> str | None:
    vm = request.match_info.get("vm_name")
    if vm is not None:
        if vms_repo.get(db_conn(request), vm) is None:
            raise NotFound(f"VM '{vm}' not found")
        return vm
    if "scope" in request.query:
        scope = request.query["scope"]
        return "" if scope == "global" else scope
    return None


def _find_rule(request: web.Request):
    conn = db_conn(request)
    name = request.match_info["name"]
    scope = _requested_scope(request)
    if scope is not None:
        row = rules_repo.get(conn, name, scope)
    else:
        # Old provisioning clients may still address a unique rule by name.
        rows = rules_repo.list_named(conn, name)
        if len(rows) > 1:
            raise Conflict(f"Rule '{name}' exists in multiple scopes; specify its VM or global scope")
        row = rows[0] if rows else None
    if row is None:
        raise NotFound(f"Rule '{name}' not found")
    return row


async def list_rules(request: web.Request) -> web.Response:
    scope = _requested_scope(request)
    rows = rules_repo.list_all(db_conn(request), scope)
    return web.json_response({"rules": [_serialize(r) for r in rows]})


async def get_rule(request: web.Request) -> web.Response:
    return web.json_response(_serialize(_find_rule(request)))


async def put_rule(request: web.Request) -> web.Response:
    conn = db_conn(request)
    name = validation.validate_name(request.match_info["name"])
    body = await read_json_body(request)
    reject_unknown_fields(body, {"priority", "vm_selector", "hostname", "action"})
    requested_scope = _requested_scope(request)
    selector = body.get("vm_selector")
    if requested_scope is not None:
        expected = {"type": "only", "vms": [requested_scope]} if requested_scope else {"type": "all"}
        if selector is not None and selector != expected:
            raise ValidationFailed("Rule scope cannot differ from its URL", fields={"vm_selector": "scope mismatch"})
        selector = expected
    priority = _validate_priority(body.get("priority"))
    hostname = validation.validate_hostname(body.get("hostname"))
    vm_selector = validation.validate_vm_selector(selector, vms_repo.all_names(conn))
    scope = "" if vm_selector["type"] == "all" else vm_selector["vms"][0]
    action = validation.validate_rule_action(body.get("action"), credentials_repo.all_names(conn))
    if hostname == "@aws" and action["type"] == "allow_with_credential" and action["injection"]["type"] != "aws_sigv4":
        raise ValidationFailed("The AWS endpoint selector requires AWS SigV4 injection", fields={"action": "use AWS SigV4"})
    if rules_repo.priority_in_use_by_other(conn, priority, exclude_name=name, scope=scope):
        raise Conflict(f"priority {priority} is already in use in this rule scope", fields={"priority": "already in use in this scope"})
    row, created = rules_repo.put(conn, name, priority, vm_selector, hostname, action)
    return web.json_response(_serialize(row), status=201 if created else 200)


async def delete_rule(request: web.Request) -> web.Response:
    row = _find_rule(request)
    rules_repo.delete(db_conn(request), row["name"], row["scope"])
    return web.Response(status=204)
