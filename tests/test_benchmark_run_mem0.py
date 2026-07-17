from benchmarks.vs_mem0 import run_mem0


def test_ingest_record_shape():
    record = run_mem0._ingest_record("user:aiko", "plan", 15.0, "ok")
    assert record == {
        "phase": "ingest", "entity": "user:aiko", "attribute": "plan", "latency_ms": 15.0,
        "status": "ok", "error": None, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }


def test_query_record_extracts_top_result():
    results = [{"id": "1", "memory": "Aiko's plan is Momentum", "score": 0.82}]
    record = run_mem0._query_record(
        "query_known", "user:aiko", "plan", 30.0, "ok", expected_value="Momentum", results=results,
    )
    assert record["returned_text"] == "Aiko's plan is Momentum"
    assert record["abstained"] is False


def test_query_record_empty_results_is_abstained():
    record = run_mem0._query_record(
        "query_unknown", "user:aiko", "shoe_size", 10.0, "ok", expected_value=None, results=[],
    )
    assert record["returned_text"] is None
    assert record["abstained"] is True


def test_query_record_error_has_no_abstained_verdict():
    record = run_mem0._query_record(
        "query_unknown", "user:aiko", "shoe_size", 10.0, "error", error="timeout",
    )
    assert record["abstained"] is None
    assert record["status"] == "error"
