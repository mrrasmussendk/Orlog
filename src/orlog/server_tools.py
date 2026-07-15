"""server_tools: the logic behind each MCP tool (ORLOG-SPEC.md §B9), as
plain functions independent of the `mcp` SDK.

Design decision: every tool is `(runtime, ...) -> dict`, testable by direct
call, with no FastMCP machinery involved. server.py just wraps each one in
an `@mcp.tool()`-decorated closure -- keeping the SDK dependency confined
to one thin file, and letting these tests run without spinning up a real
stdio server.

`recall`'s `query` argument has two valid forms: an exact "entity.attribute"
key (e.g. "user:42.email" -- unchanged, fast path, no semantic search
involved) or free text with no "." at all, which falls back to
retrieval_hybrid.resolve_key() to find the right key by meaning before
still serving the answer through the same deterministic Pipeline.answer()
every exact-key lookup uses -- see runtime.py's module docstring and
resolve_key()'s own docstring for why the semantic layer only ever finds a
KEY, never an answer.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orlog.errors import RetrieverUnavailableError
from orlog.pipeline import AnswerCandidate
from orlog.retrieval_hybrid import resolve_key
from orlog.runtime import Runtime
from orlog.verdandi import OPEN_VALID_TO, build_supersession_chains

_EMPTY_EXCLUDED = {"by_validity": 0, "by_k": 0}
PRIOR_FAILURE_WARNING_THRESHOLD = 3


def _parse_datetime(value: str) -> datetime:
    """Parse a caller-supplied ISO-8601 string, treating a missing UTC
    offset as UTC rather than producing a naive datetime.

    Every other datetime in this codebase is timezone-aware (occurred_at
    defaults, validity windows, as_of); comparing a naive one against them
    raises TypeError deep inside pydantic validation (models/event.py's
    bi-temporal check), which isn't even a ValidationError -- it bypasses
    urd.py's SchemaError wrapping entirely and crashes the MCP tool call.
    An MCP client omitting the trailing "Z"/offset is a realistic mistake,
    not an edge case, so this is fixed at the parse boundary rather than
    trusting every caller downstream to only ever see aware datetimes.

    Also normalizes a trailing "Z" to "+00:00": datetime.fromisoformat()
    only accepts "Z" from Python 3.11 (this project targets 3.10, see
    pyproject.toml), and "Z" is the more common form an MCP client (or a
    caller copying occurred_at/as_of straight out of a prior Answer) would
    actually send.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _empty_abstention(as_of: datetime, reason: str) -> dict:
    return {
        "verified": False, "claim": None, "citations": [], "as_of": as_of.isoformat(),
        "route": None, "truth_version": None, "abstained": True, "reasons": [reason],
        "excluded": dict(_EMPTY_EXCLUDED), "candidates": [],
    }


def remember_tool(
    runtime: Runtime, text: str, *, occurred_at: str | None = None, type: str = "fact",
    actor: str = "user", entity: str | None = None, attribute: str | None = None, value: str | None = None,
    entity_detail: str | None = None, register_new_type: bool = False, register_new_attribute: bool = False,
) -> dict:
    """spec §B9: {text, occurred_at?, type?="fact", actor?="user", entity?,
    attribute?, value?, entity_detail?, register_new_type?, register_new_attribute?}
    -> {event_id}. Raises orlog.errors.SchemaError (E_SCHEMA) if schema-on-write
    or the entity_detail-required-on-collision rule rejects the write -- see
    Runtime.remember()'s own docstring/helpers for the rules themselves.
    """
    occurred = _parse_datetime(occurred_at) if occurred_at else datetime.now(timezone.utc)
    event = runtime.remember(
        text, occurred_at=occurred, event_type=type, actor=actor,
        entity=entity, attribute=attribute, value=value, entity_detail=entity_detail,
        register_new_type=register_new_type, register_new_attribute=register_new_attribute,
    )
    return {"event_id": event.id}


