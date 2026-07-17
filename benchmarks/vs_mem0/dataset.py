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
