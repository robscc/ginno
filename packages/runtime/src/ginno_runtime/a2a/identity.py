"""Device identity: ECDSA P-256 keypair + self-signed X.509 certificate (design §10.3).

Trust model: **pin, not CA**. The certificate is self-signed and is never
validated against a CA chain; the peer verifies the server certificate by
comparing ``fingerprint(cert_der)`` against the ``#fp=`` fragment of the
capability address (design §6.2). This module is the **authoritative**
fingerprint implementation — ``a2a.address`` must call ``fingerprint()``
rather than re-deriving the algorithm, so both sides of the pin cannot drift.

Curve choice: P-256 + SHA-256 (not Ed25519) — stdlib ``ssl`` / OpenSSL accept
it everywhere, which matters because the A2A listener loads the identity via
``ssl.SSLContext.load_cert_chain``.

Files: the caller owns the directory (app data dir); this module only takes
paths. The private key is written 0600 via mkstemp + atomic rename, and key
material never appears in logs or exception text.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# 指纹算法的唯一真源在 address.py（纯 stdlib），此处 re-export 而非重推。
# 两个模块各写一遍同样的两行，今天值一致、改天就会漂移；而 pin 两端漂移意味着
# 「地址里的 #fp 与服务端证书对不上」——首次连接直接被拒，且极难归因。
# 方向选 address → identity 而非反向：address 保持零重依赖（不拖 cryptography），
# identity 本就有 cryptography，多一个 stdlib 兄弟 import 是免费的。
from ginno_runtime.a2a.address import fingerprint_of as fingerprint

__all__ = [
    "Identity",
    "fingerprint",
    "generate_identity",
    "load_identity",
    "save_identity",
]

#: Default certificate lifetime. Long on purpose: trust comes from pinning the
#: fingerprint, not from CA-style short-lived rotation (design §10.3).
DEFAULT_VALIDITY_YEARS = 10

#: Tolerance for peer clock skew — cert is valid from 5 minutes in the past.
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class Identity:
    """A device keypair plus its self-signed certificate."""

    name: str
    private_key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate

    @property
    def fingerprint(self) -> str:
        """Pin value for the capability address ``#fp=`` fragment."""
        return fingerprint(self.certificate.public_bytes(serialization.Encoding.DER))

    @property
    def cert_pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        """Unencrypted PKCS#8 PEM. Protection is the 0600 file mode, not a passphrase."""
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )


def generate_identity(
    *,
    name: str,
    san_ips: Sequence[str] = (),
    san_dns: Sequence[str] = (),
    validity_years: int = DEFAULT_VALIDITY_YEARS,
) -> Identity:
    """Generate a P-256 keypair and a self-signed server certificate.

    ``name`` becomes the certificate CN (device name). ``san_ips`` / ``san_dns``
    populate the SubjectAlternativeName extension — pass the addresses the
    listener will actually bind (design §6.3), e.g. ``["127.0.0.1"]`` for the
    loopback default. Invalid IPs raise ``ValueError`` before any key is made.
    """
    if validity_years < 1:
        raise ValueError("validity_years must be >= 1")
    ips = [ipaddress.ip_address(s) for s in san_ips]  # validates early
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + timedelta(days=365 * validity_years))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    )
    san: list[x509.GeneralName] = [x509.IPAddress(ip) for ip in ips]
    san.extend(x509.DNSName(d) for d in san_dns)
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    return Identity(name=name, private_key=key, certificate=cert)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """temp + rename, same-dir mkstemp so the file is born with ``mode``.

    mkstemp creates 0600; we chmod to the target mode *before* the rename so
    the private key is never briefly world-readable at its final path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_identity(
    identity: Identity, *, cert_path: str | Path, key_path: str | Path
) -> None:
    """Persist cert (0644, public) and private key (0600) as PEM.

    Paths are caller-owned (app data dir); parent dirs are created as needed.
    """
    _atomic_write(Path(key_path), identity.key_pem, 0o600)
    _atomic_write(Path(cert_path), identity.cert_pem, 0o644)


def load_identity(cert_path: str | Path, key_path: str | Path) -> Identity:
    """Load a previously saved identity; verifies the cert matches the key.

    Raises ``ValueError`` on a mismatched pair (fail-closed: a wrong key must
    never silently produce a server whose fingerprint differs from the pin).
    """
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("identity private key must be an EC key")
    if key.public_key() != cert.public_key():
        raise ValueError("certificate does not match private key")
    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    name = str(cn_attrs[0].value) if cn_attrs else ""
    return Identity(name=name, private_key=key, certificate=cert)
