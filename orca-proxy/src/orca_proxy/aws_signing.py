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


class UnknownAWSEndpoint(UnsupportedAWSRequest):
    """An auto-scoped rule matched a host that is not a SigV4 API endpoint."""


@lru_cache(maxsize=1)
def _endpoint_metadata():
    """Keep endpoint metadata and signing names, without retaining SDK clients."""
    session = Session()
    resolver = session._get_internal_component("endpoint_resolver")
    partitions = session.get_component("data_loader").load_data("endpoints")["partitions"]
    signing_names = {}
    for name in session.get_available_services():
        model = session.get_service_model(name)
        signing_names[model.endpoint_prefix] = model.signing_name
    regions = {region for partition in partitions for region in partition["regions"]}
    globals_ = [
        (prefix, region, partition["partition"])
        for partition in partitions
        for prefix, service in partition["services"].items()
        if prefix in signing_names
        for region, endpoint in service.get("endpoints", {}).items()
        if region not in regions and not endpoint.get("deprecated", False)
    ]
    return resolver, signing_names, regions, globals_


def _add_endpoints(catalog, prefix, region, partition=None):
    from botocore.exceptions import EndpointVariantError

    resolver, signing_names, regions, _ = _endpoint_metadata()
    for dualstack, fips in ((False, False), (True, False), (False, True), (True, True)):
        try:
            endpoint = resolver.construct_endpoint(
                prefix, region, partition_name=partition,
                use_dualstack_endpoint=dualstack, use_fips_endpoint=fips,
            )
        except EndpointVariantError:
            continue
        if not endpoint:
            continue
        scope = endpoint.get("credentialScope", {})
        signing_region = scope.get("region", region)
        if signing_region not in regions:
            continue
        hostname = endpoint["hostname"]
        if "{" not in hostname:
            catalog[hostname] = (scope.get("service", signing_names[prefix]), signing_region)


@lru_cache(maxsize=16)
def _regional_signing_scopes(region):
    catalog = {}
    for prefix in _endpoint_metadata()[1]:
        _add_endpoints(catalog, prefix, region)
    return catalog


@lru_cache(maxsize=1)
def _global_signing_scopes():
    catalog = {}
    for prefix, region, partition in _endpoint_metadata()[3]:
        _add_endpoints(catalog, prefix, region, partition)
    return catalog


def resolve_injection(hostname: str, injection: dict) -> dict:
    if injection["service"] != "auto" and injection["region"] != "auto":
        return injection
    hostname = hostname.lower().rstrip(".")
    known_regions = _endpoint_metadata()[2]
    region = next((label for label in hostname.split(".") if label in known_regions), None)
    catalog = _regional_signing_scopes(region) if region else _global_signing_scopes()
    scope = catalog.get(hostname)
    if scope is None:
        # Only these known endpoint families accept extra resource labels.
        for endpoint, endpoint_scope in catalog.items():
            if endpoint_scope[0] in {"s3", "execute-api"} and hostname.endswith("." + endpoint):
                scope = endpoint_scope
                break
    if scope is None:
        raise UnknownAWSEndpoint("Automatic AWS signing requires a known AWS endpoint")
    service, signing_region = scope
    if injection["region"] not in {"auto", signing_region}:
        raise UnsupportedAWSRequest("AWS endpoint is outside the configured signing region")
    if injection["service"] not in {"auto", service}:
        raise UnsupportedAWSRequest("AWS endpoint does not match the configured signing service")
    return {**injection, "service": service, "region": signing_region}


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
