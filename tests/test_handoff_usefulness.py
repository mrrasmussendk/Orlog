"""Contract: does consulting orlog change what a later agent acts on?

Every other test in this suite asks whether a mechanism works. This one
asks whether the mechanism is worth having: a second agent picks up work
after the first one's context is gone, and the question is whether the
twelve facts it acts on are current.

Two arms over one workspace, from benchmarks/phase0/handoff.py:

  A  what a /compact-style summary carried forward
  B  what recall() serves now

Arm A is not a hand-built straw man. It is `recall(key,
known_as_of=<the instant the summary was written>)` -- literally what the
record knew then, produced by the same code path as arm B with one
argument changed. A control the treatment cannot quietly win against is
the only kind worth asserting on.

The assertions run in both directions on purpose. A test file that only
pins orlog's wins would pass just as happily if the fixture had stopped
planting the failure, so `test_the_baseline_really_does_go_stale` fails if
arm A ever stops being wrong, and `test_orlog_does_not_rescue_what_was_
never_written_down` fails if orlog ever starts answering something nobody
recorded.

What this does NOT establish: that an agent handed current facts does
better work. There is no model here and no judgment. That question is the
Mimir plan's Phase 6 eval -- a fixture repo, three live arms, planted
rules, task success -- and nothing in this file substitutes for it.
"""

from __future__ import annotations

import pytest

from benchmarks.phase0.handoff import COMPACTED_AT, FIXTURE, run, truth


@pytest.fixture(scope="module")
def report():
    return run()


def _planted():
    return [d for d in FIXTURE if d.corrected is not None]


def _unrecorded():
    return [d for d in FIXTURE if not d.recorded]


def _unchanged():
    return [d for d in FIXTURE if d.recorded and d.corrected is None]


# -- the fixture is doing its job ------------------------------------------


def test_the_fixture_contains_cases_where_orlog_cannot_help():
    """A benchmark whose fixture only contains winnable cases measures
    nothing. Six facts never change, and two were never written down."""
    assert len(_unchanged()) >= 5
    assert len(_unrecorded()) >= 2
    assert len(_planted()) >= 3


def test_the_baseline_really_does_go_stale(report):
    """If this fails, the control stopped being wrong -- and every other
    assertion here became meaningless."""
    arm_a = report["arm_a_compacted_summary"]
    assert arm_a["acted_on_stale"] == len(_planted()), (
        "arm A must act on exactly the facts that were corrected after its "
        "summary was written; if it does not, the handoff being measured "
        "is not the one described"
    )


# -- what orlog changes ----------------------------------------------------


def test_asking_orlog_removes_every_stale_decision(report):
    arm_b = report["arm_b_asks_orlog"]
    assert arm_b["acted_on_stale"] == 0
    assert report["delta"]["stale_decisions_avoided"] == len(_planted())


def test_each_corrected_fact_is_served_at_its_current_value(report):
    """Not just "not stale" -- the right value, named."""
    by_key = {(r["entity"], r["attribute"]): r for r in report["rows"]}
    for d in _planted():
        row = by_key[(d.entity, d.attribute)]
        assert row["outcome"] == "correct", row
        assert d.corrected in row["served"]
        assert d.original not in row["served"], (
            f"{d.entity}.{d.attribute} still carries the superseded value"
        )


def test_facts_that_never_changed_are_unaffected(report):
    """Where the summary was already right, orlog agrees with it. This is
    the case orlog adds nothing to, and it must not break it."""
    by_key = {(r["entity"], r["attribute"]): r for r in report["rows"]}
    for d in _unchanged():
        row = by_key[(d.entity, d.attribute)]
        assert row["outcome"] == "correct"
        assert truth(d) in row["served"]


# -- where orlog loses -----------------------------------------------------


def test_orlog_does_not_rescue_what_was_never_written_down(report):
    """The honest limit. A fact nobody recorded is not recoverable, and
    orlog abstains instead of inventing one -- a better failure than a
    confident wrong answer, but still a failure to answer. If this ever
    starts passing as "correct", something is fabricating."""
    by_key = {(r["entity"], r["attribute"]): r for r in report["rows"]}
    for d in _unrecorded():
        row = by_key[(d.entity, d.attribute)]
        assert row["outcome"] == "abstained", row
        assert row["served"] is None

    assert report["arm_b_asks_orlog"]["abstained"] == len(_unrecorded())
    assert report["delta"]["decisions_orlog_could_not_rescue"] == len(_unrecorded())


def test_both_arms_fail_equally_on_what_was_never_recorded(report):
    """Consulting orlog is not a universal improvement: on the two facts
    nobody wrote down, it leaves the agent exactly where the summary did."""
    assert (
        report["arm_a_compacted_summary"]["abstained"]
        == report["arm_b_asks_orlog"]["abstained"]
        == len(_unrecorded())
    )


def test_the_arms_differ_only_in_the_transaction_time_horizon():
    """Pins the fairness of the control: arm A is arm B with a horizon.
    If someone later gives arm A its own cheaper or dumber code path, this
    benchmark stops being a comparison."""
    import inspect

    from benchmarks.phase0 import handoff

    source = inspect.getsource(handoff._score)
    assert source.count("recall_tool(") == 1, (
        "both arms must go through one recall_tool call site"
    )
    assert COMPACTED_AT < handoff.NOW


# -- the headline ----------------------------------------------------------


def test_the_summary_of_the_whole_thing(report):
    a, b = report["arm_a_compacted_summary"], report["arm_b_asks_orlog"]
    assert (a["acted_correctly"], a["acted_on_stale"], a["abstained"]) == (6, 4, 2)
    assert (b["acted_correctly"], b["acted_on_stale"], b["abstained"]) == (10, 0, 2)
    # And no token claim is made here -- both arms run through recall(), so
    # their payload sizes differ only by the horizon string in
    # truth_version. context.py measures what an answer costs.
    assert "tokens" not in a and "tokens" not in b


def test_no_superseded_value_hides_inside_its_replacement():
    """Guards the fixture itself. The first draft paired "mcp>=1.0" with
    "mcp>=1.0,<2", which made "it served the new value" and "the old one is
    gone" the same question -- the substring trap vs_mem0's own dataset
    documents. The assertion above caught it; this keeps it caught."""
    for d in _planted():
        assert d.original not in d.corrected, (d.entity, d.attribute)
        assert d.corrected not in d.original, (d.entity, d.attribute)
