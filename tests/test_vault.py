"""Contract: the vault is field-encrypted, tokenizes idempotently, and
crypto-shreds on forget() (ORLOG-SPEC.md §B7/§S3).
"""

import pytest

from orlog.errors import StorageError
from orlog.vault import Vault, generate_key


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault.sqlite", key=bytes(range(32)))


def test_same_cleartext_gets_the_same_stable_token(vault):
    t1 = vault.tokenize("alice@example.com", kind="EMAIL")
    t2 = vault.tokenize("alice@example.com", kind="EMAIL")
    assert t1 == t2


def test_different_cleartexts_get_different_incrementing_tokens(vault):
    t1 = vault.tokenize("alice@example.com", kind="EMAIL")
    t2 = vault.tokenize("bob@example.com", kind="EMAIL")
    assert t1 != t2
    assert {t1, t2} == {"EMAIL_1", "EMAIL_2"}


def test_resolve_round_trips_the_original_cleartext(vault):
    token = vault.tokenize("alice@example.com", kind="EMAIL")
    assert vault.resolve(token) == "alice@example.com"


def test_resolve_of_an_unknown_token_returns_none(vault):
    assert vault.resolve("EMAIL_999") is None


def test_forget_deletes_the_row_and_resolve_then_fails(vault):
    token = vault.tokenize("alice@example.com", kind="EMAIL")
    assert vault.forget(token) is True
    assert vault.resolve(token) is None
    assert vault.forget(token) is False  # already gone


def test_token_numbers_are_never_reissued_after_forget(vault):
    t1 = vault.tokenize("alice@example.com", kind="EMAIL")
    assert t1 == "EMAIL_1"
    vault.forget(t1)

    t2 = vault.tokenize("bob@example.com", kind="EMAIL")

    assert t2 != t1
    assert t2 == "EMAIL_2"


def test_reopening_a_pre_token_counters_vault_does_not_collide(tmp_path):
    """Simulates a vault.sqlite written before token_counters existed:
    pseudonym rows present (EMAIL_1, EMAIL_2), but no token_counters row for
    "EMAIL" -- reopening it (which runs the migration) and tokenizing a new
    email must not try to re-mint EMAIL_1 and crash on the PRIMARY KEY.
    """
    path = tmp_path / "vault.sqlite"
    v1 = Vault(path, key=bytes(range(32)))
    v1.tokenize("alice@example.com", kind="EMAIL")
    v1.tokenize("bob@example.com", kind="EMAIL")
    # Erase the counter memory to simulate a genuinely pre-migration file --
    # only pseudonyms rows survive, exactly what an old vault.sqlite has.
    v1._conn.execute("DELETE FROM token_counters")
    v1._conn.commit()
    v1.close()

    v2 = Vault(path, key=bytes(range(32)))
    token = v2.tokenize("carol@example.com", kind="EMAIL")

    assert token == "EMAIL_3"  # not a re-minted EMAIL_1/EMAIL_2


def test_ciphertext_on_disk_never_contains_the_cleartext(tmp_path):
    path = tmp_path / "vault.sqlite"
    v = Vault(path, key=bytes(range(32)))
    v.tokenize("super-secret-name@example.com", kind="EMAIL")
    v.close()

    raw = path.read_bytes()
    assert b"super-secret-name" not in raw


def test_missing_vault_key_env_raises_storage_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ORLOG_VAULT_KEY", raising=False)
    with pytest.raises(StorageError):
        Vault(tmp_path / "vault.sqlite")


def test_generated_key_is_usable_via_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())
    v = Vault(tmp_path / "vault.sqlite")
    token = v.tokenize("alice@example.com", kind="EMAIL")
    assert v.resolve(token) == "alice@example.com"
