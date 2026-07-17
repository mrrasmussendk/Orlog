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
