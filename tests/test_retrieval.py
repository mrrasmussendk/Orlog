"""Contract: retrieve_current_fact() (L2, ORLOG-SPEC.md §3.4/§4.3) populates
Candidate.excerpt from the fact's remembered evidence -- additive context
for a real LLM deriver's prompt (huginn_llm.py's _render_facts), verified
against real Anthropic and OpenAI models to be the difference between a
confident answer and a spurious INSUFFICIENT on orlog's own bare `content`
value alone. See retrieval.py's Candidate.excerpt docstring.
"""

from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.retrieval import retrieve_current_fact
from orlog.runtime import Runtime
from orlog.server_tools import remember_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace



@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="myproject"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "myproject"), config)
    yield rt
    rt.close()


def test_excerpt_falls_back_to_remembered_text_when_no_evidence_span(runtime):
    remember_tool(runtime, "Alice's plan is Pro.", entity="user:alice", attribute="plan", value="Pro")

    now = datetime.now(timezone.utc)
    pipeline = runtime.build_pipeline(now=now)
    result = retrieve_current_fact("user:alice", "plan", now, pipeline.view, pipeline.events_by_id)

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.content == "Pro"  # unchanged -- ScriptedDeriver still keys off this directly
    assert candidate.excerpt == "Alice's plan is Pro."


def test_excerpt_prefers_explicit_evidence_span_when_given(runtime):
    remember_tool(
        runtime, "According to the support ticket, Alice's plan is Pro as of today.",
        entity="user:alice", attribute="plan", value="Pro", evidence_span="Alice's plan is Pro",
    )

    now = datetime.now(timezone.utc)
    pipeline = runtime.build_pipeline(now=now)
    result = retrieve_current_fact("user:alice", "plan", now, pipeline.view, pipeline.events_by_id)

    assert result.candidates[0].excerpt == "Alice's plan is Pro"
