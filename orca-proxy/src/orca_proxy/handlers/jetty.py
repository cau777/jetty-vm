import asyncio
import base64
import logging

from aiohttp import web

from .. import handoff
from ..errors import NotFound, ServiceUnavailable
from ..jetty import JettyLxd
from ..lxd import LxdError
from ..repo import vms as vms_repo
from . import read_json_body, reject_unknown_fields

log = logging.getLogger("jetty.api")

VM_CREATE_STEPS = [
    ("validate", "Validating VM settings"),
    ("instance", "Pulling the Ubuntu image and creating the instance"),
    ("register", "Registering the VM and SSH access"),
    ("start", "Starting the VM"),
    ("guest_agent", "Waiting for the LXD guest agent"),
    ("cloud_init", "Running cloud-init"),
    ("ready", "Checking the VM is ready"),
]
GATEWAY_SETUP_STEPS = [
    ("validate", "Checking the SSH key"),
    ("project", "Preparing the LXD project"),
    ("profile", "Preparing the VM profile"),
    ("networks", "Preparing the Jetty networks"),
    ("instance", "Creating the gateway if needed"),
    ("start", "Starting the gateway"),
    ("guest_agent", "Waiting for the LXD guest agent"),
    ("cloud_init", "Running cloud-init"),
    ("configure", "Installing gateway networking and firewall rules"),
    ("ssh_config", "Updating host SSH configuration"),
]


def _service(request: web.Request) -> JettyLxd:
    return JettyLxd(request.app["db"])


async def _call(operation):
    try:
        return await operation
    except LxdError as exc:
        log.warning("LXD operation failed: %s", exc)
        raise ServiceUnavailable(str(exc)) from exc


async def status(request: web.Request) -> web.Response:
    return web.json_response(await _call(_service(request).status()))


async def setup(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    reject_unknown_fields(body, {"ssh_public_key"})
    ssh_key = body.get("ssh_public_key")
    if ssh_key is not None and not isinstance(ssh_key, str):
        from ..errors import ValidationFailed

        raise ValidationFailed("ssh_public_key must be a string", fields={"ssh_public_key": "invalid value"})
    if request.app["jobs"].has_active("gateway_setup", "vm_create"):
        from ..errors import Conflict

        raise Conflict("Another Jetty provisioning job is already in progress")
    service = _service(request)
    job = request.app["jobs"].start(
        "gateway_setup",
        "jetty-gw",
        GATEWAY_SETUP_STEPS,
        lambda progress: _call(service.setup(ssh_key, progress=progress)),
    )
    return web.json_response(job, status=202)


async def get_job(request: web.Request) -> web.Response:
    job = request.app["jobs"].get(request.match_info["job_id"])
    if job is None:
        raise NotFound(f"Job '{request.match_info['job_id']}' not found")
    return web.json_response(job)


async def list_vms(request: web.Request) -> web.Response:
    return web.json_response({"vms": await _call(_service(request).list_vms())})


async def create_vm(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    reject_unknown_fields(body, {"name", "cpus", "memory", "disk", "image", "ssh_public_key"})
    from ..validation import validate_name

    name = validate_name(body.get("name"))
    if request.app["jobs"].has_active("gateway_setup", "vm_create"):
        from ..errors import Conflict

        raise Conflict("Another Jetty provisioning job is already in progress")
    kwargs = {key: body[key] for key in ("cpus", "memory", "disk", "image") if key in body}
    if "ssh_public_key" in body:
        if not isinstance(body["ssh_public_key"], str):
            from ..errors import ValidationFailed

            raise ValidationFailed("ssh_public_key must be a string", fields={"ssh_public_key": "invalid value"})
        kwargs["ssh_key"] = body["ssh_public_key"]
    service = _service(request)
    job = request.app["jobs"].start(
        "vm_create",
        name,
        VM_CREATE_STEPS,
        lambda progress: _call(service.create_vm(name, **kwargs, progress=progress)),
    )
    return web.json_response(job, status=202)


async def delete_vm(request: web.Request) -> web.Response:
    await _call(_service(request).delete_vm(request.match_info["name"]))
    return web.Response(status=204)


async def vm_action(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    reject_unknown_fields(body, {"action"})
    if not isinstance(body.get("action"), str):
        from ..errors import ValidationFailed

        raise ValidationFailed("action must be a string", fields={"action": "invalid value"})
    result = await _call(_service(request).vm_action(request.match_info["name"], body["action"]))
    return web.json_response(result)


async def vm_exec(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    reject_unknown_fields(body, {"command"})
    command = body.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        from ..errors import ValidationFailed

        raise ValidationFailed("command must be a list of strings", fields={"command": "invalid value"})
    result = await _call(_service(request).execute_vm(request.match_info["name"], command))
    return web.json_response(
        {
            "exit_code": result.exit_code,
            "stdout_b64": base64.b64encode(result.stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(result.stderr).decode("ascii"),
        }
    )


async def vm_upload(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    reject_unknown_fields(body, {"path", "content_b64", "mode", "owner"})
    path = body.get("path")
    content_b64 = body.get("content_b64")
    mode = body.get("mode", 0o644)
    owner = body.get("owner", "ubuntu")
    if not isinstance(path, str) or not isinstance(content_b64, str):
        from ..errors import ValidationFailed

        raise ValidationFailed("path and content_b64 must be strings", fields={"path": "required"})
    if not isinstance(mode, int) or isinstance(mode, bool):
        from ..errors import ValidationFailed

        raise ValidationFailed("mode must be an integer", fields={"mode": "invalid value"})
    if not isinstance(owner, str):
        from ..errors import ValidationFailed

        raise ValidationFailed("owner must be a string", fields={"owner": "invalid value"})
    try:
        content = base64.b64decode(content_b64, validate=True)
    except ValueError as exc:
        from ..errors import ValidationFailed

        raise ValidationFailed("content_b64 is invalid", fields={"content_b64": "invalid base64"}) from exc
    await _call(_service(request).upload_vm_file(request.match_info["name"], path, content, mode=mode, owner=owner))
    return web.json_response({"path": path, "bytes_written": len(content)})


async def vm_handoff(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    reject_unknown_fields(body, {"agent", "project_dir"})
    name = request.match_info["name"]
    vm = vms_repo.get(request.app["db"], name)
    if vm is None:
        raise NotFound(f"VM '{name}' is not registered")
    project_dir = handoff.project_directory(body.get("project_dir"))
    agent = body.get("agent")
    result = await asyncio.to_thread(handoff.launch, agent if isinstance(agent, str) else "", name, vm["ip_address"], project_dir)
    return web.json_response(result, status=202)


async def ssh_config(request: web.Request) -> web.Response:
    names = request.query.getall("name", [])
    return web.json_response({"config": await _call(_service(request).ssh_config(names or None))})
