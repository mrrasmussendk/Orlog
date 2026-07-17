# Orlog vs Mem0 Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible benchmark comparing orlog and Mem0 on speed, accuracy, and abstention/false-positive behavior, and publish the results as an interactive chart dashboard.

**Architecture:** A new `benchmarks/vs_mem0/` package with a deterministic synthetic dataset, two runner scripts that drive each system through its real Python API (real OpenAI calls), a scoring module that turns raw per-item JSON into aggregated metrics, and a dashboard built from the scored output.

**Tech Stack:** Python 3.10+, orlog's own `src/orlog` package (already installed editable), `mem0ai` (already `pip install`ed into `.venv`), `pytest`, no other new runtime dependencies.

## Global Constraints

- Zero changes to `src/orlog/` — see `docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md`'s Non-goals.
- Dataset is deterministic, hand-authored, no randomness — same content every run.
- Both systems use OpenAI as their LLM backend, model `gpt-5-mini`, since `ANTHROPIC_API_KEY` is not set in this environment (`OPENAI_API_KEY` is).
- Grading is deterministic case-insensitive substring match — no LLM judge.
- Mem0 uses its own shipped defaults throughout (`threshold=0.1`, `infer=True`, `version="v1.1"`) — never tuned favorably or unfavorably.
- All entities share a single Mem0 `user_id` (`"vs_mem0_bench"`), mirroring orlog's single global event log — see the spec's "System wiring" section for why per-entity scoping would be unfair.
- Every per-item API call (write or query) is wrapped in try/except; a single item failing must not abort the run.

---

### Task 1: Package skeleton, pytest import path, mem0ai dependency

**Files:**
- Create: `benchmarks/__init__.py`
- Create: `benchmarks/vs_mem0/__init__.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `benchmarks.vs_mem0` as an importable package from any test under `tests/` or `benchmarks/vs_mem0/`, once `pythonpath = ["."]` is in pytest's ini options.

- [ ] **Step 1: Create the package directories**

`benchmarks/__init__.py`:
```python
"""benchmarks: standalone comparison harnesses, not part of the orlog
distribution (see pyproject.toml's [tool.setuptools.packages.find] --
only src/ is packaged).
"""
```

`benchmarks/vs_mem0/__init__.py`:
```python
"""vs_mem0: benchmark comparing orlog and Mem0 on speed, accuracy, and
abstention/false-positive behavior. See README.md in this directory for
methodology, and docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md
for the full design rationale.
"""
```

- [ ] **Step 2: Add `pythonpath` to pytest config and the `mem0ai` optional dependency**

In `pyproject.toml`, change:
```toml
[project.optional-dependencies]
anthropic = ["anthropic>=0.40"]
openai = ["openai>=1.0"]
dev = [
    "pytest>=8",
]
```
to:
```toml
[project.optional-dependencies]
anthropic = ["anthropic>=0.40"]
openai = ["openai>=1.0"]
dev = [
    "pytest>=8",
]
benchmark = [
    "mem0ai>=2.0",
]
```

And change:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = ["-m", "not integration"]
markers = [
    "integration: makes real Anthropic API calls; requires ANTHROPIC_API_KEY, skipped otherwise",
]
```
to:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = ["-m", "not integration"]
pythonpath = ["."]
markers = [
    "integration: makes real Anthropic API calls; requires ANTHROPIC_API_KEY, skipped otherwise",
]
```

- [ ] **Step 3: Verify pytest still collects the existing suite cleanly**

Run: `.venv/Scripts/python.exe -m pytest --collect-only -q`
Expected: same collection count as before this change (no errors, no new tests yet since `benchmarks/` has none).

- [ ] **Step 4: Commit**

```bash
git add benchmarks/__init__.py benchmarks/vs_mem0/__init__.py pyproject.toml
git commit -m "chore: scaffold benchmarks/vs_mem0 package for orlog-vs-mem0 benchmark"
```

---

### Task 2: Dataset

**Files:**
- Create: `benchmarks/vs_mem0/dataset.py`
- Test: `tests/test_benchmark_dataset.py`

**Interfaces:**
- Produces:
  - `Fact(entity: str, attribute: str, value: str, statement: str)` — frozen dataclass
  - `Question(entity: str, attribute: str, question: str, expected_value: str | None)` — frozen dataclass
  - `ENTITIES: list[str]` (10 items, `"user:<name>"`)
  - `INITIAL_FACTS: list[Fact]` (40 items)
  - `UPDATE_FACTS: list[Fact]` (10 items)
  - `KNOWN_QUESTIONS: list[Question]` (40 items)
  - `UNKNOWN_QUESTIONS: list[Question]` (15 items)
  - `current_value(entity: str, attribute: str) -> str` (raises `KeyError` if never stored)
  - `updated_pairs() -> set[tuple[str, str]]`

- [ ] **Step 1: Write the failing tests**

`tests/test_benchmark_dataset.py`:
```python
import pytest

