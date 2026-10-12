"""Sign an intercepted request using rule-scoped, host-held AWS credentials."""

from hashlib import sha256
import logging
from urllib.parse import parse_qsl, urlsplit

from botocore.auth import S3SigV4Auth, SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from orca_proxy.aws_credentials import parse_credentials

# Botocore's debug canonical-request dump includes the real session token.
logging.getLogger("botocore.auth").setLevel(logging.WARNING)


class UnsupportedAWSRequest(ValueError):
    """A request that cannot safely be re-signed by this implementation."""


def validate_request(request, hostname: str) -> None:
    if request.scheme != "https" or request.host.lower().rstrip(".") != hostname.lower().rstrip("."):
        raise UnsupportedAWSRequest("AWS signing requires HTTPS to the rule's hostname")
    # Bind the HTTP authority as well as the URL to the intercepted TLS name.
    authority = request.host_header
    expected = hostname if request.port == 443 else f"{hostname}:{request.port}"
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
    data, _ = parse_credentials(value)
    include_payload_hash = "x-amz-content-sha256" in request.headers or injection["service"] == "s3"
    # Discard guest authentication and payload claims; sign the bytes that
    # will actually be sent. Do not decode or normalize S3 object paths.
    for name in ("authorization", "x-amz-security-token", "x-amz-date", "date", "x-amz-content-sha256"):
        request.headers.pop(name, None)
    request.headers["Host"] = hostname if request.port == 443 else f"{hostname}:{request.port}"
    if include_payload_hash:
        request.headers["X-Amz-Content-SHA256"] = sha256(request.raw_content).hexdigest()
    aws_request = AWSRequest(method=request.method, url=request.url, data=request.raw_content)
    for name, header_value in request.headers.items(multi=True):
        aws_request.headers.add_header(name, header_value)
    credentials = Credentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("SessionToken"))
    signer = S3SigV4Auth if injection["service"] == "s3" else SigV4Auth
    signer(credentials, injection["service"], injection["region"]).add_auth(aws_request)
    for name in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256"):
        if name in aws_request.headers:
            request.headers[name] = aws_request.headers[name]