def recall_tool(runtime: Runtime, query: str, *, as_of: str = "now") -> dict:
    """spec §B9: {query, as_of?="now"} -> the Answer object (spec §A3)."""
    now = datetime.now(timezone.utc)
    as_of_dt = now if as_of == "now" else _parse_datetime(as_of)

    entity, sep, attribute = query.rpartition(".")
    pipeline = runtime.build_pipeline(now=now)
    if pipeline is None:
        # No Pipeline to instrument here -- there's nothing to project from
        # yet -- but this is still a real recall() call that abstained, and
        # must do everything Pipeline._abstain() does for every other
        # abstention: bump stats AND append the durable outcome.abstention
        # event, or this path's abstentions are invisible to both
        # log-replay and check_action_tool.
        runtime.stats.record_recall()
        runtime.stats.record_abstention(["NO_CANDIDATES"])
        runtime.ledger.record_abstention(query_id=query, reasons=["NO_CANDIDATES"], occurred_at=now)
        return _empty_abstention(as_of_dt, "NO_CANDIDATES")

    if sep:
        # Exact "entity.attribute" key lookup -- unchanged, fast path, no
        # semantic search. A query shaped like a key but naming an entity
        # that doesn't exist stays NO_CANDIDATES rather than falling back:
        # attribute names are often shared across entities ("plan", "city"),
        # so a fuzzy fallback here would risk silently answering about the
        # WRONG entity instead of honestly abstaining.
        answer = pipeline.answer(query, entity, attribute, as_of_dt, now=now)
        return answer.model_dump(mode="json")

    # No "." at all: not a key, it's free text. Search every fact's own
    # remembered text for the one whose MEANING best matches this query --
    # resolve_key() only ever returns a KEY, never an answer. Handing the
    # resolved (entity, attribute) to pipeline.answer() is what gives the
    # deterministic, freshness-checked, cited value.
    try:
        embedder = runtime.embedder
    except RetrieverUnavailableError:
        # A cold/network-bound embedder build didn't finish within its
        # bounded timeout (runtime.py's embedder property) -- abstain
        # honestly rather than hang the whole stdio server on this call.
        runtime.stats.record_recall()
        runtime.stats.record_abstention(["EMBEDDER_UNAVAILABLE"])
        runtime.ledger.record_abstention(query_id=query, reasons=["EMBEDDER_UNAVAILABLE"], occurred_at=now)
        return _empty_abstention(as_of_dt, "EMBEDDER_UNAVAILABLE")

    candidates = resolve_key(query, pipeline.view, pipeline.events_by_id, embedder, k=5)
    strong = [c for c in candidates if c.score >= runtime.config.retrieval.min_confidence]

    if not strong:
        runtime.stats.record_recall()
        runtime.stats.record_abstention(["NO_CANDIDATES"])
        runtime.ledger.record_abstention(query_id=query, reasons=["NO_CANDIDATES"], occurred_at=now)
        return _empty_abstention(as_of_dt, "NO_CANDIDATES")

    margin = runtime.config.retrieval.ambiguity_margin
    tied = [c for c in strong if c.score >= strong[0].score * margin]
    if len(tied) > 1:
        # More than one key is an about-equally-good match -- report every
        # tied candidate with its confidence instead of silently guessing
        # which entity the query meant (the "which Anna" problem).
        runtime.stats.record_recall()
        answer = pipeline._abstain(
            query, ["AMBIGUOUS"], _EMPTY_EXCLUDED, as_of_dt, truth_version=None, now=now,
            candidates=[
                AnswerCandidate(
                    entity=c.entity, attribute=c.attribute, entity_label=c.entity_label,
                    entity_detail=c.entity_detail, excerpt=c.matched_text, score=c.score,
                )
                for c in tied
            ],
        )
        return answer.model_dump(mode="json")

    top = strong[0]
    answer = pipeline.answer(query, top.entity, top.attribute, as_of_dt, now=now)
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