from benchmarks.vs_mem0 import dataset


def test_entities_and_initial_facts_shape():
    assert len(dataset.ENTITIES) == 10
    assert len(dataset.INITIAL_FACTS) == 40
    per_entity = {}
    for fact in dataset.INITIAL_FACTS:
        per_entity.setdefault(fact.entity, set()).add(fact.attribute)
    assert set(per_entity.keys()) == set(dataset.ENTITIES)
    for attrs in per_entity.values():
        assert attrs == {"plan", "city", "job_title", "native_language"}


def test_update_facts_reference_existing_pairs_with_new_values():
    assert len(dataset.UPDATE_FACTS) == 10
    initial_by_pair = {(f.entity, f.attribute): f.value for f in dataset.INITIAL_FACTS}
    for fact in dataset.UPDATE_FACTS:
        assert (fact.entity, fact.attribute) in initial_by_pair
        assert fact.value != initial_by_pair[(fact.entity, fact.attribute)]


def test_all_attribute_values_are_pairwise_substring_free():
    for attr in ("plan", "city", "job_title", "native_language"):
        values = [f.value for f in dataset.INITIAL_FACTS if f.attribute == attr]
        values += [f.value for f in dataset.UPDATE_FACTS if f.attribute == attr]
        for a in values:
            for b in values:
                if a != b:
                    assert a.lower() not in b.lower(), (a, b, attr)


def test_current_value_reflects_updates():
    assert dataset.current_value("user:aiko", "plan") == "Momentum"
    assert dataset.current_value("user:farah", "city") == "Porto"
    assert dataset.current_value("user:aiko", "city") == "Lisbon"


def test_current_value_raises_for_unknown_pair():
    with pytest.raises(KeyError):
        dataset.current_value("user:aiko", "shoe_size")


def test_updated_pairs_matches_update_facts():
    pairs = dataset.updated_pairs()
    assert len(pairs) == 10
    assert pairs == {(f.entity, f.attribute) for f in dataset.UPDATE_FACTS}


def test_known_questions_use_current_values():
    assert len(dataset.KNOWN_QUESTIONS) == 40
    for q in dataset.KNOWN_QUESTIONS:
        assert q.expected_value == dataset.current_value(q.entity, q.attribute)


def test_unknown_questions_were_never_stored():
    assert len(dataset.UNKNOWN_QUESTIONS) == 15
    initial_pairs = {(f.entity, f.attribute) for f in dataset.INITIAL_FACTS}
    for q in dataset.UNKNOWN_QUESTIONS:
        assert (q.entity, q.attribute) not in initial_pairs
        assert q.expected_value is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_dataset.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.vs_mem0.dataset'`

- [ ] **Step 3: Implement `dataset.py`**

