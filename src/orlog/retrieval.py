"""retrieval: L2 -- as-of-aware candidate search (ORLOG-SPEC.md §3.4/§4.3).

Design decision: not one of this milestone's numbered deliverables, but
pipeline.py (step 5) needs *some* L2 backend to produce Candidates for
huginn. This is the minimal deterministic backend that proves the L2 MUST
clauses -- filter by validity window at as_of, report exclusions (the
no-silent-caps rule) -- not a production retrieval engine. Per spec §1.2,
retrieval quality is explicitly outside the trust guarantee; only honesty
about validity and exclusions is normative here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from orlog.models.event import Event
from orlog.verdandi import SupersessionChainsView


class Candidate(BaseModel):
    """spec/ORLOG-SPEC.md §3.4."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    content: str
    valid_from: datetime
    valid_to: datetime
    score: float
    importance: float = 1.0
    # spec §3.4's optional candidate.excerpt: the remembered evidence_span,
    # or the full remembered text if no explicit span was given. `content`
    # (the bare stored value, e.g. "Pro") is unchanged -- ScriptedDeriver's
    # extraction (huginn.py) still keys off it directly. `excerpt` is
    # additive context for a real LLM deriver's prompt (huginn_llm.py's
    # _render_facts): a bare value with no attribute label or grounding
    # sentence tying it to the question was verified (against real
    # Anthropic and OpenAI models) to produce a spurious INSUFFICIENT even
    # on orlog's own README example -- the excerpt is what the model needs
    # to answer confidently instead.
    excerpt: str | None = None


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[Candidate]
    excluded: dict[str, int]  # {"by_validity": n, "by_k": n} -- spec §4.3


def retrieve_current_fact(
    entity: str,
    attribute: str,
    as_of: datetime,
    view: SupersessionChainsView,
    events_by_id: dict[str, Event],
    *,
    k: int = 1,
) -> RetrievalResult:
    """Return the candidate(s) for (entity, attribute) whose validity window
    contains `as_of`, most-recent first, plus honest exclusion counts.

    By construction, verdandi's windows partition time per chain, so at most
    one candidate should ever be "in window" for a given as_of -- but this
    function doesn't assume that; it just filters and ranks.
    """
    key = f"{entity}::{attribute}"
    chain = view.chains.get(key, [])

    in_window: list[str] = []
    by_validity_excluded = 0
    for event_id in chain:
        window = view.windows[event_id]
        if window.valid_from <= as_of < window.valid_to:
            in_window.append(event_id)
        else:
            by_validity_excluded += 1

    # Most-recent-first, in case more than one window somehow contains as_of.
    in_window.sort(key=lambda eid: view.windows[eid].valid_from, reverse=True)

    candidates = [
        Candidate(
            event_id=event_id,
            content=str(events_by_id[event_id].payload.get("value")),
            valid_from=view.windows[event_id].valid_from,
            valid_to=view.windows[event_id].valid_to,
            score=1.0,
            importance=1.0,
            excerpt=events_by_id[event_id].payload.get("evidence_span") or events_by_id[event_id].payload.get("text"),
        )
        for event_id in in_window[:k]
    ]
    by_k_excluded = max(0, len(in_window) - k)

    return RetrievalResult(candidates=candidates, excluded={"by_validity": by_validity_excluded, "by_k": by_k_excluded})
