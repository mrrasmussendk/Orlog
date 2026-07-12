"""Contract: StructuredLogger emits one JSON line per call, and Stats
tallies the counters spec §B8 lists, including p50/p95 latency.
"""

import io
import json

from orlog.observability import Stats, StructuredLogger


def test_structured_logger_writes_one_json_object_per_line():
    stream = io.StringIO()
    logger = StructuredLogger(stream)

    logger.log("append", event_type="fact")
    logger.log("recall", route="fresh")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "append"
    assert first["event_type"] == "fact"
    assert "ts" in first


def test_stats_snapshot_reflects_every_recorded_counter():
    stats = Stats()
    stats.record_append()
    stats.record_append()
    stats.record_recall()
    stats.record_cache_hit()
    stats.record_cache_evict()
    stats.record_rederivation()
    stats.record_verification_pass()
    stats.record_verification_fail(["EXPIRED", "EXPIRED", "UNSUPPORTED"])
    stats.record_abstention(["NO_CANDIDATES"])
    stats.record_tokens({"in": 10, "out": 5})
    stats.record_tokens({"in": 3, "out": 2})

    snapshot = stats.snapshot()

    assert snapshot["appends"] == 2
    assert snapshot["recalls"] == 1
    assert snapshot["cache_hits"] == 1
    assert snapshot["cache_evicts"] == 1
    assert snapshot["rederivations"] == 1
    assert snapshot["verifications"] == {"pass": 1, "fail": {"EXPIRED": 2, "UNSUPPORTED": 1}}
    assert snapshot["abstentions"] == {"by_reason": {"NO_CANDIDATES": 1}}
    assert snapshot["tokens"] == {"in": 13, "out": 7}


def test_percentiles_are_none_with_no_samples_and_computed_with_some():
    stats = Stats()
    assert stats.snapshot()["p50_ms"] is None
    assert stats.snapshot()["p95_ms"] is None

    for ms in [10, 20, 30, 40, 100]:
        stats.record_latency_ms(ms)

    snapshot = stats.snapshot()
    assert snapshot["p50_ms"] == 30
    assert snapshot["p95_ms"] == 100