`benchmarks/vs_mem0/dataset.py`:
```python
"""dataset.py: deterministic synthetic fixture for the Orlog vs Mem0
benchmark -- 10 fictional entities x 4 attributes, a set of later-arriving
updates (tests supersession/smart-update), and a set of never-stored
"unknown" questions (tests abstention vs. false-positive behavior).

All values within an attribute family (including its update values) are
globally unique strings with no substring relationships to each other, so
grading.is_correct()'s substring match can't accidentally credit the wrong
entity's value.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    entity: str
    attribute: str
    value: str
    statement: str


@dataclass(frozen=True)
class Question:
    entity: str
    attribute: str
    question: str
    expected_value: str | None


DISPLAY_NAMES = [
    "Aiko", "Bilal", "Chen", "Dmitri", "Elena",
    "Farah", "Gustav", "Hana", "Ivo", "Jamila",
]
ENTITIES = [f"user:{name.lower()}" for name in DISPLAY_NAMES]

PLANS = ["Starter", "Basic", "Plus", "Pro", "Team", "Business", "Growth", "Scale", "Premier", "Enterprise"]
CITIES = ["Lisbon", "Nairobi", "Osaka", "Quito", "Reykjavik", "Sofia", "Tallinn", "Cusco", "Vilnius", "Wellington"]
JOB_TITLES = [
    "Data Analyst", "Product Designer", "Backend Engineer", "Marketing Lead", "Research Scientist",
    "Operations Manager", "UX Researcher", "DevOps Engineer", "Sales Director", "Technical Writer",
]
NATIVE_LANGUAGES = [
    "Japanese", "Arabic", "Mandarin", "Russian", "Greek",
    "Persian", "Bulgarian", "Estonian", "Croatian", "Swahili",
]

_ATTRS = [
    ("plan", PLANS, "{name}'s subscription plan is {value}.", "What is {name}'s subscription plan?"),
    ("city", CITIES, "{name} lives in {value}.", "What city does {name} live in?"),
    ("job_title", JOB_TITLES, "{name} works as a {value}.", "What is {name}'s job title?"),
    ("native_language", NATIVE_LANGUAGES, "{name}'s native language is {value}.", "What is {name}'s native language?"),
]

INITIAL_FACTS: list[Fact] = [
    Fact(
        entity=ENTITIES[i], attribute=attr, value=values[i],
        statement=stmt_tmpl.format(name=DISPLAY_NAMES[i], value=values[i]),
    )
    for attr, values, stmt_tmpl, _ in _ATTRS
    for i in range(len(DISPLAY_NAMES))
]

# 5 plan updates (entities 0-4) + 5 city updates (entities 5-9). Each new
# value shares no substring with any base PLANS/CITIES value or with each
# other -- see module docstring.
_PLAN_UPDATES = [
    (0, "Momentum", "{name} upgraded to the Momentum plan."),
    (1, "Horizon", "{name} upgraded to the Horizon plan."),
    (2, "Vertex", "{name} upgraded to the Vertex plan."),
    (3, "Zenith", "{name} upgraded to the Zenith plan."),
    (4, "Catalyst", "{name} upgraded to the Catalyst plan."),
]
_CITY_UPDATES = [
    (5, "Porto", "{name} moved to Porto."),
    (6, "Accra", "{name} moved to Accra."),
    (7, "Kyoto", "{name} moved to Kyoto."),
    (8, "Bogota", "{name} moved to Bogota."),
    (9, "Riga", "{name} moved to Riga."),
]

UPDATE_FACTS: list[Fact] = [
    Fact(entity=ENTITIES[i], attribute="plan", value=value, statement=stmt_tmpl.format(name=DISPLAY_NAMES[i]))
    for i, value, stmt_tmpl in _PLAN_UPDATES
] + [
    Fact(entity=ENTITIES[i], attribute="city", value=value, statement=stmt_tmpl.format(name=DISPLAY_NAMES[i]))
    for i, value, stmt_tmpl in _CITY_UPDATES
]


def current_value(entity: str, attribute: str) -> str:
    """The post-update expected value for (entity, attribute): the
    UPDATE_FACTS value if this pair was updated, else the INITIAL_FACTS
    value. Raises KeyError if this pair was never stored at all.
    """
    for fact in UPDATE_FACTS:
        if fact.entity == entity and fact.attribute == attribute:
            return fact.value
    for fact in INITIAL_FACTS:
        if fact.entity == entity and fact.attribute == attribute:
            return fact.value
    raise KeyError((entity, attribute))


def updated_pairs() -> set[tuple[str, str]]:
    """(entity, attribute) pairs that appear in UPDATE_FACTS."""
    return {(f.entity, f.attribute) for f in UPDATE_FACTS}


KNOWN_QUESTIONS: list[Question] = [
    Question(
        entity=ENTITIES[i], attribute=attr,
        question=q_tmpl.format(name=DISPLAY_NAMES[i]),
        expected_value=current_value(ENTITIES[i], attr),
    )
    for attr, _, _, q_tmpl in _ATTRS
    for i in range(len(DISPLAY_NAMES))
]

# 5 topics x 3 entities each -- none ever appear in INITIAL_FACTS/UPDATE_FACTS.
_UNKNOWN = [
    (0, "shoe_size", "What is {name}'s shoe size?"),
    (2, "shoe_size", "What is {name}'s shoe size?"),
    (7, "shoe_size", "What is {name}'s shoe size?"),
    (1, "middle_name", "What is {name}'s middle name?"),
    (3, "middle_name", "What is {name}'s middle name?"),
    (8, "middle_name", "What is {name}'s middle name?"),
    (4, "favorite_food", "What is {name}'s favorite food?"),
    (5, "favorite_food", "What is {name}'s favorite food?"),
    (9, "favorite_food", "What is {name}'s favorite food?"),
    (6, "pet_name", "What is the name of {name}'s pet?"),
    (0, "pet_name", "What is the name of {name}'s pet?"),
    (3, "pet_name", "What is the name of {name}'s pet?"),
    (2, "birth_year", "What year was {name} born?"),
    (7, "birth_year", "What year was {name} born?"),
    (1, "birth_year", "What year was {name} born?"),
]

UNKNOWN_QUESTIONS: list[Question] = [
    Question(entity=ENTITIES[i], attribute=attr, question=q_tmpl.format(name=DISPLAY_NAMES[i]), expected_value=None)
    for i, attr, q_tmpl in _UNKNOWN
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_dataset.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/vs_mem0/dataset.py tests/test_benchmark_dataset.py
git commit -m "feat: add deterministic benchmark dataset for orlog-vs-mem0"
```

---

### Task 3: Grading helpers

**Files:**
- Create: `benchmarks/vs_mem0/grading.py`
- Test: `tests/test_benchmark_grading.py`

**Interfaces:**
- Consumes: nothing (pure, no dependency on `dataset.py`)
- Produces:
  - `is_correct(returned_text: str | None, expected_value: str) -> bool`
  - `percentiles(values_ms: list[float]) -> dict` with keys `mean`, `p50`, `p95`, `n`

