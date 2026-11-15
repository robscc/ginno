"""Unit tests for a2a.auth — capability-address token lifecycle (design §6.2, §11.3.4).

Covers: token shape/entropy, hash one-wayness, constant-time verify, one-time
single consumption + 10-minute expiry, immediate revoke, concurrent issue
uniqueness, plaintext never on disk, persistence across instances.
"""

from __future__ import annotations

import json as stdlib_json
import re
import string
from concurrent.futures import ThreadPoolExecutor

import pytest

from ginno_runtime.a2a import auth

BASE32_ALPHABET = set(string.ascii_uppercase + "234567")


@pytest.fixture()
def store(tmp_path):
    return auth.TokenStore(tmp_path / "tokens.json")


# -- token primitives ------------------------------------------------------


def test_token_shape_26_base32_chars():
    for _ in range(200):
        token = auth.generate_token()
        assert len(token) == 26
        assert set(token) <= BASE32_ALPHABET
        assert "=" not in token


def test_token_entropy_rough_check():
    # 512 samples: all unique, and every base32 symbol shows up somewhere —
    # a stuck/degenerate RNG would fail one of these. ~130 bit nominal.
    tokens = [auth.generate_token() for _ in range(512)]
    assert len(set(tokens)) == 512
    seen = set("".join(tokens))
    assert seen == BASE32_ALPHABET


def test_token_hash_deterministic_sha256_hex():
    token = auth.generate_token()
    h = auth.token_hash(token)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert auth.token_hash(token) == h
    assert auth.token_hash(auth.generate_token()) != h


def test_hash_is_not_reversible_to_token():
    token = auth.generate_token()
    h = auth.token_hash(token)
    assert h != token
    # The hash itself is not a valid credential for its own record.
    assert auth.verify_token(h, h) is False
    assert auth.verify_token(token, token) is False


def test_verify_token_true_and_false():
    token = auth.generate_token()
    h = auth.token_hash(token)
    assert auth.verify_token(token, h) is True
    assert auth.verify_token(auth.generate_token(), h) is False
    assert auth.verify_token("", h) is False
    assert auth.verify_token(token, "0" * 64) is False


def test_verify_token_uses_constant_time_compare(monkeypatch):
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    token = auth.generate_token()
    assert auth.verify_token(token, auth.token_hash(token)) is True
    assert len(calls) == 1  # digest comparison actually happened


# -- TokenStore lifecycle ---------------------------------------------------


def test_issue_verify_revoke_flow(store):
    token = store.issue(note="laptop")
    assert store.verify(token) is True
    assert store.revoke(token) is True
    assert store.verify(token) is False
    assert store.revoke(token) is False  # already revoked


def test_one_time_single_consumption(store):
    token = store.issue(kind="one_time")
    assert store.verify(token) is True
    assert store.verify(token) is False  # consumed
    assert store.list() == []


def test_one_time_default_ttl_is_600s(store):
    token = store.issue(kind="one_time")
    (record,) = store.list()
    assert record["expires_at"] - record["issued_at"] == 600
    assert record["kind"] == "one_time"
    assert store.verify(token) is True


def test_one_time_expires_after_10_minutes(store, monkeypatch):
    t0 = 1_000_000.0
    monkeypatch.setattr(auth, "_now", lambda: t0)
    token = store.issue(kind="one_time")
    monkeypatch.setattr(auth, "_now", lambda: t0 + 601)
    assert store.verify(token) is False  # expired unused → dead
    assert store.list() == []  # lazily pruned


def test_one_time_still_valid_just_before_expiry(store, monkeypatch):
    t0 = 1_000_000.0
    monkeypatch.setattr(auth, "_now", lambda: t0)
    token = store.issue(kind="one_time", ttl_s=600)
    monkeypatch.setattr(auth, "_now", lambda: t0 + 599)
    assert store.verify(token) is True


def test_long_term_does_not_expire(store, monkeypatch):
    t0 = 1_000_000.0
    monkeypatch.setattr(auth, "_now", lambda: t0)
    token = store.issue()
    monkeypatch.setattr(auth, "_now", lambda: t0 + 10 * 365 * 86400)
    assert store.verify(token) is True


def test_revoke_by_hash_works(store):
    token = store.issue()
    h = auth.token_hash(token)
    assert store.revoke(h) is True
    assert store.verify(token) is False


def test_unknown_token_verify_is_false(store):
    assert store.verify(auth.generate_token()) is False
    assert store.verify("") is False


def test_unknown_kind_and_bad_ttl_raise(store):
    with pytest.raises(ValueError):
        store.issue(kind="forever")
    with pytest.raises(ValueError):
        store.issue(kind="one_time", ttl_s=0)


# -- storage properties -----------------------------------------------------


def test_plaintext_never_on_disk(store, tmp_path):
    token = store.issue(kind="one_time", note="secret-label")
    raw = (tmp_path / "tokens.json").read_text()
    assert token not in raw
    assert auth.token_hash(token) in raw
    assert "secret-label" in raw  # note is metadata, fine to persist


def test_list_contains_no_plaintext(store):
    token = store.issue()
    (record,) = store.list()
    assert record["hash"] == auth.token_hash(token)
    assert token not in str(record)
    assert record["revoked"] is False


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "tokens.json"
    token = auth.TokenStore(path).issue()
    assert auth.TokenStore(path).verify(token) is True


def test_store_file_is_0600(store, tmp_path):
    import stat

    store.issue()
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600


def test_corrupt_store_fails_closed(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        auth.TokenStore(path).list()


def test_concurrent_issue_unique_and_all_valid(store):
    def issue(_i):
        return store.issue()

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(issue, range(200)))
    assert len(set(tokens)) == 200
    assert all(store.verify(t) for t in tokens)
    assert len(store.list()) == 200
    # File on disk is still valid JSON with exactly 200 records.
    data = stdlib_json.loads(store._path.read_text())
    assert len(data["tokens"]) == 200


def test_concurrent_one_time_verify_exactly_one_winner(store):
    token = store.issue(kind="one_time")

    def verify(_i):
        return store.verify(token)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(verify, range(32)))
    assert results.count(True) == 1
    assert results.count(False) == 31
