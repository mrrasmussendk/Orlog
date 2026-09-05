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
# Credential shapes. The original pattern only matched HYPHEN-delimited
# sk-/pk-/xox[bp]- prefixes, which misses most of what is actually pasted
# into a memory: Stripe and OpenAI now use underscores (sk_live_...,
# sk-proj-...), GitHub uses ghp_/gho_/ghu_/ghs_/ghr_, AWS access key ids
# have no delimiter at all, and Google API keys start AIza. A secret that
# slips past here lands in the append-only log in CLEARTEXT -- so it can
# never be reached by vault.forget()'s crypto-shredding -- and is then
# shipped verbatim to the LLM provider as candidate fact text.
_KEY_SHAPED = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9_\-]{16,}"  # Stripe / OpenAI style
    r"|xox[bpasr]-[A-Za-z0-9\-]{10,}"                                # Slack
    r"|gh[pousr]_[A-Za-z0-9]{36,}"                                   # GitHub
    r"|github_pat_[A-Za-z0-9_]{22,}"                                 # GitHub fine-grained
    r"|AKIA[0-9A-Z]{16}"                                             # AWS access key id
    r"|AIza[0-9A-Za-z_\-]{35}"                                       # Google API key
    r"|sk-ant-[A-Za-z0-9_\-]{16,}"                                   # Anthropic
    r")"
)
# An ISO-8601 calendar date (2026-04-15) otherwise satisfies _PHONE exactly
# -- a leading digit, 8 characters drawn from [digits - space ()], a trailing
# digit -- so every bare date stored anywhere in a memory silently became a
# PHONE token, corrupting the value it was recorded with. Dates are ordinary
# fact content in a temporal memory system (contract_end, start_date, ...),
# so the phone pattern refuses that shape explicitly rather than eating it.
# A date embedded in a longer digit run (a real phone number that happens to
# contain one) is unaffected: the guard only fires when the candidate span
# BEGINS with a complete date.
_ISO_DATE_PREFIX = r"(?!\d{4}-\d{2}-\d{2}(?!\d))"
_PHONE = re.compile(r"(?<!\w)" + _ISO_DATE_PREFIX + r"(\+?\d[\d\-\s()]{7,}\d)(?!\w)")
#: Any ISO date anywhere inside a candidate span, not just at its start.
#: The prefix guard above only refuses a span that BEGINS with a complete
#: date, and the phone class is loose enough to begin one character later:
#: "contract 2026-04-15 2026-04-16 renewal" matched the span
#: "04-15 2026-04-16" -- starting after the first hyphen, where the
#: four-digit lookahead cannot fire -- and durably rewrote two ordinary
#: dates as PHONE_1 in the immutable log.
_ISO_DATE_ANYWHERE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: A real phone number has 7-15 digits (ITU E.164 caps it at 15). The bare
#: "[\d\-\s()]{7,}" class counts separators toward that length, so runs of
#: plainly non-phone grouped numbers ("budget 1 234 567 890 kr",
#: "range 100 - 200 - 300 units") were redacted too.
_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 15


def _is_phone_like(span: str) -> bool:
    """Whether a _PHONE candidate span is plausibly a phone number rather
    than dates or ordinary grouped figures.
    """
    if _ISO_DATE_ANYWHERE.search(span):
        return False
    digits = sum(1 for c in span if c.isdigit())
    return _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS
# NANP-style dot separators (212.555.0147) don't fit the general _PHONE
# class above -- "." can't just be added to it, since that class also
# matches IP addresses, decimals, and dotted version strings (192.168.1.100,
# 3.14159265, 1.2.3.4.5.6.7). This narrower pattern requires the exact
# 3-3-4 digit grouping, which no dotted-quad IP (4 groups) or plain decimal
# (2 groups) can match.
_PHONE_DOTTED = re.compile(r"(?<!\w)(\d{3}\.\d{3}\.\d{4})(?!\w)")

_REGEX_DETECTORS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", _EMAIL),
    ("SSN", _SSN_LIKE),
    ("CPR", _CPR_LIKE),
    ("SECRET", _KEY_SHAPED),
    ("PHONE", _PHONE_DOTTED),
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
        if pattern is _PHONE:
            # The loose phone class needs a plausibility check its regex
            # can't express (see _is_phone_like): leave a non-phone span
            # exactly as written instead of tokenizing it.
            text = pattern.sub(
                lambda m: vault.tokenize(m.group(0), kind="PHONE") if _is_phone_like(m.group(0)) else m.group(0),
                text,
            )
            continue
        text = pattern.sub(lambda m, k=kind: vault.tokenize(m.group(0), kind=k), text)
    return text


def detect_pii_kinds(text: str) -> set[str]:
    """Which regex-pack detectors match anywhere in `text`.

    Detection without tokenization, for callers that need to REFUSE a value
    rather than rewrite it -- see Runtime._reject_pii_in_key, where the
    value is a lookup key that cannot be scrubbed without breaking recall.
    """
    kinds = set()
    for kind, pattern in _REGEX_DETECTORS:
        for match in pattern.finditer(text):
            if pattern is _PHONE and not _is_phone_like(match.group(0)):
                continue
            kinds.add(kind)
            break
    return kinds