- [ ] **Step 1: Write the failing tests**

`tests/test_benchmark_grading.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_grading.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.vs_mem0.grading'`

- [ ] **Step 3: Implement `grading.py`**

`benchmarks/vs_mem0/grading.py`:
```python
"""grading.py: pure, deterministic scoring helpers -- no I/O, no network."""

from __future__ import annotations

import math


def is_correct(returned_text: str | None, expected_value: str) -> bool:
    """Case-insensitive substring match. False for None/empty returned_text."""
    if not returned_text:
        return False
    return expected_value.lower() in returned_text.lower()


def percentiles(values_ms: list[float]) -> dict:
    """Nearest-rank mean/p50/p95 over values_ms. Empty input -> all None,
    n=0. Nearest-rank index for percentile p (0-100) over n sorted values:
    index = ceil(p / 100 * n) - 1, clamped to [0, n-1].
    """
    if not values_ms:
        return {"mean": None, "p50": None, "p95": None, "n": 0}
    ordered = sorted(values_ms)
    n = len(ordered)

    def _rank(pct: float) -> float:
        index = max(0, min(n - 1, math.ceil(pct / 100 * n) - 1))
        return ordered[index]

    return {
        "mean": sum(ordered) / n,
        "p50": _rank(50),
        "p95": _rank(95),
        "n": n,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_grading.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/vs_mem0/grading.py tests/test_benchmark_grading.py
git commit -m "feat: add pure grading/latency-percentile helpers for the benchmark"
```

---

### Task 4: Scoring

**Files:**
- Create: `benchmarks/vs_mem0/score.py`
- Test: `tests/test_benchmark_score.py`

**Interfaces:**
- Consumes: `grading.is_correct`, `grading.percentiles` (Task 3); `dataset.updated_pairs` (Task 2)
- Produces:
  - Shared per-item record schema (produced by Tasks 5/6, consumed here):
    ```python
    {
        "phase": "ingest" | "query_known" | "query_unknown",
        "entity": str, "attribute": str, "latency_ms": float,
        "status": "ok" | "error", "error": str | None,
        "returned_text": str | None, "expected_value": str | None,
        "abstained": bool | None, "tokens": int | None,
    }
    ```
  - `load_raw_results(path: Path) -> list[dict]`
  - `compute_latency_stats(records: list[dict], phase: str) -> dict` (percentiles + `errors: int`)
  - `compute_accuracy(records: list[dict], updated_pairs: set[tuple[str, str]]) -> dict` (`overall`, `overall_n`, `post_update`, `post_update_n`)
  - `compute_abstention(records: list[dict]) -> dict` (`correct_rate`, `n`)
  - `compute_tokens(records: list[dict]) -> dict` (`total`, `n_with_tokens`)
  - `compute_metrics(system_name: str, records: list[dict], updated_pairs: set[tuple[str, str]]) -> dict`
  - `main(orlog_path: Path, mem0_path: Path, out_path: Path) -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/test_benchmark_score.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_score.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.vs_mem0.score'`

- [ ] **Step 3: Implement `score.py`**

`benchmarks/vs_mem0/score.py`:
```python
"""score.py: combines run_orlog.py's and run_mem0.py's raw per-item JSON
result files into aggregated metrics for the benchmark dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.vs_mem0 import grading


def load_raw_results(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_latency_stats(records: list[dict], phase: str) -> dict:
    ok = [r for r in records if r["phase"] == phase and r["status"] == "ok"]
    errors = sum(1 for r in records if r["phase"] == phase and r["status"] == "error")
    stats = grading.percentiles([r["latency_ms"] for r in ok])
    stats["errors"] = errors
    return stats


def compute_accuracy(records: list[dict], updated_pairs: set[tuple[str, str]]) -> dict:
    known = [r for r in records if r["phase"] == "query_known" and r["status"] == "ok"]
    overall = [grading.is_correct(r["returned_text"], r["expected_value"]) for r in known]
    post_update = [
        grading.is_correct(r["returned_text"], r["expected_value"])
        for r in known
        if (r["entity"], r["attribute"]) in updated_pairs
    ]
    return {
        "overall": (sum(overall) / len(overall)) if overall else None,
        "overall_n": len(overall),
        "post_update": (sum(post_update) / len(post_update)) if post_update else None,
        "post_update_n": len(post_update),
    }


def compute_abstention(records: list[dict]) -> dict:
    unknown = [r for r in records if r["phase"] == "query_unknown" and r["status"] == "ok"]
    correct = [bool(r["abstained"]) for r in unknown]
    return {
        "correct_rate": (sum(correct) / len(correct)) if correct else None,
        "n": len(correct),
    }


def compute_tokens(records: list[dict]) -> dict:
    with_tokens = [r["tokens"] for r in records if r.get("tokens") is not None]
    return {"total": sum(with_tokens), "n_with_tokens": len(with_tokens)}


def compute_metrics(system_name: str, records: list[dict], updated_pairs: set[tuple[str, str]]) -> dict:
    return {
        "system": system_name,
        "ingest": compute_latency_stats(records, "ingest"),
        "query_known": compute_latency_stats(records, "query_known"),
        "query_unknown": compute_latency_stats(records, "query_unknown"),
        "accuracy": compute_accuracy(records, updated_pairs),
        "abstention": compute_abstention(records),
        "tokens": compute_tokens(records),
    }


def main(orlog_path: Path, mem0_path: Path, out_path: Path) -> dict:
    from benchmarks.vs_mem0.dataset import updated_pairs as get_updated_pairs

    pairs = get_updated_pairs()
    orlog_records = load_raw_results(orlog_path) if Path(orlog_path).exists() else []
    mem0_records = load_raw_results(mem0_path) if Path(mem0_path).exists() else []
    combined = {
        "orlog": compute_metrics("orlog", orlog_records, pairs),
        "mem0": compute_metrics("mem0", mem0_records, pairs),
    }
    Path(out_path).write_text(json.dumps(combined, indent=2), encoding="utf-8")
    return combined


if __name__ == "__main__":
    base = Path(__file__).parent
    main(base / "orlog_results.json", base / "mem0_results.json", base / "results.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_score.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/vs_mem0/score.py tests/test_benchmark_score.py
git commit -m "feat: add scoring module that aggregates raw benchmark records into metrics"
```

