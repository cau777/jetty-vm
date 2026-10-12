"""AWS's published SigV4 vector plus request preservation and rejection."""
import json
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from mitmproxy import http

from orca_proxy.aws_signing import UnsupportedAWSRequest, sign_request

VALUE = json.dumps({"Version": 1, "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"})
INJECTION = {"type": "aws_sigv4", "service": "service", "region": "us-east-1"}


def test_aws_published_get_vanilla_vector(monkeypatch):
    # https://github.com/boto/botocore/blob/develop/tests/unit/auth/aws4_testsuite/get-vanilla/get-vanilla.authz
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda: datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc))
    request = http.Request.make("GET", "https://example.amazonaws.com/")
    request.headers.pop("Content-Length", None)
    sign_request(request, "example.amazonaws.com", INJECTION, VALUE)
    assert request.headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_s3_preserves_raw_path_query_and_body_and_replaces_guest_auth():
    url = "https://bucket.s3.us-east-1.amazonaws.com/a//b%20c/../d?tag=z&tag=a&x=%2F"
    request = http.Request.make("PUT", url, b"\x00\xffpayload", {
        "Authorization": "guest-signature", "X-Amz-Security-Token": "guest-token",
        "X-Amz-Date": "20000101T000000Z", "X-Amz-Content-SHA256": "UNSIGNED-PAYLOAD",
    })
    value = json.loads(VALUE)
    value["SessionToken"] = "host-token"
    sign_request(request, request.host, {**INJECTION, "service": "s3"}, json.dumps(value))
    assert request.url == url
    assert request.raw_content == b"\x00\xffpayload"
    assert request.headers["X-Amz-Content-SHA256"] == sha256(b"\x00\xffpayload").hexdigest()
    assert request.headers["X-Amz-Security-Token"] == "host-token"
    assert request.headers["X-Amz-Date"] != "20000101T000000Z"
    assert "/us-east-1/s3/aws4_request" in request.headers["Authorization"]
    assert "guest" not in request.headers["Authorization"]


def test_long_term_credentials_remove_guest_session_token():
    request = http.Request.make("GET", "https://example.amazonaws.com/", headers={"X-Amz-Security-Token": "guest-token"})
    sign_request(request, request.host, INJECTION, VALUE)
    assert "X-Amz-Security-Token" not in request.headers


@pytest.mark.parametrize("headers", [
    {"X-Amz-Content-SHA256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"},
    {"Content-Encoding": "aws-chunked"}, {"X-Amz-Region-Set": "*"},
    {"Authorization": "AWS4-ECDSA-P256-SHA256 guest"}, {"Host": "evil.example"},
])
def test_unsupported_requests_are_rejected_before_mutation(headers):
    request = http.Request.make("PUT", "https://example.amazonaws.com/", b"body", headers)
    request.headers.update(headers)
    original = request.headers.copy()
    with pytest.raises(UnsupportedAWSRequest):
        sign_request(request, "example.amazonaws.com", INJECTION, VALUE)
    assert request.headers == original


@pytest.mark.parametrize("url,hostname", [
    ("https://example.amazonaws.com/?X-Amz-Signature=guest", "example.amazonaws.com"),
    ("http://example.amazonaws.com/", "example.amazonaws.com"),
    ("https://evil.example/", "example.amazonaws.com"),
])
def test_presigned_plaintext_and_wrong_destinations_are_rejected(url, hostname):
    with pytest.raises(UnsupportedAWSRequest):
        sign_request(http.Request.make("GET", url), hostname, INJECTION, VALUE)
