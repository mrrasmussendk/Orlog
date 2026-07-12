"""Contract: L3 assertions without citations are rejected."""

import pytest
from pydantic import ValidationError


def test_assertion_without_citations_is_rejected(make_assertion):
    with pytest.raises(ValidationError):
        make_assertion(citations=[])


def test_assertion_with_a_citation_is_accepted(make_assertion):
    assertion = make_assertion()
    assert len(assertion.citations) == 1
    assert assertion.citations[0].citation_id == "ev-1"
