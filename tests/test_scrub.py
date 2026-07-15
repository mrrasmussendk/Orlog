"""Contract: scrub() replaces every detected PII span with a stable
pseudonym before text is allowed near the log (ORLOG-SPEC.md §B7/§8).
"""

import pytest

from orlog.scrub import scrub
from orlog.vault import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault.sqlite", key=bytes(range(32)))


def test_email_is_replaced_with_a_pseudonym(vault):
    scrubbed = scrub("contact me at alice@example.com please", vault)
    assert "alice@example.com" not in scrubbed
    assert "EMAIL_1" in scrubbed


def test_same_email_twice_gets_the_same_pseudonym(vault):
    scrubbed = scrub("alice@example.com wrote to alice@example.com", vault)
    tokens = [w for w in scrubbed.split() if w.startswith("EMAIL_")]
    assert len(tokens) == 2
    assert tokens[0] == tokens[1]


def test_ssn_like_id_is_detected(vault):
    scrubbed = scrub("SSN on file: 123-45-6789", vault)
    assert "123-45-6789" not in scrubbed
    assert "SSN_1" in scrubbed


def test_key_shaped_string_is_detected(vault):
    scrubbed = scrub("use sk-abcdEFGH1234567890xyz to authenticate", vault)
    assert "sk-abcdEFGH1234567890xyz" not in scrubbed
    assert "SECRET_1" in scrubbed


def test_phone_number_is_detected(vault):
    scrubbed = scrub("call me at +1 415-555-0182 tomorrow", vault)
    assert "415-555-0182" not in scrubbed
    assert "PHONE_1" in scrubbed


def test_dot_separated_phone_number_is_detected(vault):
    scrubbed = scrub("call the office at 212.555.0147 today", vault)
    assert "212.555.0147" not in scrubbed
    assert "PHONE_1" in scrubbed


def test_ip_addresses_decimals_and_version_strings_are_not_mistaken_for_phone_numbers(vault):
    assert scrub("server ip is 192.168.1.100 ok", vault) == "server ip is 192.168.1.100 ok"
    assert scrub("pi is 3.14159265 approx", vault) == "pi is 3.14159265 approx"
    assert scrub("version 1.2.3.4.5.6.7 released", vault) == "version 1.2.3.4.5.6.7 released"


def test_resolving_the_pseudonym_recovers_the_original_text(vault):
    scrubbed = scrub("email alice@example.com", vault)
    token = next(w for w in scrubbed.split() if w.startswith("EMAIL_"))
    assert vault.resolve(token) == "alice@example.com"


def test_disabling_the_regex_pack_leaves_text_untouched(vault):
    text = "contact me at alice@example.com please"
    assert scrub(text, vault, detectors=[]) == text


def test_text_with_no_pii_is_unchanged(vault):
    text = "the sky is blue today"
    assert scrub(text, vault) == text
