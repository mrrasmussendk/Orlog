"""remember()'s core write-time contract -- the tests that fail silently if
this is ever wrong.

These exist because a claim of "remember() rejects incomplete writes" was
made without live evidence: the actual deployed server had loaded its code
before the fix landed (Python does not hot-reload a running process), so it
kept silently accepting incomplete writes for over an hour after the source
was already correct on disk. The bug wasn't in the code -- it was in
reporting "done" without observing the running system. See ORLOG-SPEC.md
§B9 (remember requires entity, attribute, AND value together) and
runtime.py's Runtime.remember() docstring for the rule itself.

Ordered deliberately: the partial-write case (test 2) goes first, because
it is the most dangerous failure mode -- bare text at least LOOKS
unstructured, but a partial write (a plausible entity + attribute, no
value) returns a success code and a key that resolves to nothing. It looks
more correct than the bare-text case while being equally void.
"""

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.errors import SchemaError
from orlog.runtime import Runtime
from orlog.server_tools import recall_tool, remember_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    config = OrlogConfig(workspace=WorkspaceConfig(name="contract"), retrieval=RetrievalConfig(embedder="hashing"))
    rt = Runtime(Workspace(tmp_path / "contract"), config)
    yield rt
    rt.close()


def test_2_partial_write_entity_and_attribute_with_no_value_is_rejected(runtime):
    # The most dangerous case: a plausible key, a success code if this were
    # ever silently accepted, and a key that resolves to nothing on recall.
    with pytest.raises(SchemaError) as exc_info:
        remember_tool(runtime, entity="person:mikkel", attribute="office_preference", text="Mikkel prefers a private office.")
    assert "entity" in str(exc_info.value)
    assert "attribute" in str(exc_info.value)
    assert "value" in str(exc_info.value)
    # And the key it would have implied must genuinely not exist -- not a
    # partial/ghost fact sitting in the log despite the raised error.
    answer = recall_tool(runtime, "person:mikkel.office_preference")
    assert answer["abstained"] is True
    assert answer["reasons"] == ["NO_CANDIDATES"]


def test_1_bare_text_write_is_rejected(runtime):
    with pytest.raises(SchemaError):
        remember_tool(runtime, text="Mikkel hates open-plan offices.")


def test_3_a_full_write_round_trips_through_recall_by_the_key_it_implies(runtime):
    remember_tool(runtime, entity="person:mikkel", attribute="office_preference", value="dislikes open-plan", text="Mikkel dislikes open-plan offices.")

    answer = recall_tool(runtime, "person:mikkel.office_preference")

    assert answer["verified"] is True
    assert answer["claim"] == "person:mikkel.office_preference = dislikes open-plan"
