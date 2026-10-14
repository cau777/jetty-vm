import re
from urllib.parse import unquote

from .errors import ValidationFailed

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def validate_name(value: object, field: str = "name") -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ValidationFailed(
            f"'{field}' must be a lowercase slug (letters, digits, hyphens; "
            "starting with a letter or digit; max 63 characters)",
            fields={field: "invalid slug"},
        )
    return value


def validate_ip_address(value: object, field: str = "ip_address") -> str:
    if not isinstance(value, str) or not _IPV4_RE.match(value):
        raise ValidationFailed(f"'{field}' must be a valid IPv4 address", fields={field: "invalid IPv4 address"})
    parts = value.split(".")
    if not all(0 <= int(p) <= 255 for p in parts):
        raise ValidationFailed(f"'{field}' must be a valid IPv4 address", fields={field: "invalid IPv4 address"})
    return value


def validate_hostname(value: object, field: str = "hostname") -> str:
    if value == "@aws":
        return value
    if not isinstance(value, str) or not value:
        raise ValidationFailed(f"'{field}' is required", fields={field: "required"})
    if "://" in value or "/" in value:
        raise ValidationFailed(
            f"'{field}' must be a hostname (no scheme or path)",
            fields={field: "must be a bare hostname"},
        )
    normalized = value.lower()
    if normalized.endswith("."):
        normalized = normalized[:-1]
    host, _, port = normalized.partition(":")
    if port:
        raise ValidationFailed(f"'{field}' must not include a port", fields={field: "must not include a port"})
    bare_host = host[2:] if host.startswith("*.") else host
    if not _HOSTNAME_RE.fullmatch(bare_host):
        raise ValidationFailed(f"'{field}' is not a valid hostname", fields={field: "not a valid hostname"})
    return host


def is_safe_absolute_path(value: str) -> bool:
    """Non-raising check shared by write-time path_prefix validation (below)
    and live request-path eligibility (rule_engine.py) — both need the exact
    same "is this path form unambiguous enough to trust" rule from #5 step 7:
    reject dot-segments, percent-encoding, and empty segments.
    """
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    if "%" in value or unquote(value) != value:
        return False
    segments = value.split("/")
    if any(seg in (".", "..") for seg in segments):
        return False
    # collapse duplicate slashes' resulting empty segments, except the
    # leading one that "/".split("/") always produces
    if any(seg == "" for seg in segments[1:-1]):
        return False
    return True


def normalize_path(value: str) -> str:
    if value != "/" and value.endswith("/"):
        return value[:-1]
    return value


def validate_path_prefix(value: object, field: str = "path_prefix") -> str:
    if not isinstance(value, str) or not is_safe_absolute_path(value):
        raise ValidationFailed(
            f"'{field}' must be a safe absolute path (no '.'/'..' segments, "
            "no percent-encoding, no empty segments)",
            fields={field: "must be a safe absolute path"},
        )
    return normalize_path(value)


