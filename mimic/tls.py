"""TLS certificates: a self-signed fallback so every socket is HTTPS by default."""

from __future__ import annotations

import ipaddress
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from mimic.settings import data_dir, ensure_private_dir, restrict_permissions

log = logging.getLogger(__name__)

CERT_KEY_NAME = "certificate.key"
FULLCHAIN_NAME = "fullchain.pem"

# Long enough that a mock server's fallback cert never expires in practice.
SELF_SIGNED_DAYS = 3650


def cert_paths() -> tuple[Path, Path]:
    """(fullchain, key) under $MIMIC_DATA_DIR/certs — the same files mimic.acme writes."""
    cert_dir = data_dir() / "certs"
    return cert_dir / FULLCHAIN_NAME, cert_dir / CERT_KEY_NAME


def write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    """Write PEM key material owner-only.

    The only writer of private keys under $MIMIC_DATA_DIR/certs. ``os.open`` with
    an explicit mode keeps the file owner-only from creation, so the process umask
    can never expose it; existing files are re-checked and restricted too.
    """
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    # O_TRUNC ignores the mode argument on an existing file, so restrict it first;
    # a pre-existing world-readable key would otherwise stay open during the write.
    if path.exists():
        restrict_permissions(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    restrict_permissions(path)


def _generate_self_signed() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mimic")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # A few minutes of skew so slightly-off clocks don't reject the cert.
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SELF_SIGNED_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_dir = data_dir() / "certs"
    ensure_private_dir(cert_dir)
    fullchain_path, key_path = cert_paths()
    fullchain_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    write_private_key(key_path, key)


def ensure_certificate() -> None:
    """Make sure a usable certificate exists; generate a self-signed one if not.

    Let's Encrypt issuance (mimic.acme.generate_certificate) writes to the same files,
    so an LE cert always overrides the self-signed fallback.
    """
    fullchain_path, key_path = cert_paths()
    if fullchain_path.exists() and key_path.exists():
        # Repair a key an earlier write left world-readable.
        restrict_permissions(key_path)
        return
    _generate_self_signed()
    log.info("No certificates found, generating a self-signed certificate")
