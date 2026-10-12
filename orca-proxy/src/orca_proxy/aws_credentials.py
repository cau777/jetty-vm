"""Validate the JSON emitted by an AWS Credential bash command."""

import json
import time
from datetime import datetime


def parse_credentials(value: str) -> tuple[dict, float | None]:
    """Return process-format credentials and their refresh deadline.

    Refresh 30 seconds before expiration, allowing for network transit and
    clock differences. Never include command output in validation errors.
    """
    try:
        data = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("AWS credentials must be a JSON object") from exc
    if not isinstance(data, dict) or type(data.get("Version")) is not int or data["Version"] != 1:
        raise ValueError("AWS credentials require Version 1")
    for key in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
        item = data.get(key)
        if key == "SessionToken" and item is None:
            continue
        if not isinstance(item, str) or not item or any(ord(c) < 0x21 or ord(c) == 0x7F for c in item):
            raise ValueError("AWS credentials contain an invalid credential field")
    deadline = None
    if "Expiration" in data:
        try:
            expiration = datetime.fromisoformat(data["Expiration"].replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                raise ValueError("timezone required")
            deadline = expiration.timestamp() - 30
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("AWS credentials contain an invalid expiration") from exc
        if deadline <= time.time():
            raise ValueError("AWS credentials have expired or are about to expire")
    return data, deadline
