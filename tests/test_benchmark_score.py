import json

from benchmarks.vs_mem0 import score


def _record(phase, entity="user:aiko", attribute="plan", latency_ms=100.0, status="ok", **kw):
    base = {
        "phase": phase, "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": None, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }
    base.update(kw)
    return base


def test_load_raw_results_round_trips(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps([_record("ingest")]), encoding="utf-8")
    assert score.load_raw_results(path) == [_record("ingest")]


def test_compute_latency_stats_filters_phase_and_status():
    records = [
        _record("ingest", latency_ms=10.0),
        _record("ingest", latency_ms=30.0),
        _record("ingest", latency_ms=999.0, status="error"),
        _record("query_known", latency_ms=50.0),
    ]
    stats = score.compute_latency_stats(records, "ingest")
    assert stats["n"] == 2
    assert stats["mean"] == 20.0
    assert stats["errors"] == 1


def test_compute_accuracy_overall_and_post_update():
    records = [
        _record("query_known", entity="user:aiko", attribute="plan",
                 returned_text="Aiko's plan is Momentum", expected_value="Momentum"),
        _record("query_known", entity="user:bilal", attribute="city",
                 returned_text="Bilal lives in Nairobi", expected_value="Nairobi"),
        _record("query_known", entity="user:chen", attribute="plan",
                 returned_text="wrong answer", expected_value="Vertex"),
    ]
    updated_pairs = {("user:aiko", "plan"), ("user:chen", "plan")}
    result = score.compute_accuracy(records, updated_pairs)
    assert result["overall_n"] == 3
    assert result["overall"] == 2 / 3
    assert result["post_update_n"] == 2
    assert result["post_update"] == 1 / 2


def test_compute_accuracy_empty_returns_none():
    assert score.compute_accuracy([], set()) == {
        "overall": None, "overall_n": 0, "post_update": None, "post_update_n": 0,
    }


def test_compute_abstention_counts_correct_declines():
    records = [
        _record("query_unknown", abstained=True),
        _record("query_unknown", abstained=False),
        _record("query_unknown", abstained=True),
    ]
    assert score.compute_abstention(records) == {"correct_rate": 2 / 3, "n": 3}


def test_compute_tokens_skips_missing():
    records = [
        _record("query_known", tokens=100),
        _record("query_known", tokens=None),
        _record("query_known", tokens=50),
    ]
    assert score.compute_tokens(records) == {"total": 150, "n_with_tokens": 2}


def test_compute_metrics_wires_everything():
    records = [
        _record("ingest", latency_ms=10.0),
        _record("query_known", returned_text="Momentum", expected_value="Momentum",
                 entity="user:aiko", attribute="plan"),
        _record("query_unknown", abstained=True),
    ]
    metrics = score.compute_metrics("orlog", records, {("user:aiko", "plan")})
    assert metrics["system"] == "orlog"
    assert metrics["ingest"]["n"] == 1
    assert metrics["accuracy"]["overall"] == 1.0
    assert metrics["abstention"]["correct_rate"] == 1.0


def test_main_handles_missing_file(tmp_path):
    out = tmp_path / "results.json"
    combined = score.main(tmp_path / "missing_orlog.json", tmp_path / "missing_mem0.json", out)
    assert combined["orlog"]["accuracy"]["overall"] is None
    assert combined["mem0"]["accuracy"]["overall"] is None
    assert out.exists()


def test_aggregate_across_runs_reports_median_min_max():
    run_a = score.compute_metrics("orlog", [
        _record("ingest", latency_ms=10.0),
        _record("query_known", returned_text="Momentum", expected_value="Momentum"),
    ], set())
    run_b = score.compute_metrics("orlog", [
        _record("ingest", latency_ms=30.0),
        _record("query_known", returned_text="wrong", expected_value="Momentum"),
    ], set())
    combined = score.aggregate_across_runs([run_a, run_b])
    assert combined["system"] == "orlog"
    assert combined["ingest"]["mean"] == {"median": 20.0, "min": 10.0, "max": 30.0, "n": 2}
    assert combined["ingest"]["n"] == 1  # sample size carried over, not aggregated
    assert combined["accuracy"]["overall"] == {"median": 0.5, "min": 0.0, "max": 1.0, "n": 2}
    assert combined["accuracy"]["overall_n"] == 1


def test_main_multi_combines_n_raw_files(tmp_path):
    for i, latency in enumerate([10.0, 20.0, 30.0]):
        path = tmp_path / f"orlog_run{i}.json"
        path.write_text(json.dumps([_record("ingest", latency_ms=latency)]), encoding="utf-8")
    mem0_path = tmp_path / "mem0_run0.json"
    mem0_path.write_text(json.dumps([_record("ingest", latency_ms=5.0)]), encoding="utf-8")

    out = tmp_path / "results.json"
    combined = score.main_multi(
        [tmp_path / f"orlog_run{i}.json" for i in range(3)],
        [mem0_path, mem0_path, mem0_path],
        out,
    )
    assert combined["n_runs"] == 3
    assert combined["orlog"]["ingest"]["mean"] == {"median": 20.0, "min": 10.0, "max": 30.0, "n": 3}
    assert out.exists()
