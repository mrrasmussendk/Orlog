"""server_tools: the logic behind each MCP tool (ORLOG-SPEC.md §B9), as
plain functions independent of the `mcp` SDK.

Design decision: every tool is `(runtime, ...) -> dict`, testable by direct
call, with no FastMCP machinery involved. server.py just wraps each one in
an `@mcp.tool()`-decorated closure -- keeping the SDK dependency confined
to one thin file, and letting these tests run without spinning up a real
stdio server.

`recall`'s `query` argument is expected in "entity.attribute" form (e.g.
"user:42.email") -- see runtime.py's module docstring for why this
reference server doesn't yet support genuinely free-text recall.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orlog.runtime import Runtime
from orlog.verdandi import OPEN_VALID_TO, build_supersession_chains

_EMPTY_EXCLUDED = {"by_validity": 0, "by_k": 0}
PRIOR_FAILURE_WARNING_THRESHOLD = 3


def _empty_abstention(as_of: datetime, reason: str) -> dict:
    return {
        "verified": False, "claim": None, "citations": [], "as_of": as_of.isoformat(),
        "route": None, "truth_version": None, "abstained": True, "reasons": [reason],
        "excluded": dict(_EMPTY_EXCLUDED),
    }


def remember_tool(
    runtime: Runtime, text: str, *, occurred_at: str | None = None, type: str = "fact",
    actor: str = "user", entity: str | None = None, attribute: str | None = None, value: str | None = None,
) -> dict:
    """spec §B9: {text, occurred_at?, type?="fact", actor?="user"} -> {event_id}."""
    occurred = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(timezone.utc)
    event = runtime.remember(text, occurred_at=occurred, event_type=type, actor=actor, entity=entity, attribute=attribute, value=value)
    return {"event_id": event.id}


def recall_tool(runtime: Runtime, query: str, *, as_of: str = "now") -> dict:
    """spec §B9: {query, as_of?="now"} -> the Answer object (spec §A3)."""
    now = datetime.now(timezone.utc)
    as_of_dt = now if as_of == "now" else datetime.fromisoformat(as_of)

    entity, sep, attribute = query.rpartition(".")
    pipeline = runtime.build_pipeline(now=now)
    if pipeline is None or not sep:
        return _empty_abstention(as_of_dt, "NO_CANDIDATES")

    answer = pipeline.answer(query, entity, attribute, as_of_dt, now=now)
    return answer.model_dump(mode="json")


def recall_history_tool(runtime: Runtime, query: str) -> dict:
    """spec §B9: {query} -> the fact's full chain with validity windows."""
    entity, sep, attribute = query.rpartition(".")
    if not sep:
        return {"query": query, "chain": []}

    fact_events = [
        e for e in runtime.log.read_all()
        if e.type == "fact" and e.payload.get("entity") == entity and e.payload.get("attribute") == attribute
    ]
    if not fact_events:
        return {"query": query, "chain": []}

    view, _ = build_supersession_chains(fact_events, builder="orlog-runtime", built_at=datetime.now(timezone.utc))
    events_by_id = {e.id: e for e in fact_events}
    chain = []
    for event_id in view.chains.get(f"{entity}::{attribute}", []):
        window = view.windows[event_id]
        chain.append({
            "event_id": event_id,
            "value": events_by_id[event_id].payload.get("value"),
            "valid_from": window.valid_from.isoformat(),
            "valid_to": None if window.valid_to == OPEN_VALID_TO else window.valid_to.isoformat(),
        })
    return {"query": query, "chain": chain}


def check_action_tool(runtime: Runtime, action_description: str) -> dict:
    """spec §A5.1 (OPTIONAL memory-as-governance profile): advisory only.

    Best-effort: action descriptors aren't a first-class concept in this
    reference implementation, so prior outcomes are matched by substring
    against the query_id of past outcome events -- a real deployment would
    want a dedicated action-outcome event type instead.
    """
    events = runtime.log.read_all()
    failures = [e for e in events if e.type == "outcome.abstention" and action_description in e.payload.get("query_id", "")]
    successes = [
        e for e in events
        if e.type == "outcome.verification" and e.payload.get("status") == "pass" and action_description in e.payload.get("assertion_query_id", "")
    ]
    warnings = []
    if len(failures) >= PRIOR_FAILURE_WARNING_THRESHOLD:
        warnings.append(f"{len(failures)} prior failed attempts matching {action_description!r}")
    return {"warnings": warnings, "prior_outcomes": {"failures": len(failures), "successes": len(successes)}}


def stats_tool(runtime: Runtime) -> dict:
    """spec §B9: no args -> counters (spec §B8)."""
    return runtime.stats.snapshot()
