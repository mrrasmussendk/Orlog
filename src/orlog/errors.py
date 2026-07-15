"""errors: the closed error taxonomy (ORLOG-SPEC.md §B8).

Design decision: this module has zero imports from the rest of orlog (like
states.py), so it can be imported anywhere -- including urd.py, the most
foundational module -- without any circular-import risk.

These are internal/operational failures (always bugs, or environment
problems) and are distinct from abstention reasons (UNCITED, EXPIRED, ...),
which are normal, expected outcomes surfaced on the Answer object, not
exceptions. See pipeline.py's Answer.reasons for those.
"""

from __future__ import annotations


class OrlogError(Exception):
    """Base class for every error in the closed taxonomy. `code` is the
    machine-readable identifier spec §B8 defines.
    """

    code: str = "E_UNKNOWN"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class SchemaError(OrlogError):
    """An event failed schema validation on append."""

    code = "E_SCHEMA"


class LockedError(OrlogError):
    """A second server instance tried to open a workspace already locked."""

    code = "E_LOCKED"


class ChainBrokenError(OrlogError):
    """The event log's hash chain does not verify."""

    code = "E_CHAIN_BROKEN"


class ProtocolError(OrlogError):
    """An illegal state transition was attempted -- always a bug.

    Conceptually the same failure states.IllegalTransitionError raises;
    states.py is left untouched (per this project's own ground rules) so it
    does not subclass this, but code outside states.py that wants to surface
    a protocol violation through the E_PROTOCOL taxonomy should raise this.
    """

    code = "E_PROTOCOL"


class VerifierUnavailableError(OrlogError):
    """The verifier's truth source is unreachable. Callers MUST turn this
    into an abstention (reason TRUTH_UNAVAILABLE), never serve unverified.
    """

    code = "E_VERIFIER_UNAVAILABLE"


class RetrieverUnavailableError(OrlogError):
    """The L2 retriever (e.g. a semantic embedder that needs a cold model
    load/download) didn't become ready within its bounded timeout. Callers
    MUST turn this into an abstention (reason EMBEDDER_UNAVAILABLE), never
    block indefinitely -- a single stdio MCP server call hanging blocks
    every other call behind it, which is worse than an honest abstention.
    """

    code = "E_RETRIEVER_UNAVAILABLE"


class StorageError(OrlogError):
    """A storage-layer failure (disk, SQLite index, etc.)."""

    code = "E_STORAGE"


class VerifyTimeoutError(OrlogError):
    """Pipeline.answer()'s derive+verify attempt did not complete within its
    bounded timeout (config.verifier.answer_timeout_s) -- a backstop
    independent of whatever per-call timeout the deriver itself claims to
    enforce, so a read never blocks past this ceiling even if that inner
    timeout fails to fire. Callers MUST turn this into an abstention
    (reason VERIFY_TIMEOUT), same convention as RetrieverUnavailableError.
    """

    code = "E_VERIFY_TIMEOUT"