---

### Task 5: Orlog runner

**Files:**
- Create: `benchmarks/vs_mem0/run_orlog.py`
- Test: `tests/test_benchmark_run_orlog.py`

**Interfaces:**
- Consumes: `orlog.config.{DeriverConfig, OrlogConfig, RetrievalConfig, WorkspaceConfig}`, `orlog.runtime.Runtime`, `orlog.server_tools.{recall_tool, remember_tool, stats_tool}`, `orlog.vault.generate_key`, `orlog.workspace.Workspace` (all existing, unmodified); `benchmarks.vs_mem0.dataset.{Fact, Question, INITIAL_FACTS, UPDATE_FACTS, KNOWN_QUESTIONS, UNKNOWN_QUESTIONS}` (Task 2)
- Produces:
  - `build_runtime(workspace_dir: Path, *, deriver_backend: str = "openai", deriver_model: str = "gpt-5-mini", embedder: str = "BAAI/bge-small-en-v1.5") -> Runtime`
  - `warm_up_embedder(runtime: Runtime) -> None`
  - `ingest(runtime: Runtime, facts: list[Fact]) -> list[dict]` (records match Task 4's shared schema, `phase="ingest"`)
  - `run_queries(runtime: Runtime, questions: list[Question], phase: str) -> list[dict]`
  - `main(out_path: Path) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

`tests/test_benchmark_run_orlog.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_run_orlog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.vs_mem0.run_orlog'`

- [ ] **Step 3: Implement `run_orlog.py`**

`benchmarks/vs_mem0/run_orlog.py`:
```python
"""run_orlog.py: drives a real orlog Runtime through the benchmark dataset,
timing every write and query, and writes one JSON record per item to
orlog_results.json. Uses real OpenAI API calls via orlog's LLMDeriver
(deriver.backend="openai") for every query -- see
docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from orlog.config import DeriverConfig, OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import recall_tool, remember_tool, stats_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

from benchmarks.vs_mem0.dataset import INITIAL_FACTS, KNOWN_QUESTIONS, UNKNOWN_QUESTIONS, UPDATE_FACTS


def build_runtime(
    workspace_dir: Path, *, deriver_backend: str = "openai",
    deriver_model: str = "gpt-5-mini", embedder: str = "BAAI/bge-small-en-v1.5",
) -> Runtime:
    os.environ["ORLOG_VAULT_KEY"] = generate_key()
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="vs_mem0_bench"),
        deriver=DeriverConfig(backend=deriver_backend, model=deriver_model, api_key_env="OPENAI_API_KEY"),
        retrieval=RetrievalConfig(embedder=embedder),
    )
    return Runtime(Workspace(workspace_dir), config)


def warm_up_embedder(runtime: Runtime) -> None:
    """Touches runtime.embedder once, outside any timed loop, so the first
    real query doesn't pay a cold-start model download.
    """
    _ = runtime.embedder


def _ingest_record(entity: str, attribute: str, latency_ms: float, status: str, *, error: str | None = None) -> dict:
    return {
        "phase": "ingest", "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }


def _query_record(
    phase: str, entity: str, attribute: str, latency_ms: float, status: str, *,
    expected_value: str | None = None, answer: dict | None = None, tokens: dict | None = None,
    error: str | None = None,
) -> dict:
    returned_text = answer.get("claim") if answer else None
    abstained = answer.get("abstained") if answer else None
    total_tokens = (tokens["in"] + tokens["out"]) if tokens else None
    return {
        "phase": phase, "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": returned_text, "expected_value": expected_value,
        "abstained": abstained, "tokens": total_tokens,
    }


def ingest(runtime: Runtime, facts: list) -> list[dict]:
    records = []
    for fact in facts:
        started = time.perf_counter()
        try:
            remember_tool(runtime, fact.statement, entity=fact.entity, attribute=fact.attribute, value=fact.value)
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "ok"))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "error", error=str(exc)))
    return records


def run_queries(runtime: Runtime, questions: list, phase: str) -> list[dict]:
    records = []
    for q in questions:
        before = stats_tool(runtime)["tokens"]
        started = time.perf_counter()
        try:
            answer = recall_tool(runtime, q.question)
            latency_ms = (time.perf_counter() - started) * 1000
            after = stats_tool(runtime)["tokens"]
            diff = {"in": after["in"] - before["in"], "out": after["out"] - before["out"]}
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "ok",
                expected_value=q.expected_value, answer=answer, tokens=diff,
            ))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "error",
                expected_value=q.expected_value, error=str(exc),
            ))
    return records


def main(out_path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="orlog_vs_mem0_") as tmp:
        runtime = build_runtime(Path(tmp) / "workspace")
        warm_up_embedder(runtime)
        records = []
        records += ingest(runtime, INITIAL_FACTS + UPDATE_FACTS)
        records += run_queries(runtime, KNOWN_QUESTIONS, "query_known")
        records += run_queries(runtime, UNKNOWN_QUESTIONS, "query_unknown")
    Path(out_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


if __name__ == "__main__":
    main(Path(__file__).parent / "orlog_results.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_run_orlog.py -v`
Expected: 5 passed. (The last test builds a real, on-disk workspace with the offline `scripted`/`hashing` config — no network call, should run in well under a second.)

- [ ] **Step 5: Commit**

```bash
git add benchmarks/vs_mem0/run_orlog.py tests/test_benchmark_run_orlog.py
git commit -m "feat: add orlog runner for the vs-mem0 benchmark"
```

---

### Task 6: Mem0 runner

**Files:**
- Create: `benchmarks/vs_mem0/run_mem0.py`
- Test: `tests/test_benchmark_run_mem0.py`

**Interfaces:**
- Consumes: `mem0.Memory` (installed, `mem0ai>=2.0`); `benchmarks.vs_mem0.dataset.{Fact, Question, INITIAL_FACTS, UPDATE_FACTS, KNOWN_QUESTIONS, UNKNOWN_QUESTIONS}` (Task 2)
- Produces:
  - `BENCHMARK_USER_ID: str` (`"vs_mem0_bench"`)
  - `build_memory(qdrant_path: Path, *, llm_model: str = "gpt-5-mini") -> Memory`
  - `ingest(memory: Memory, facts: list[Fact]) -> list[dict]` (same shared schema as Task 5, `tokens` always `None` — Mem0 doesn't expose usage in its `add`/`search` responses)
  - `run_queries(memory: Memory, questions: list[Question], phase: str) -> list[dict]`
  - `main(out_path: Path) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

`tests/test_benchmark_run_mem0.py`:
```python
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
```

Note: unlike `run_orlog.py`, Mem0 has no offline/no-API mode (its default `llm`/`embedder` are both OpenAI-backed and `infer=False` still requires a working embedder for storage), so there is no equivalent offline orchestration smoke test here. `ingest`/`run_queries`/`main`'s real behavior is exercised by Task 8's end-to-end run — see this file's own module docstring and the spec's "Testing" section.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_run_mem0.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.vs_mem0.run_mem0'`

- [ ] **Step 3: Implement `run_mem0.py`**

`benchmarks/vs_mem0/run_mem0.py`:
```python
"""run_mem0.py: drives a real mem0 Memory instance through the benchmark
dataset, timing every add()/search() call, and writes one JSON record per
item to mem0_results.json. All entities share a single Mem0 user_id
("vs_mem0_bench") -- mirroring orlog's single global event log, so both
systems face the same challenge of disambiguating the right entity from
free-text alone rather than relying on a pre-scoped partition per entity.
See docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from mem0 import Memory

BENCHMARK_USER_ID = "vs_mem0_bench"


def build_memory(qdrant_path: Path, *, llm_model: str = "gpt-5-mini") -> Memory:
    return Memory.from_config({
        "vector_store": {"provider": "qdrant", "config": {"path": str(qdrant_path)}},
        "llm": {"provider": "openai", "config": {"model": llm_model}},
    })


def _ingest_record(entity: str, attribute: str, latency_ms: float, status: str, *, error: str | None = None) -> dict:
    return {
        "phase": "ingest", "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }


def _query_record(
    phase: str, entity: str, attribute: str, latency_ms: float, status: str, *,
    expected_value: str | None = None, results: list[dict] | None = None, error: str | None = None,
) -> dict:
    top = results[0] if results else None
    returned_text = top.get("memory") if top else None
    abstained = (not bool(results)) if results is not None else None
    return {
        "phase": phase, "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": returned_text, "expected_value": expected_value,
        "abstained": abstained, "tokens": None,
    }


def ingest(memory: Memory, facts: list) -> list[dict]:
    records = []
    for fact in facts:
        started = time.perf_counter()
        try:
            memory.add(fact.statement, user_id=BENCHMARK_USER_ID)
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "ok"))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "error", error=str(exc)))
    return records


def run_queries(memory: Memory, questions: list, phase: str) -> list[dict]:
    records = []
    for q in questions:
        started = time.perf_counter()
        try:
            response = memory.search(q.question, filters={"user_id": BENCHMARK_USER_ID})
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "ok",
                expected_value=q.expected_value, results=response.get("results", []),
            ))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "error",
                expected_value=q.expected_value, error=str(exc),
            ))
    return records


def main(out_path: Path) -> list[dict]:
    from benchmarks.vs_mem0.dataset import INITIAL_FACTS, KNOWN_QUESTIONS, UNKNOWN_QUESTIONS, UPDATE_FACTS

    with tempfile.TemporaryDirectory(prefix="mem0_vs_orlog_") as tmp:
        memory = build_memory(Path(tmp) / "qdrant")
        records = []
        records += ingest(memory, INITIAL_FACTS + UPDATE_FACTS)
        records += run_queries(memory, KNOWN_QUESTIONS, "query_known")
        records += run_queries(memory, UNKNOWN_QUESTIONS, "query_unknown")
    Path(out_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


if __name__ == "__main__":
    main(Path(__file__).parent / "mem0_results.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_benchmark_run_mem0.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add benchmarks/vs_mem0/run_mem0.py tests/test_benchmark_run_mem0.py
git commit -m "feat: add mem0 runner for the vs-mem0 benchmark"
```

---

### Task 7: README

**Files:**
- Create: `benchmarks/vs_mem0/README.md`

**Interfaces:**
- Consumes: nothing new (documents Tasks 1-6 and previews Task 8's orchestrator)

- [ ] **Step 1: Write the README**

`benchmarks/vs_mem0/README.md`:
```markdown
# orlog vs Mem0 benchmark

Compares [orlog](../../README.md) and [Mem0](https://github.com/mem0ai/mem0)
on speed, accuracy, and abstention/false-positive behavior, using a
deterministic 10-entity/4-attribute synthetic dataset (`dataset.py`).

Full design rationale:
[`docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md`](../../docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md).

## Running it

Requires `OPENAI_API_KEY` in the environment (both systems use OpenAI --
`gpt-5-mini` -- as their LLM backend, since no Anthropic key was configured
when this benchmark was built) and the `benchmark` extra installed:

```
python -m pip install -e ".[benchmark]"
python -m benchmarks.vs_mem0
```

This makes real, billed OpenAI API calls: one derivation call per orlog
query, one extraction (+ possible update-decision) call per Mem0 `add()`.
At this benchmark's scale (40 initial facts + 10 updates + 55 questions per
system) that's on the order of 100-150 LLM calls total, well under $1 with
`gpt-5-mini`.

Writes `orlog_results.json`, `mem0_results.json` (raw per-item timings and
outcomes) and `results.json` (aggregated metrics) into this directory.

## Methodology

- **Same inputs, both systems.** Every fact's natural-language statement is
  fed to both `orlog.remember()` and `mem0.add()`; every question is fed to
  both `orlog.recall()` (free-text path) and `mem0.search()`.
- **Single shared scope.** All 10 entities live in orlog's one event log and
  in one shared Mem0 `user_id`. Neither system is told at query time which
  entity a question is about -- it has to work that out from the question
  text itself, the same challenge either way.
- **Grading is a deterministic, case-insensitive substring match**
  (`grading.is_correct`) between the expected value and whatever text the
  system returned -- not an LLM judge. This works cleanly here because every
  attribute's value pool (`dataset.py`) is constructed so no value is a
  substring of another, but it is a simplification worth knowing about
  before generalizing these numbers beyond this dataset.
- **Abstention/false-positive rate** on the 15 "unknown" questions (about
  attributes never stored for that entity): orlog counts a `NO_CANDIDATES`/
  other abstention as correct; Mem0 counts "no result cleared its own
  default `threshold=0.1`" as correct. Mem0 has no built-in
  verified/abstained concept -- this metric is specifically about whether it
  ever hands back a stored memory in response to a question it has no real
  answer to.
- **Mem0's defaults are used as-is** (`threshold=0.1`, `infer=True`,
  `version="v1.1"`) -- nothing is tuned in either system's favor.
- **Numbers will drift slightly between runs.** Real LLM calls aren't
  perfectly deterministic; the committed `results.json` is one real
  snapshot, not a guarantee reproduced exactly on every rerun.
```

- [ ] **Step 2: Commit**

```bash
git add benchmarks/vs_mem0/README.md
git commit -m "docs: add methodology README for the vs-mem0 benchmark"
```

---

### Task 8: Orchestrator + real end-to-end run

**Files:**
- Create: `benchmarks/vs_mem0/__main__.py`
- Create (generated by running the script, then committed): `benchmarks/vs_mem0/orlog_results.json`, `benchmarks/vs_mem0/mem0_results.json`, `benchmarks/vs_mem0/results.json`

**Interfaces:**
- Consumes: `run_orlog.main`, `run_mem0.main`, `score.main` (Tasks 4-6)
- Produces: a runnable `python -m benchmarks.vs_mem0` entry point

- [ ] **Step 1: Implement the orchestrator**

`benchmarks/vs_mem0/__main__.py`:
```python
"""Runs the full orlog vs Mem0 benchmark end to end: both systems' raw
runs, then scoring. Requires OPENAI_API_KEY in the environment; makes real,
billed OpenAI API calls for every ingest and query -- see README.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from benchmarks.vs_mem0 import run_mem0, run_orlog, score

BENCH_DIR = Path(__file__).parent


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set -- both systems require it.", file=sys.stderr)
        raise SystemExit(1)

    print("Running orlog...")
    run_orlog.main(BENCH_DIR / "orlog_results.json")
    print("Running mem0...")
    run_mem0.main(BENCH_DIR / "mem0_results.json")
    print("Scoring...")
    combined = score.main(
        BENCH_DIR / "orlog_results.json", BENCH_DIR / "mem0_results.json", BENCH_DIR / "results.json",
    )
    print(f"Wrote {BENCH_DIR / 'results.json'}")
    for system in ("orlog", "mem0"):
        m = combined[system]
        print(
            f"{system}: accuracy={m['accuracy']['overall']} "
            f"abstention={m['abstention']['correct_rate']} "
            f"query_p50_ms={m['query_known']['p50']}"
        )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the full benchmark for real**

Run: `.venv/Scripts/python.exe -m benchmarks.vs_mem0`
Expected: prints progress for both systems, then a summary line per system with non-`None` `accuracy`/`abstention`/`query_p50_ms` values. This makes real OpenAI API calls and takes several minutes -- see README.md's cost/scale note.

- [ ] **Step 3: Sanity-check the results**

Open `benchmarks/vs_mem0/results.json` and confirm, for both `orlog` and `mem0`:
- `ingest.n == 50` and `query_known.n + query_unknown.n` sums close to 55 (allowing for a handful of `status=="error"` retries being excluded)
- `accuracy.overall` is a plausible fraction (not `None`, not wildly implausible like `0.0` for both -- if either is `0.0`, stop and debug before proceeding, per this project's "fail visible, not plausible" convention rather than shipping a broken benchmark as if it were a real result)

- [ ] **Step 4: Commit the orchestrator and the real result snapshot**

```bash
git add benchmarks/vs_mem0/__main__.py benchmarks/vs_mem0/orlog_results.json benchmarks/vs_mem0/mem0_results.json benchmarks/vs_mem0/results.json
git commit -m "feat: add benchmark orchestrator; commit a real orlog-vs-mem0 run"
```

---

### Task 9: Interactive dashboard

**Files:**
- Create: `benchmarks/vs_mem0/dashboard.html`

**Interfaces:**
- Consumes: `benchmarks/vs_mem0/results.json` (Task 8's committed output)
- Produces: a self-contained HTML file, published as an Artifact

- [ ] **Step 1: Invoke the dataviz skill**

Before writing any chart code or choosing colors, load the `dataviz` skill (`Skill` tool, `skill: "dataviz"`) and follow its palette/form/layout guidance for this dashboard.

- [ ] **Step 2: Read the real results**

Read `benchmarks/vs_mem0/results.json` (Task 8's committed output) to get the actual numbers this dashboard will render.

- [ ] **Step 3: Build `dashboard.html`**

A self-contained HTML file (inline CSS/JS/SVG or chart code per the dataviz skill's rules -- no CDN/external requests) with the values from `results.json` embedded directly (not fetched at runtime, so the page works standalone). Include, per the spec's "Output / charts" section:
- Ingest latency bar chart (mean/p50/p95, orlog vs. mem0)
- Query latency bar chart (known vs. unknown phase, orlog vs. mem0)
- Accuracy bar chart (overall vs. post-update-only, orlog vs. mem0)
- Abstention/false-positive-rate bar chart (orlog vs. mem0)
- Token-usage chart (orlog vs. mem0, noting mem0's is unavailable/omitted)
- A short methodology note linking back to `README.md`'s content, so the dashboard is self-explanatory without the repo open alongside it

- [ ] **Step 4: Publish as an Artifact**

Use the `Artifact` tool with `file_path` pointing at `benchmarks/vs_mem0/dashboard.html`, an appropriate `title`/`description`, and a `favicon`.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/vs_mem0/dashboard.html
git commit -m "feat: add orlog-vs-mem0 benchmark dashboard"
```
