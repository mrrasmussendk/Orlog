from benchmarks.vs_mem0 import run_orlog
from benchmarks.vs_mem0.dataset import Fact, Question


def test_ingest_record_shape():
    record = run_orlog._ingest_record("user:aiko", "plan", 12.5, "ok")
    assert record == {
        "phase": "ingest", "entity": "user:aiko", "attribute": "plan", "latency_ms": 12.5,
        "status": "ok", "error": None, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }


def test_ingest_record_carries_error():
    record = run_orlog._ingest_record("user:aiko", "plan", 5.0, "error", error="boom")
    assert record["status"] == "error"
    assert record["error"] == "boom"


def test_query_record_extracts_claim_and_abstained():
    answer = {"claim": "user:aiko.plan is Momentum", "abstained": False}
    record = run_orlog._query_record(
        "query_known", "user:aiko", "plan", 20.0, "ok",
        expected_value="Momentum", answer=answer, tokens={"in": 100, "out": 20},
    )
    assert record["returned_text"] == "user:aiko.plan is Momentum"
    assert record["abstained"] is False
    assert record["tokens"] == 120
    assert record["expected_value"] == "Momentum"


def test_query_record_handles_error_with_no_answer():
    record = run_orlog._query_record("query_unknown", "user:aiko", "shoe_size", 5.0, "error", error="timeout")
    assert record["returned_text"] is None
    assert record["abstained"] is None
    assert record["tokens"] is None
    assert record["status"] == "error"


def test_build_runtime_and_ingest_query_round_trip_offline(tmp_path):
    # No network/API calls: "scripted" deriver + "hashing" embedder mirror
    # this project's own offline-test convention (tests/conftest.py).
    runtime = run_orlog.build_runtime(tmp_path / "ws", deriver_backend="scripted", embedder="hashing")
    fact = Fact(entity="user:test", attribute="plan", value="Pro", statement="Test's plan is Pro.")
    ingest_records = run_orlog.ingest(runtime, [fact])
    assert len(ingest_records) == 1
    assert ingest_records[0]["status"] == "ok"

    question = Question(entity="user:test", attribute="plan", question="What is Test's plan?", expected_value="Pro")
    query_records = run_orlog.run_queries(runtime, [question], "query_known")
    assert len(query_records) == 1
    assert query_records[0]["phase"] == "query_known"
    assert query_records[0]["status"] == "ok"
    assert query_records[0]["expected_value"] == "Pro"
