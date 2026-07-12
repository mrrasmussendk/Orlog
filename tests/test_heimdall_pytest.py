"""Contract: the pytest verifier (ORLOG-SPEC.md §B11) checks V1/V2 like
heimdall, but V3 means "the mapped tests currently pass" (a real subprocess
run against a scratch test file) and V4 means "the claim text matches the
stored fact text".
"""

from datetime import datetime, timezone

import pytest

from orlog.heimdall_pytest import PytestVerifier
from orlog.huginn import Assertion

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sample_project(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("pytest_verifier_target")
    (project_dir / "test_sample.py").write_text(
        "def test_passes():\n    assert True\n\n\ndef test_fails():\n    assert False\n",
        encoding="utf-8",
    )
    return project_dir


@pytest.fixture(scope="module")
def verifier(sample_project):
    facts = {
        "ev-pass": {"claim_text": "the answer is 42", "test_ids": ["test_sample.py::test_passes"]},
        "ev-fail": {"claim_text": "the answer is 7", "test_ids": ["test_sample.py::test_fails"]},
    }
    return PytestVerifier(facts, cwd=sample_project, timeout_s=30)


def _assertion(claim: str, citations: list[str]) -> Assertion:
    return Assertion(query_id="q1", claim=claim, citations=citations, derived_by="test", derived_at=NOW, route="fresh")


def test_v1_uncited_fails(verifier):
    result = verifier.verify(_assertion("doesn't matter", []), NOW, checked_at=NOW)
    assert result.status == "fail"
    assert result.failures[0].code == "UNCITED"


def test_v2_not_found_for_an_unknown_citation(verifier):
    result = verifier.verify(_assertion("doesn't matter", ["nonexistent"]), NOW, checked_at=NOW)
    assert result.status == "fail"
    assert result.failures[0].code == "NOT_FOUND"


def test_v3_passes_when_the_mapped_test_currently_passes(verifier):
    result = verifier.verify(_assertion("the answer is 42", ["ev-pass"]), NOW, checked_at=NOW)
    assert result.status == "pass"


def test_v3_fails_as_expired_when_the_mapped_test_currently_fails(verifier):
    result = verifier.verify(_assertion("the answer is 7", ["ev-fail"]), NOW, checked_at=NOW)
    assert result.status == "fail"
    assert result.failures[0].code == "EXPIRED"


def test_v4_fails_as_unsupported_when_claim_text_does_not_match(verifier):
    result = verifier.verify(_assertion("a completely different claim", ["ev-pass"]), NOW, checked_at=NOW)
    assert result.status == "fail"
    assert result.failures[0].code == "UNSUPPORTED"


def test_unreachable_pytest_command_is_truth_unavailable_not_a_normal_fail(sample_project):
    verifier = PytestVerifier(
        {"ev-pass": {"claim_text": "x", "test_ids": ["test_sample.py::test_passes"]}},
        cwd=sample_project,
        test_command="this-command-does-not-exist-anywhere",
        timeout_s=5,
    )
    result = verifier.verify(_assertion("x", ["ev-pass"]), NOW, checked_at=NOW)
    assert result.status == "fail"
    assert result.failures[0].code == "TRUTH_UNAVAILABLE"
    assert verifier.truth_version == "unavailable"