def list_entities_tool(runtime: Runtime, *, prefix: str | None = None, limit: int | None = None) -> dict:
    """spec §B9: {prefix?, limit?} -> {entities: [{entity_label, entity_detail,
    attribute_count}]}. A deterministic scan over the log's own fact events
    grouped by (entity_label, entity_detail) -- zero tokens, no embedding
    call, no derive/verify. The scan/list primitive: lets a caller enumerate
    what it already knows without a semantic search.
    """
    groups: dict[tuple[str, str | None], set[str]] = {}
    for event in runtime.log.read_all():
        if event.type != "fact":
            continue
        entity = event.payload.get("entity")
        attribute = event.payload.get("attribute")
        if entity is None or attribute is None:
            continue
        label = event.payload.get("entity_label") or entity
        detail = event.payload.get("entity_detail")
        groups.setdefault((label, detail), set()).add(attribute)

    entities = [
        {"entity_label": label, "entity_detail": detail, "attribute_count": len(attributes)}
        for (label, detail), attributes in groups.items()
        if prefix is None or label.startswith(prefix)
    ]
    entities.sort(key=lambda e: (e["entity_label"], e["entity_detail"] or ""))
    if limit is not None:
        entities = entities[:limit]
    return {"entities": entities}


def list_attributes_tool(runtime: Runtime, entity: str) -> dict:
    """spec §B9: {entity} -> {entity, attributes: [{attribute, value,
    valid_from, valid_to}]} (current value per attribute). `entity` may be
    the bare label or the exact "label#detail" form. If a bare label matches
    more than one disambiguated entity, returns {ambiguous: true, entity,
    candidates: [...]} instead of guessing -- same abstain-don't-guess
    discipline as recall_tool's AMBIGUOUS path. Same zero-token, no-embedding
    guarantee as list_entities_tool.
    """
    fact_events = [
        e for e in runtime.log.read_all()
        if e.type == "fact" and e.payload.get("entity") is not None and e.payload.get("attribute") is not None
    ]

    exact_matches = [e for e in fact_events if e.payload["entity"] == entity]
    if exact_matches:
        matched_groups: dict[str, list] = {entity: exact_matches}
    else:
        matched_groups = {}
        for e in fact_events:
            label = e.payload.get("entity_label") or e.payload["entity"]
            if label == entity:
                matched_groups.setdefault(e.payload["entity"], []).append(e)

    if not matched_groups:
        return {"entity": entity, "attributes": []}

    if len(matched_groups) > 1:
        candidates = []
        for key, events in matched_groups.items():
            sample = events[0]
            candidates.append({
                "entity": key,
                "entity_label": sample.payload.get("entity_label") or sample.payload["entity"],
                "entity_detail": sample.payload.get("entity_detail"),
                "attribute_count": len({e.payload["attribute"] for e in events}),
            })
        candidates.sort(key=lambda c: c["entity"])
        return {"ambiguous": True, "entity": entity, "candidates": candidates}

    (resolved_key, events), = matched_groups.items()
    view, _ = build_supersession_chains(events, builder="orlog-runtime", built_at=datetime.now(timezone.utc))
    events_by_id = {e.id: e for e in events}
    attributes = []
    for chain_key, event_ids in view.chains.items():
        _, _, attribute = chain_key.partition("::")
        current_id = event_ids[-1]  # last in occurred_at order == the open-ended (current) window
        window = view.windows[current_id]
        attributes.append({
            "attribute": attribute,
            "value": events_by_id[current_id].payload.get("value"),
            "valid_from": window.valid_from.isoformat(),
            "valid_to": None if window.valid_to == OPEN_VALID_TO else window.valid_to.isoformat(),
        })
    attributes.sort(key=lambda a: a["attribute"])
    return {"entity": resolved_key, "attributes": attributes}


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
