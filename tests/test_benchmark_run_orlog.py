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


def test_main_completes_without_resource_leak(tmp_path, monkeypatch):
    """Regression test for runtime.close() bug: verifies that main() completes
    without PermissionError when closing the TemporaryDirectory (Windows sqlite3
    cleanup issue), and that the output JSON is valid.
    """
    import json

    # Create minimal test data to keep test fast and offline
    test_initial_facts = [Fact(entity="user:test", attribute="plan", value="Pro", statement="Test plan is Pro.")]
    test_update_facts = [Fact(entity="user:test", attribute="status", value="active", statement="Test status is active.")]
    test_known_questions = [Question(entity="user:test", attribute="plan", question="What is test plan?", expected_value="Pro")]
    test_unknown_questions = [Question(entity="user:test", attribute="shoe_size", question="What is test shoe size?", expected_value="unknown")]

    # Monkeypatch dataset constants to use tiny test data
    monkeypatch.setattr(run_orlog, "INITIAL_FACTS", test_initial_facts)
    monkeypatch.setattr(run_orlog, "UPDATE_FACTS", test_update_facts)
    monkeypatch.setattr(run_orlog, "KNOWN_QUESTIONS", test_known_questions)
    monkeypatch.setattr(run_orlog, "UNKNOWN_QUESTIONS", test_unknown_questions)

    # Monkeypatch build_runtime to use offline backends (scripted deriver + hashing embedder)
    original_build_runtime = run_orlog.build_runtime

    def mock_build_runtime(workspace_dir, **kwargs):
        return original_build_runtime(workspace_dir, deriver_backend="scripted", embedder="hashing")

    monkeypatch.setattr(run_orlog, "build_runtime", mock_build_runtime)

    # Call main() and verify no exception (especially PermissionError on cleanup)
    out_path = tmp_path / "results.json"
    records = run_orlog.main(out_path)

    # Verify output file exists and is valid JSON with expected structure
    assert out_path.exists(), "Output JSON file was not created"
    with open(out_path) as f:
        persisted_records = json.load(f)

    # Verify records list is not empty and has expected structure
    assert len(persisted_records) > 0, "Records list is empty"
    assert len(records) == len(persisted_records), "In-memory records don't match persisted records"

    # Verify each record has the expected shape
    for record in persisted_records:
        assert "phase" in record
        assert "entity" in record
        assert "attribute" in record
        assert "latency_ms" in record
        assert "status" in record
