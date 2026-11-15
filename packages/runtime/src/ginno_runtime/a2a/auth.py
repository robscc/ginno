"""Capability-address tokens: issue / verify / revoke (design §6.2, §11.3.4).

Address-as-credential: the token **is** the secret. The server stores only
``token_hash(token)`` (SHA-256 hex) — a leaked token store cannot be replayed
(design §10.3). Plaintext is returned exactly once, at issue time, and never
touches disk, logs, or exception text.

Two kinds (design §11.3.4):
  long_term — default; valid until revoked.
  one_time  — consumed on the first successful verify; expires unused after
              ``ttl_s`` (default 600 s). Expiry is pruned lazily on
              verify/list — no background timer.

Storage: one JSON file, temp + rename atomic writes (repo ``jsonx`` idiom).
A ``threading.Lock`` serializes read-modify-write inside this process; the
file is 0600 because hashes are credential-equivalent. Corrupt JSON raises
(fail-closed: silently resetting would drop revocations).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ginno_runtime import jsonx as json

__all__ = [
    "TokenStore",
    "generate_token",
    "token_hash",
    "verify_token",
]

#: 26 base32 chars ≈ 130 bit — same shape as the fingerprint (design §6.2).
_TOKEN_CHARS = 26
_TOKEN_BYTES = 17  # 136 bit of entropy, truncated to the 130-bit address shape

#: Default one-time TTL: 10 minutes unused → dead (design §11.3.4).
ONE_TIME_TTL_S = 600

_KINDS = ("long_term", "one_time")

#: Fixed digest for the not-found compare, so a missing token costs the same
#: as a present one (no existence oracle via timing).
_DUMMY_HASH = "0" * 64


def _now() -> float:
    """Wall clock, isolated for tests (monkeypatch this, not global time)."""
    return time.time()


def generate_token() -> str:
    """A fresh capability token: 26 base32 chars (A–Z, 2–7), ~130 bit."""
    return base64.b32encode(secrets.token_bytes(_TOKEN_BYTES)).decode("ascii")[:_TOKEN_CHARS]


def token_hash(token: str) -> str:
    """SHA-256 hex of the token — the only form the server persists."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    """Constant-time comparison of ``token_hash(token)`` against the stored hash."""
    if not isinstance(token, str) or not isinstance(stored_hash, str):
        return False
    return hmac.compare_digest(token_hash(token), stored_hash)


class TokenStore:
    """Minimal JSON-file token registry. Path is caller-owned.

    Records are keyed by hash; plaintext exists only in the return value of
    :meth:`issue`. ``verify`` on a ``one_time`` token deletes it atomically
    *before* returning True (crash-safe single consumption).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    # -- public API ------------------------------------------------------

    def issue(
        self,
        *,
        kind: str = "long_term",
        ttl_s: int | None = None,
        note: str = "",
    ) -> str:
        """Mint a token, persist its hash, return the plaintext exactly once.

        ``kind='one_time'`` defaults to ``ttl_s=600``; ``long_term`` defaults
        to no expiry. ``note`` is a human label for the list view.
        """
        if kind not in _KINDS:
            raise ValueError(f"unknown token kind: {kind!r}")
        if ttl_s is None and kind == "one_time":
            ttl_s = ONE_TIME_TTL_S
        if ttl_s is not None and ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        token = generate_token()
        now = _now()
        record = {
            "kind": kind,
            "issued_at": now,
            "expires_at": now + ttl_s if ttl_s is not None else None,
            "revoked": False,
            "note": note,
        }
        with self._lock:
            data = self._load()
            data["tokens"][token_hash(token)] = record
            self._save(data)
        return token

    def verify(self, token: str) -> bool:
        """True iff the token is live; consumes one-time tokens on success."""
        h = token_hash(token)
        with self._lock:
            data = self._load()
            tokens = data["tokens"]
            changed = self._prune(tokens)
            record = tokens.get(h)
            ok = record is not None and not record.get("revoked", False)
            if ok and record.get("kind") == "one_time":
                del tokens[h]
                changed = True
            if changed:
                self._save(data)
            if not ok:
                # Same digest work as the success path — no timing oracle.
                hmac.compare_digest(h, _DUMMY_HASH)
            return ok

    def revoke(self, token_or_hash: str) -> bool:
        """Revoke by plaintext token or by stored hash. False if unknown/already revoked.

        The record is kept (flagged) so the list view can show what was revoked.
        """
        h = token_hash(token_or_hash)
        with self._lock:
            data = self._load()
            tokens = data["tokens"]
            record = tokens.get(h)
            if record is None:
                record = tokens.get(token_or_hash)  # caller passed the hash itself
            if record is None or record.get("revoked", False):
                return False
            record["revoked"] = True
            self._save(data)
            return True

    def list(self) -> list[dict[str, Any]]:
        """All live-or-revoked records (hash + metadata, never plaintext), oldest first.

        Expired records are lazily pruned here too.
        """
        with self._lock:
            data = self._load()
            if self._prune(data["tokens"]):
                self._save(data)
            return [
                {"hash": h, **record}
                for h, record in sorted(
                    data["tokens"].items(), key=lambda kv: kv[1].get("issued_at", 0)
                )
            ]

    # -- internals -------------------------------------------------------

    def _prune(self, tokens: dict[str, Any]) -> bool:
        """Drop expired records (lazy expiry). Returns True if anything changed."""
        now = _now()
        dead = [
            h
            for h, r in tokens.items()
            if r.get("expires_at") is not None and now >= r["expires_at"]
        ]
        for h in dead:
            del tokens[h]
        return bool(dead)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "tokens": {}}
        try:
            data = json.loads(self._path.read_bytes())
        except (OSError, ValueError) as exc:
            # Fail-closed: a corrupt store must not silently drop revocations.
            raise ValueError(f"token store unreadable: {self._path.name}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
            raise ValueError(f"token store malformed: {self._path.name}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)  # mkstemp mode 0600 carries over
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
