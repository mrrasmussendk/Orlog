"""Contract: the epistemic state machine rejects illegal transitions."""

import pytest

from orlog.states import ALLOWED_TRANSITIONS, EpistemicState, IllegalTransitionError, TERMINAL_STATES, transition

HAPPY_PATH = [
    EpistemicState.OBSERVED,
    EpistemicState.INTERPRETED,
    EpistemicState.RETRIEVED,
    EpistemicState.ASSERTED,
    EpistemicState.VERIFIED,
    EpistemicState.REINFORCED,
]


def test_happy_path_is_legal_start_to_finish():
    state = HAPPY_PATH[0]
    for target in HAPPY_PATH[1:]:
        state = transition(state, target)
    assert state == EpistemicState.REINFORCED


def test_asserted_can_go_to_abstained_directly():
    assert transition(EpistemicState.ASSERTED, EpistemicState.ABSTAINED) is EpistemicState.ABSTAINED


@pytest.mark.parametrize(
    "current,target",
    [
        (EpistemicState.OBSERVED, EpistemicState.ASSERTED),  # skips two steps
        (EpistemicState.OBSERVED, EpistemicState.VERIFIED),
        (EpistemicState.RETRIEVED, EpistemicState.VERIFIED),  # skips ASSERTED
        (EpistemicState.VERIFIED, EpistemicState.OBSERVED),  # backwards
        (EpistemicState.ABSTAINED, EpistemicState.VERIFIED),  # out of a terminal state
        (EpistemicState.REINFORCED, EpistemicState.ASSERTED),  # out of a terminal state
    ],
)
def test_illegal_transitions_are_rejected(current, target):
    with pytest.raises(IllegalTransitionError):
        transition(current, target)


def test_terminal_states_have_no_outgoing_transitions():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
