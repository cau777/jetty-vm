"""Sign an intercepted request using rule-scoped, host-held AWS credentials."""

from hashlib import sha256
from functools import lru_cache
import logging
from urllib.parse import parse_qsl, urlsplit

from botocore.auth import S3SigV4Auth, SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import Session

from orca_proxy.aws_credentials import parse_credentials

# Botocore's debug canonical-request dump includes the real session token.
logging.getLogger("botocore.auth").setLevel(logging.WARNING)


class UnsupportedAWSRequest(ValueError):
    """A request that cannot safely be re-signed by this implementation."""


@lru_cache(maxsize=16)
def _regional_signing_services(region: str) -> dict[str, str]:
    """Resolve known regional endpoints using the bundled SDK metadata."""
    session = Session()
    resolver = session._get_internal_component("endpoint_resolver")
    services = {}
    for name in session.get_available_services():
        model = session.get_service_model(name)
        endpoint = resolver.construct_endpoint(model.endpoint_prefix, region)
        if not endpoint:
            continue
        scope = endpoint.get("credentialScope", {})
        if scope.get("region", region) != region:
            continue
        service = scope.get("service", model.signing_name)
        for variant in [endpoint, *endpoint.get("variants", [])]:
            hostname = variant.get("hostname", "")
            if "{" not in hostname and f".{region}." in hostname:
                services[hostname] = service
    return services


def resolve_injection(hostname: str, injection: dict) -> dict:
    if injection["service"] != "auto":
        return injection
    hostname = hostname.lower().rstrip(".")
    services = _regional_signing_services(injection["region"])
    service = services.get(hostname)
    if service is None:
        # Only these known endpoint families accept an extra resource label.
        for endpoint, signing_name in services.items():
            if signing_name in {"s3", "execute-api"} and hostname.endswith("." + endpoint):
                service = signing_name
                break
    if service is None:
        raise UnsupportedAWSRequest("Automatic AWS signing requires a known endpoint in the configured region")
    return {**injection, "service": service}


def validate_request(request, hostname: str) -> None:
    if request.scheme != "https":
        raise UnsupportedAWSRequest("AWS signing requires HTTPS to the rule's hostname")
    # Bind the HTTP authority as well as the URL to the intercepted TLS name.
    authority = request.host_header
    expected = hostname if request.port == 443 else f"{hostname}:{request.port}"
    if not authority and request.host.lower().rstrip(".") != hostname.lower().rstrip("."):
        raise UnsupportedAWSRequest("AWS request authority does not match its TLS hostname")
    if authority and authority.lower().rstrip(".") != expected.lower().rstrip("."):
        raise UnsupportedAWSRequest("AWS request authority does not match its TLS hostname")
    if request.raw_content is None:
        raise UnsupportedAWSRequest("AWS signing requires a buffered request body")
    payload = request.headers.get("x-amz-content-sha256", "")
    encoding = request.headers.get("content-encoding", "")
    if payload.startswith("STREAMING-") or "aws-chunked" in encoding.lower():
        raise UnsupportedAWSRequest("AWS streaming uploads are not supported")
    if "x-amz-region-set" in request.headers or request.headers.get("authorization", "").startswith("AWS4-ECDSA"):
        raise UnsupportedAWSRequest("AWS SigV4a is not supported")
    if any(key.lower().startswith("x-amz-") for key, _ in parse_qsl(urlsplit(request.url).query)):
        raise UnsupportedAWSRequest("AWS presigned requests are not supported")


def sign_request(request, hostname: str, injection: dict, value: str) -> None:
    validate_request(request, hostname)
    injection = resolve_injection(hostname, injection)
    data, _ = parse_credentials(value)
    include_payload_hash = "x-amz-content-sha256" in request.headers or injection["service"] == "s3"
    # Discard guest authentication and payload claims; sign the bytes that
    # will actually be sent. Do not decode or normalize S3 object paths.
    for name in ("authorization", "x-amz-security-token", "x-amz-date", "date", "x-amz-content-sha256"):
        request.headers.pop(name, None)
    request.headers["Host"] = hostname if request.port == 443 else f"{hostname}:{request.port}"
    if include_payload_hash:
        request.headers["X-Amz-Content-SHA256"] = sha256(request.raw_content).hexdigest()
    # Transparent mode stores the destination IP in request.host. Use the
    # authorized TLS name for signing, retaining the IP for upstream routing.
    authority = hostname if request.port == 443 else f"{hostname}:{request.port}"
    aws_request = AWSRequest(method=request.method, url=f"https://{authority}{request.path}", data=request.raw_content)
    for name, header_value in request.headers.items(multi=True):
        aws_request.headers.add_header(name, header_value)
    credentials = Credentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("SessionToken"))
    signer = S3SigV4Auth if injection["service"] == "s3" else SigV4Auth
    signer(credentials, injection["service"], injection["region"]).add_auth(aws_request)
    for name in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256"):
        if name in aws_request.headers:
            request.headers[name] = aws_request.headers[name]
