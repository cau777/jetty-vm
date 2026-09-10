import datetime
import os
import sqlite3
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .db import now_iso

CA_SUBJECT_NAME = "Orca Local Interception CA"
CA_VALIDITY_YEARS = 10


def get(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM interception_ca WHERE id = 1").fetchone()


def _generate() -> tuple[str, str, str, datetime.datetime, datetime.datetime]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_SUBJECT_NAME)])
    not_before = datetime.datetime.now(datetime.timezone.utc)
    not_after = not_before + datetime.timedelta(days=365 * CA_VALIDITY_YEARS)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # RFC 5280 4.2.1.2: "MUST appear in all conforming CA certificates."
        # Without it, strict chain validation (OpenSSL 3.2+, seen via Python's
        # ssl module on a VM) rejects every leaf mitmproxy signs off this CA
        # with CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier --
        # confirmed in production once firewall enforcement actually started
        # routing traffic through the proxy for the first time.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    return cert_pem, key_pem, fingerprint, not_before, not_after


def ensure_generated(conn: sqlite3.Connection) -> sqlite3.Row:
    existing = get(conn)
    if existing is not None:
        return existing

    cert_pem, key_pem, fingerprint, not_before, not_after = _generate()
    conn.execute(
        "INSERT INTO interception_ca "
        "(id, certificate_pem, private_key_pem, fingerprint_sha256, not_before, not_after, created_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        (cert_pem, key_pem, fingerprint, not_before.isoformat(), not_after.isoformat(), now_iso()),
    )
    return get(conn)


def materialize(row: sqlite3.Row, path: Path) -> None:
    """Atomically write the combined cert+key PEM to the given path.

    Opens the tmp file with mode 0o600 from creation (os.open, not
    Path.write_text) so the private key is never briefly readable at the
    umask-default mode a plain open()+chmod() sequence would leave it at.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(row["certificate_pem"] + row["private_key_pem"])
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(path)
