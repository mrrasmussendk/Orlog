"""scrub: PII detection and tokenization, run before every append
(ORLOG-SPEC.md §B7/§8).

Design decisions:

1. Detectors are a small, fixed list of (kind, compiled regex) pairs -- the
   "regex pack" spec §B7 says is "always on". An optional local NER pack is
   a named, config-gated extension point (config.privacy.detectors can list
   "ner") but is not implemented here; this reference ships the regex pack
   only. Order matters: more specific patterns (SSN/CPR/key-shaped) run
   before the looser phone pattern, so e.g. a CPR number isn't first
   swallowed as "a phone".

2. scrub() takes a Vault so a detected span becomes a STABLE pseudonym (the
   same phone number mentioned twice gets the same token), not a fresh one
   each call -- see vault.py's own docstring for how that idempotency
   works without ever decrypting the vault to check "have I seen this".

3. This is the one place raw PII is allowed to exist in memory at all
   (spec §8, principle 11 in DESIGN-PRINCIPLES.md: scrub by tokenization,
   never by deletion) -- callers MUST run text through scrub() before it
   ever reaches EventLog.append(); nothing downstream of this module should
   see unscrubbed text.
"""

from __future__ import annotations

import re

from orlog.vault import Vault

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SSN_LIKE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")  # US SSN shape: NNN-NN-NNNN
_CPR_LIKE = re.compile(r"(?<!\d)\d{6}-\d{4}(?!\d)")  # Nordic CPR shape: DDMMYY-XXXX
_KEY_SHAPED = re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|xox[bp])-[A-Za-z0-9_\-]{16,}")
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\-\s()]{7,}\d)(?!\w)")

_REGEX_DETECTORS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", _EMAIL),
    ("SSN", _SSN_LIKE),
    ("CPR", _CPR_LIKE),
    ("SECRET", _KEY_SHAPED),
    ("PHONE", _PHONE),
]


def scrub(text: str, vault: Vault, *, detectors: list[str] | None = None) -> str:
    """Replace every detected span in `text` with a stable pseudonym token.

    `detectors` (config.privacy.detectors) selects which packs run.
    Defaults to running the regex pack; pass an explicit list (e.g. `[]`)
    to disable it, or a list containing "regex" to enable it explicitly.
    """
    active = detectors if detectors is not None else ["regex"]
    if "regex" not in active:
        return text

    for kind, pattern in _REGEX_DETECTORS:
        text = pattern.sub(lambda m, k=kind: vault.tokenize(m.group(0), kind=k), text)
    return text
