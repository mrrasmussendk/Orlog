"""Contract: hybrid_retrieve() filters by validity BEFORE ranking, reports
honest exclusions, ranks by the spec §B3 blended score, and tie-breaks
deterministically by event id.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from orlog.retrieval_hybrid import HashingEmbedder, _default_fastembed_cache_dir, hybrid_retrieve, resolve_key
from orlog.urd import EventLog
from orlog.verdandi import build_supersession_chains

CLOCK = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)  # noqa: E731
T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUILT_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _build(tmp_path, make_event, facts):
    log = EventLog(tmp_path / "log.jsonl", clock=CLOCK)
    events = [log.append(make_event(occurred_at=T1, payload=p)) for p in facts]
    view, _ = build_supersession_chains(events, builder="test", built_at=BUILT_AT)
    events_by_id = {e.id: e for e in events}
    return view, events_by_id


def test_a_lexically_matching_fact_outranks_an_unrelated_one(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "value": "enterprise subscription"},
            {"entity": "user:2", "attribute": "color", "value": "the sky is blue today"},
        ],
    )

    result = hybrid_retrieve("enterprise subscription plan", NOW, view, events_by_id, HashingEmbedder())

    assert result.candidates[0].content == "enterprise subscription"


def test_a_fact_missing_value_renders_as_none_like_the_other_l2_backend(tmp_path, make_event):
    # Matches retrieval.py/heimdall.py's str(payload.get("value")) convention
    # -- a fact without "value" content must look the same everywhere it's
    # read, not silently become "" only in this backend.
    view, events_by_id = _build(tmp_path, make_event, [{"entity": "user:1", "attribute": "plan"}])

    result = hybrid_retrieve("plan", NOW, view, events_by_id, HashingEmbedder(), k=10)

    assert result.candidates[0].content == "None"


def test_facts_outside_the_validity_window_are_excluded_and_reported(tmp_path, make_event):
    log = EventLog(tmp_path / "log.jsonl", clock=CLOCK)
    e1 = log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    e2 = log.append(make_event(occurred_at=datetime(2026, 3, 1, tzinfo=timezone.utc), payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    view, _ = build_supersession_chains([e1, e2], builder="test", built_at=BUILT_AT)
    events_by_id = {e1.id: e1, e2.id: e2}

    # as_of T1: only "free" is valid; "pro" hasn't happened yet.
    result = hybrid_retrieve("plan", T1, view, events_by_id, HashingEmbedder(), k=10)

    assert [c.content for c in result.candidates] == ["free"]
    assert result.excluded["by_validity"] == 1


def test_k_limits_results_and_reports_the_excluded_count(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [{"entity": f"user:{i}", "attribute": "plan", "value": f"plan value {i}"} for i in range(5)],
    )

    result = hybrid_retrieve("plan value", NOW, view, events_by_id, HashingEmbedder(), k=2)

    assert len(result.candidates) == 2
    assert result.excluded["by_k"] == 3


def test_ties_break_deterministically_by_event_id(tmp_path, make_event):
    # Two facts with content that scores identically (empty query -> every
    # score is 0.0) must still come back in a stable, deterministic order.
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "value": "same"},
            {"entity": "user:2", "attribute": "plan", "value": "same"},
        ],
    )

    result_a = hybrid_retrieve("", NOW, view, events_by_id, HashingEmbedder(), k=10)
    result_b = hybrid_retrieve("", NOW, view, events_by_id, HashingEmbedder(), k=10)

    ids_a = [c.event_id for c in result_a.candidates]
    ids_b = [c.event_id for c in result_b.candidates]
    assert ids_a == ids_b == sorted(ids_a)


def test_importance_weighting_can_change_the_ranking(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "value": "same content here"},
            {"entity": "user:2", "attribute": "plan", "value": "same content here"},
        ],
    )
    low_id = list(view.chains["user:1::plan"])[0]
    high_id = list(view.chains["user:2::plan"])[0]

    result = hybrid_retrieve(
        "same content here", NOW, view, events_by_id, HashingEmbedder(),
        importance={low_id: 0.1, high_id: 5.0},
    )

    assert result.candidates[0].event_id == high_id


def test_no_candidates_in_window_returns_empty_with_honest_exclusions(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [{"entity": "user:1", "attribute": "plan", "value": "free"}],
    )

    result = hybrid_retrieve("plan", datetime(2020, 1, 1, tzinfo=timezone.utc), view, events_by_id, HashingEmbedder())

    assert result.candidates == []
    assert result.excluded["by_validity"] == 1


# -- resolve_key(): finds a KEY from free text, never an answer --


def test_resolve_key_finds_the_right_key_from_its_own_remembered_text(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "anna", "attribute": "city", "value": "Seattle", "text": "Anna just moved to Seattle last week"},
            {"entity": "bob", "attribute": "city", "value": "Denver", "text": "Bob has lived in Denver for years"},
        ],
    )

    results = resolve_key("where does anna live now", view, events_by_id, HashingEmbedder())

    assert results[0].entity == "anna"
    assert results[0].attribute == "city"


def test_resolve_key_prefers_text_over_the_terse_extracted_value(tmp_path, make_event):
    # The bare `value` ("Seattle") shares almost no tokens with a natural
    # phrasing of the query -- it's the remembered `text` that should
    # actually carry the match.
    view, events_by_id = _build(
        tmp_path, make_event,
        [{"entity": "anna", "attribute": "city", "value": "Seattle", "text": "Anna just moved to Seattle last week"}],
    )

    results = resolve_key("has anna moved recently", view, events_by_id, HashingEmbedder())

    assert results[0].matched_text == "Anna just moved to Seattle last week"


def test_resolve_key_counts_an_old_superseded_mention_as_evidence(tmp_path, make_event):
    # An out-of-window (superseded) event is still valid evidence the query
    # is ABOUT this key -- freshness is the caller's job afterwards, not
    # resolve_key()'s.
    log = EventLog(tmp_path / "log.jsonl", clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    e1 = log.append(make_event(
        occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        payload={"entity": "anna", "attribute": "city", "value": "Boston", "text": "Anna lives in Boston"},
    ))
    e2 = log.append(make_event(
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        payload={"entity": "anna", "attribute": "city", "value": "Seattle", "text": "Anna moved to Seattle"},
    ))
    view, _ = build_supersession_chains([e1, e2], builder="test", built_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    events_by_id = {e1.id: e1, e2.id: e2}

    results = resolve_key("anna lives in boston", view, events_by_id, HashingEmbedder())

    assert len(results) == 1
    assert results[0].entity == "anna"
    assert results[0].matched_text == "Anna lives in Boston"


def test_resolve_key_falls_back_to_value_when_text_is_missing(tmp_path, make_event):
    view, events_by_id = _build(tmp_path, make_event, [{"entity": "user:1", "attribute": "plan", "value": "pro"}])

    results = resolve_key("pro", view, events_by_id, HashingEmbedder())

    assert results[0].matched_text == "pro"


def test_resolve_key_ranks_and_ties_break_deterministically_by_key(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "text": "same"},
            {"entity": "user:2", "attribute": "plan", "text": "same"},
        ],
    )

    results_a = resolve_key("", view, events_by_id, HashingEmbedder())
    results_b = resolve_key("", view, events_by_id, HashingEmbedder())

    keys_a = [(c.entity, c.attribute) for c in results_a]
    keys_b = [(c.entity, c.attribute) for c in results_b]
    assert keys_a == keys_b == sorted(keys_a)


def test_resolve_key_returns_empty_for_an_empty_view(tmp_path, make_event):
    log = EventLog(tmp_path / "log.jsonl", clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    e1 = log.append(make_event(occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc), payload={"note": "no entity here"}))
    view, _ = build_supersession_chains([e1], builder="test", built_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    events_by_id = {e1.id: e1}

    assert resolve_key("anything", view, events_by_id, HashingEmbedder()) == []


def test_default_fastembed_cache_dir_is_not_under_the_system_temp_folder():
    # A temp-folder cache can be wiped by a reboot or a cleanup tool at any
    # time, silently turning a warm-cache embedder build back into a cold,
    # network-bound download -- the exact failure mode that caused a
    # multi-minute recall() hang. The cache must be somewhere persistent.
    cache_dir = _default_fastembed_cache_dir()

    system_temp = Path(tempfile.gettempdir()).resolve()
    assert system_temp not in cache_dir.resolve().parents
    assert cache_dir.resolve() != system_temp


def test_resolve_key_surfaces_entity_label_and_detail_when_present(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [{"entity": "anna#coworker-at-acme", "attribute": "city", "value": "Seattle", "text": "Anna (coworker) moved to Seattle"}],
    )
    # Simulate what Runtime.remember() stores when entity_detail is used.
    only_event_id = next(iter(events_by_id))
    events_by_id[only_event_id] = events_by_id[only_event_id].model_copy(
        update={"payload": {**events_by_id[only_event_id].payload, "entity_label": "anna", "entity_detail": "coworker-at-acme"}}
    )

    results = resolve_key("anna moved to seattle", view, events_by_id, HashingEmbedder())

    assert results[0].entity_label == "anna"
    assert results[0].entity_detail == "coworker-at-acme"
