from benchmarks.vs_mem0 import grading


def test_is_correct_matches_case_insensitive_substring():
    assert grading.is_correct("user:aiko.plan is Momentum", "Momentum") is True
    assert grading.is_correct("Aiko's plan is now momentum-based", "Momentum") is True
    assert grading.is_correct("Aiko's plan is Horizon", "Momentum") is False


def test_is_correct_handles_missing_text():
    assert grading.is_correct(None, "Momentum") is False
    assert grading.is_correct("", "Momentum") is False


def test_percentiles_empty():
    assert grading.percentiles([]) == {"mean": None, "p50": None, "p95": None, "n": 0}


def test_percentiles_known_values():
    result = grading.percentiles([10.0, 20.0, 30.0, 40.0, 50.0])
    assert result["n"] == 5
    assert result["mean"] == 30.0
    assert result["p50"] == 30.0
    assert result["p95"] == 50.0


def test_percentiles_single_value():
    assert grading.percentiles([42.0]) == {"mean": 42.0, "p50": 42.0, "p95": 42.0, "n": 1}


def test_summarize_runs_empty():
    assert grading.summarize_runs([]) == {"median": None, "min": None, "max": None, "n": 0}


def test_summarize_runs_drops_none():
    assert grading.summarize_runs([10.0, None, 30.0]) == {"median": 20.0, "min": 10.0, "max": 30.0, "n": 2}


def test_summarize_runs_odd_count_median():
    result = grading.summarize_runs([30.0, 10.0, 20.0])
    assert result == {"median": 20.0, "min": 10.0, "max": 30.0, "n": 3}


def test_summarize_runs_even_count_median_averages_middle_two():
    result = grading.summarize_runs([10.0, 20.0, 30.0, 40.0])
    assert result == {"median": 25.0, "min": 10.0, "max": 40.0, "n": 4}
