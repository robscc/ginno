"""Unit tests for a2a.identity — device keypair + self-signed cert (design §10.3).

Covers: generation round-trip, SAN (IP + DNS), validity window, fingerprint
stability/shape/algorithm, 0600 key file, stdlib ssl loadability, fail-closed
load on mismatched pairs.
"""

from __future__ import annotations

import base64
import hashlib
import ssl
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID, NameOID

from ginno_runtime.a2a import identity as ident

BASE32_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _gen(**kw) -> ident.Identity:
    defaults = dict(
        name="ginno-test-device",
        san_ips=["127.0.0.1", "192.168.1.5"],
        san_dns=["ginno.local"],
    )
    defaults.update(kw)
    return ident.generate_identity(**defaults)


def _paths(tmp_path):
    return tmp_path / "device.crt", tmp_path / "device.key"


def test_generate_save_load_roundtrip(tmp_path):
    original = _gen()
    cert_path, key_path = _paths(tmp_path)
    ident.save_identity(original, cert_path=cert_path, key_path=key_path)
    loaded = ident.load_identity(cert_path, key_path)
    assert loaded.name == original.name
    assert loaded.fingerprint == original.fingerprint
    assert loaded.private_key.public_key() == original.private_key.public_key()
    assert loaded.cert_pem == original.cert_pem


def test_san_contains_ip_and_dns():
    identity = _gen()
    san = identity.certificate.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    dns = set(san.get_values_for_type(x509.DNSName))
    assert ips == {"127.0.0.1", "192.168.1.5"}
    assert dns == {"ginno.local"}


def test_default_validity_is_10_years():
    cert = _gen().certificate
    days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
    assert 3649 <= days <= 3651  # 10 * 365, ± clock-skew tolerance
    assert cert.not_valid_before_utc.tzinfo is not None


def test_validity_years_parameter():
    cert = _gen(validity_years=2).certificate
    days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
    assert 729 <= days <= 731


def test_fingerprint_stable_26_chars_base32():
    identity = _gen()
    der = identity.certificate.public_bytes(serialization.Encoding.DER)
    fp1 = ident.fingerprint(der)
    fp2 = ident.fingerprint(der)
    assert fp1 == fp2 == identity.fingerprint
    assert len(fp1) == 26
    assert set(fp1) <= BASE32_ALPHABET
    assert "=" not in fp1
    # Different cert → different pin.
    other = _gen(name="other-device")
    assert other.fingerprint != fp1


def test_fingerprint_algorithm_is_sha256_base32_truncated():
    # Authoritative cross-check: independent recomputation must agree.
    der = _gen().certificate.public_bytes(serialization.Encoding.DER)
    expected = base64.b32encode(hashlib.sha256(der).digest()).decode("ascii")[:26]
    assert ident.fingerprint(der) == expected


def test_private_key_file_is_0600(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    ident.save_identity(_gen(), cert_path=cert_path, key_path=key_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    # Cert is public information — readable, but not executable.
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o644


def test_ssl_context_loads_cert_chain(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    ident.save_identity(_gen(), cert_path=cert_path, key_path=key_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))  # must not raise


def test_key_is_ecdsa_p256():
    key = _gen().private_key
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.curve.name == "secp256r1"


def test_cert_public_key_matches_private_key():
    identity = _gen()
    assert identity.certificate.public_key() == identity.private_key.public_key()


def test_subject_cn_is_device_name():
    identity = _gen(name="我的设备")
    cn = identity.certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert cn[0].value == "我的设备"


def test_empty_san_still_ssl_loadable(tmp_path):
    identity = ident.generate_identity(name="no-san")
    with pytest.raises(x509.ExtensionNotFound):
        identity.certificate.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
    cert_path, key_path = _paths(tmp_path)
    ident.save_identity(identity, cert_path=cert_path, key_path=key_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))


def test_invalid_san_ip_raises_before_keygen():
    with pytest.raises(ValueError):
        ident.generate_identity(name="bad", san_ips=["not-an-ip"])


def test_invalid_validity_years_raises():
    with pytest.raises(ValueError):
        ident.generate_identity(name="bad", validity_years=0)


def test_load_mismatched_pair_fails_closed(tmp_path):
    a, b = _gen(name="a"), _gen(name="b")
    cert_path, key_path = _paths(tmp_path)
    ident.save_identity(a, cert_path=cert_path, key_path=key_path)
    # Overwrite the key with a different identity's key.
    key_path.write_bytes(b.key_pem)
    with pytest.raises(ValueError, match="does not match"):
        ident.load_identity(cert_path, key_path)


def test_save_creates_parent_dirs(tmp_path):
    nested = tmp_path / "deep" / "nested"
    cert_path, key_path = nested / "d.crt", nested / "d.key"
    ident.save_identity(_gen(), cert_path=cert_path, key_path=key_path)
    assert cert_path.exists() and key_path.exists()


def test_self_signed_issuer_equals_subject():
    cert = _gen().certificate
    assert cert.issuer == cert.subject
    assert cert.not_valid_before_utc <= cert.not_valid_after_utc
    assert cert.not_valid_after_utc.year >= cert.not_valid_before_utc.year + 9