def validate_vm_selector(value: object, existing_vm_names: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValidationFailed("'vm_selector' must be an object", fields={"vm_selector": "must be an object"})
    selector_type = value.get("type")
    if selector_type == "all":
        if set(value.keys()) != {"type"}:
            raise ValidationFailed(
                "'vm_selector' of type 'all' must not include other fields",
                fields={"vm_selector": "unexpected fields for type 'all'"},
            )
        return {"type": "all"}
    if selector_type == "only":
        if set(value.keys()) != {"type", "vms"}:
            raise ValidationFailed(
                "'vm_selector' of type 'only' must include exactly 'type' and 'vms'",
                fields={"vm_selector": "unexpected fields for type 'only'"},
            )
        vms = value.get("vms")
        if not isinstance(vms, list) or len(vms) != 1 or not isinstance(vms[0], str):
            raise ValidationFailed(
                "'vm_selector.vms' must contain exactly one VM",
                fields={"vm_selector": "select exactly one VM"},
            )
        if len(set(vms)) != len(vms):
            raise ValidationFailed(
                "'vm_selector.vms' must not contain duplicates",
                fields={"vm_selector": "duplicate VM names"},
            )
        missing = [v for v in vms if v not in existing_vm_names]
        if missing:
            raise ValidationFailed(
                f"'vm_selector.vms' references unknown VM(s): {', '.join(missing)}",
                fields={"vm_selector": f"unknown VM(s): {', '.join(missing)}"},
            )
        return {"type": "only", "vms": vms}
    raise ValidationFailed(
        "'vm_selector.type' must be 'all' or 'only'",
        fields={"vm_selector": "type must be 'all' or 'only'"},
    )


def validate_rule_action(value: object, existing_credential_names: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValidationFailed("'action' must be an object", fields={"action": "must be an object"})
    action_type = value.get("type")

    if action_type == "allow":
        if set(value.keys()) != {"type"}:
            raise ValidationFailed(
                "'action' of type 'allow' must not include other fields",
                fields={"action": "unexpected fields for type 'allow'"},
            )
        return {"type": "allow"}

    if action_type == "block":
        if set(value.keys()) != {"type"}:
            raise ValidationFailed(
                "'action' of type 'block' must not include other fields",
                fields={"action": "unexpected fields for type 'block'"},
            )
        return {"type": "block"}

    if action_type == "allow_with_credential":
        allowed_keys = {"type", "credential", "path_prefix", "injection"}
        if set(value.keys()) != allowed_keys:
            raise ValidationFailed(
                "'action' of type 'allow_with_credential' must include exactly "
                "'credential', 'path_prefix', and 'injection'",
                fields={"action": "unexpected/missing fields for type 'allow_with_credential'"},
            )
        credential = value.get("credential")
        validate_name(credential, field="action.credential")
        if credential not in existing_credential_names:
            raise ValidationFailed(
                f"'action.credential' references unknown Credential '{credential}'",
                fields={"action": f"unknown Credential '{credential}'"},
            )
        path_prefix = validate_path_prefix(value.get("path_prefix"), field="action.path_prefix")
        injection = _validate_injection(value.get("injection"))
        return {
            "type": "allow_with_credential",
            "credential": credential,
            "path_prefix": path_prefix,
            "injection": injection,
        }

    raise ValidationFailed(
        "'action.type' must be 'allow', 'block', or 'allow_with_credential'",
        fields={"action": "invalid type"},
    )


def _validate_injection(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValidationFailed(
            "'action.injection' must be an object",
            fields={"action": "injection must be an object"},
        )
    injection_type = value.get("type")
    if injection_type == "aws_sigv4":
        if set(value) != {"type", "service", "region"}:
            raise ValidationFailed(
                "AWS SigV4 injection requires exactly 'type', 'service', and 'region'",
                fields={"action": "unexpected/missing fields for AWS SigV4"},
            )
        for key in ("service", "region"):
            item = value[key]
            if not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", item):
                raise ValidationFailed(
                    f"'action.injection.{key}' must be a lowercase AWS signing identifier",
                    fields={"action": f"invalid AWS signing {key}"},
                )
        return {"type": "aws_sigv4", "service": value["service"], "region": value["region"]}
    if injection_type == "bearer":
        if set(value.keys()) != {"type"}:
            raise ValidationFailed(
                "'action.injection' of type 'bearer' must not include other fields",
                fields={"action": "unexpected fields for injection type 'bearer'"},
            )
        return {"type": "bearer"}
    if injection_type == "basic":
        if set(value.keys()) != {"type", "username"}:
            raise ValidationFailed(
                "'action.injection' of type 'basic' must include exactly 'type' and 'username'",
                fields={"action": "unexpected/missing fields for injection type 'basic'"},
            )
        username = value.get("username")
        if not isinstance(username, str) or not username:
            raise ValidationFailed(
                "'action.injection.username' must be a non-empty string",
                fields={"action": "injection.username must be non-empty"},
            )
        return {"type": "basic", "username": username}
    raise ValidationFailed(
        "'action.injection.type' must be 'bearer', 'basic', or 'aws_sigv4'",
        fields={"action": "invalid injection type"},
    )
