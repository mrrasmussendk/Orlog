"""vault: the pseudonym registry (ORLOG-SPEC.md §B7/§8).

Design decisions:

1. Field-level AES-256-GCM (not SQLCipher, which needs a system library
   this project doesn't want to depend on) via the `cryptography` package's
   AESGCM primitive directly -- each cleartext value gets a fresh random
   96-bit nonce, so two ciphertexts for the same cleartext never look alike.

2. A second column, `lookup_hash`, is an HMAC-SHA256 of the cleartext
   (keyed with the vault key) -- NOT the AES-GCM ciphertext. This is what
   makes idempotent scrubbing possible ("the same person mentioned twice
   gets the same pseudonym") without ever decrypting the vault just to
   check "have I seen this before": HMAC is deterministic for a fixed key
   and input, so equal cleartexts always produce the same lookup_hash,
   while still being infeasible to invert without the key.

3. Key source: the ORLOG_VAULT_KEY env var (spec §B7), 32 raw bytes,
   base64- or hex-encoded. There is no config-file fallback -- a key is a
   secret, and secrets belong in the environment only (the same principle
   config.py enforces for API keys).

4. Erasure (crypto-shredding, spec §S3/§B7): forget() just deletes the row.
   Once it's gone, the ciphertext is unrecoverable garbage even if a
   backup of the file exists elsewhere -- "deletion" here really is "throw
   away the only key," which for this one token is exactly what happens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from orlog.errors import StorageError

VAULT_KEY_ENV = "ORLOG_VAULT_KEY"

_DDL = """
CREATE TABLE IF NOT EXISTS pseudonyms (
    token TEXT PRIMARY KEY,
    lookup_hash TEXT UNIQUE NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def generate_key() -> str:
    """A fresh, correctly-sized (32-byte) key, base64-encoded -- what
    `orlog init` writes into the ORLOG_VAULT_KEY line of its setup output.
    """
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _load_key_from_env() -> bytes:
    raw = os.environ.get(VAULT_KEY_ENV)
    if not raw:
        raise StorageError(f"{VAULT_KEY_ENV} is not set -- the vault cannot be opened without a key")
    for decode in (lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), bytes.fromhex):
        try:
            key = decode(raw)
        except Exception:
            continue
        if len(key) == 32:
            return key
    raise StorageError(f"{VAULT_KEY_ENV} must decode (base64 or hex) to exactly 32 bytes")


class Vault:
    def __init__(self, path: Path | str, *, key: bytes | None = None) -> None:
        self.path = Path(path)
        self._key = key if key is not None else _load_key_from_env()
        self._aead = AESGCM(self._key)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_DDL)
        self._conn.commit()

    def _lookup_hash(self, cleartext: str) -> str:
        return hmac.new(self._key, cleartext.encode("utf-8"), hashlib.sha256).hexdigest()

    def tokenize(self, cleartext: str, *, kind: str, now: datetime | None = None) -> str:
        """Return the stable pseudonym for `cleartext` (e.g. "PERSON_7"),
        creating one if this exact cleartext hasn't been seen before.
        """
        lookup_hash = self._lookup_hash(cleartext)
        row = self._conn.execute("SELECT token FROM pseudonyms WHERE lookup_hash = ?", (lookup_hash,)).fetchone()
        if row is not None:
            return row[0]

        token = self._next_token(kind)
        nonce = os.urandom(12)
        ciphertext = self._aead.encrypt(nonce, cleartext.encode("utf-8"), None)
        self._conn.execute(
            "INSERT INTO pseudonyms (token, lookup_hash, nonce, ciphertext, kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token, lookup_hash, nonce, ciphertext, kind, (now or datetime.now(timezone.utc)).isoformat()),
        )
        self._conn.commit()
        return token

    def _next_token(self, kind: str) -> str:
        count = self._conn.execute("SELECT COUNT(*) FROM pseudonyms WHERE kind = ?", (kind,)).fetchone()[0]
        return f"{kind}_{count + 1}"

    def resolve(self, token: str) -> str | None:
        """Re-identify a token. Spec §B7: 'only via explicit resolve API,
        never automatic in recall output.' Callers (the MCP server) are
        responsible for gating this behind config.privacy.resolve_entities
        plus a capability token -- the vault itself has no notion of who is
        asking.
        """
        row = self._conn.execute("SELECT nonce, ciphertext FROM pseudonyms WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        nonce, ciphertext = row
        return self._aead.decrypt(nonce, ciphertext, None).decode("utf-8")

    def forget(self, token: str) -> bool:
        """Crypto-shred: delete the row. Returns True if one existed."""
        cursor = self._conn.execute("DELETE FROM pseudonyms WHERE token = ?", (token,))
        self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._conn.close()
