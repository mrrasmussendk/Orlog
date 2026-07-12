"""heimdall_pytest: the 'pytest' built-in verifier -- the coding-agent
wedge (ORLOG-SPEC.md §B11).

Design decisions:

1. Ground truth = the test suite itself, not any log-derived window table.
   Citations for this verifier reference "fact" events whose payload
   carries {claim_text: str, test_ids: [str, ...]} -- a different
   convention from the entity/attribute/value shape orlog.heimdall (the
   "windows" verifier) uses, because this verifier's domain is "is this
   claim about the code still backed by passing tests", not "is this claim
   about a point-in-time fact still the current one".

2. V3 (VALID) runs the cited test_ids via `test_command` in a subprocess,
   once per verify() call, and passes only if every one of them collects
   AND passes. `truth_version` is a hash of pytest's own --collect-only
   report at construction time -- a cheap proxy for spec's "HEAD commit +
   test collection hash": it changes whenever the test suite's shape
   changes, without this reference implementation needing to shell out to
   git.

3. Unreachable pytest / a broken collection is NOT a normal V3 failure
   (EXPIRED) -- it's TRUTH_UNAVAILABLE (spec §B8's error taxonomy): "the
   truth source itself is broken" is a more serious, different situation
   than "this specific fact is no longer true", worth telling apart in the
   failures list even though pipeline.py treats every failure code the
   same way (abstain with the reason).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from orlog.heimdall import VerificationFailure, VerificationResult
from orlog.huginn import Assertion

UNCITED = "<uncited>"
DEFAULT_TEST_COMMAND = f"{sys.executable} -m pytest"


class PytestVerifier:
    """`facts` maps citation id -> {"claim_text": str, "test_ids": [str, ...]}
    -- built by the caller directly from the log's "fact" events, the same
    way heimdall.build_ground_truth is: never from a projection.
    """

    def __init__(
        self,
        facts: dict[str, dict],
        *,
        test_command: str = DEFAULT_TEST_COMMAND,
        cwd: Path | str = ".",
        timeout_s: float = 60.0,
    ) -> None:
        self._facts = facts
        self._test_command = test_command
        self._cwd = Path(cwd)
        self._timeout_s = timeout_s
        self.truth_version = self._collection_fingerprint()

    def _run(self, extra_args: list[str]) -> subprocess.CompletedProcess | None:
        try:
            # posix=False on Windows: plain shlex.split() treats backslashes
            # as escape characters and mangles Windows paths (e.g.
            # sys.executable) -- posix=False keeps them intact.
            command = shlex.split(self._test_command, posix=(os.name != "nt"))
            return subprocess.run(
                [*command, *extra_args],
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _collection_fingerprint(self) -> str:
        result = self._run(["--collect-only", "-q"])
        if result is None:
            return "unavailable"
        digest = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()[:12]
        return f"pytest@{digest}"

    def _tests_currently_pass(self, test_ids: list[str]) -> bool | None:
        """True/False if the run completed; None if pytest itself couldn't
        run at all (TRUTH_UNAVAILABLE, not a normal fail).
        """
        if not test_ids:
            return False
        result = self._run(test_ids)
        return None if result is None else result.returncode == 0

    def verify(self, assertion: Assertion, as_of: datetime, *, checked_at: datetime) -> VerificationResult:
        if not assertion.citations:
            return VerificationResult(
                assertion_query_id=assertion.query_id,
                status="fail",
                checked_at=checked_at,
                truth_version=self.truth_version,
                failures=[VerificationFailure(citation=UNCITED, code="UNCITED", detail="assertion has no citations")],
            )

        failures: list[VerificationFailure] = []
        for citation in assertion.citations:
            fact = self._facts.get(citation)
            if fact is None:
                failures.append(VerificationFailure(citation=citation, code="NOT_FOUND", detail="citation does not resolve to a known fact"))
                continue

            test_ids = fact.get("test_ids") or []
            outcome = self._tests_currently_pass(test_ids)
            if outcome is None:
                failures.append(VerificationFailure(citation=citation, code="TRUTH_UNAVAILABLE", detail="the test suite could not be run"))
                continue
            if not outcome:
                failures.append(VerificationFailure(citation=citation, code="EXPIRED", detail=f"mapped tests no longer pass: {test_ids}"))
                continue

            claim_text = fact.get("claim_text", "")
            if not claim_text or claim_text not in assertion.claim:
                failures.append(VerificationFailure(citation=citation, code="UNSUPPORTED", detail="claim does not match the stored fact text"))

        return VerificationResult(
            assertion_query_id=assertion.query_id,
            status="fail" if failures else "pass",
            checked_at=checked_at,
            truth_version=self.truth_version,
            failures=failures,
        )
