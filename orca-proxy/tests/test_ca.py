from pathlib import Path

from cryptography import x509


async def test_ca_certificate_has_subject_key_identifier(client, app):
    """RFC 5280 4.2.1.2 requires this on every CA certificate; without it,
    strict TLS clients (OpenSSL 3.2+) reject the chain of every leaf
    mitmproxy signs off this CA with CERTIFICATE_VERIFY_FAILED: Missing
    Subject Key Identifier.
    """
    resp = await client.get("/api/v1/ca")
    body = await resp.json()
    cert = x509.load_pem_x509_certificate(body["certificate_pem"].encode())
    cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)


async def test_get_ca_returns_public_material_only(client, app):
    resp = await client.get("/api/v1/ca")
    body = await resp.json()
    assert resp.status == 200
    assert "BEGIN CERTIFICATE" in body["certificate_pem"]
    assert "private_key_pem" not in body
    assert "BEGIN PRIVATE KEY" not in str(body)
    assert body["subject"] == "Orca Local Interception CA"
    assert len(body["fingerprint_sha256"]) == 64  # hex-encoded SHA-256


async def test_ca_materialized_to_data_dir(client, app):
    data_dir = Path(app["db"].execute("PRAGMA database_list").fetchone()["file"]).parent
    # Must match mitmproxy's own CertStore lookup exactly (confdir/mitmproxy-ca.pem)
    # or mitmdump silently signs with a different, auto-generated CA.
    ca_path = data_dir / "mitm-confdir" / "mitmproxy-ca.pem"
    assert ca_path.exists()
    contents = ca_path.read_text()
    assert "BEGIN CERTIFICATE" in contents
    assert "BEGIN PRIVATE KEY" in contents
    assert oct(ca_path.stat().st_mode)[-3:] == "600"


async def test_ca_stable_across_restarts(client, app):
    resp1 = await client.get("/api/v1/ca")
    body1 = await resp1.json()

    # Re-run create_app() against the same data dir (simulating a restart)
    # and confirm the CA isn't regenerated.
    from orca_proxy.app import create_app

    app2 = create_app()
    row = app2["db"].execute("SELECT fingerprint_sha256 FROM interception_ca").fetchone()
    assert row["fingerprint_sha256"] == body1["fingerprint_sha256"]
    app2["db"].close()
