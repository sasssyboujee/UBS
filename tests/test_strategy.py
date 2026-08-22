from app.models import MoveRequest
from app.strategy import (
    choose_action,
    post_reveal_equity,
    pot_odds,
    pre_reveal_equity,
)


def test_pre_reveal_equity_known_values():
    assert pre_reveal_equity(1) == 37 / 338
    assert pre_reveal_equity(13) == 301 / 338
    assert abs(pre_reveal_equity(7) - 0.5) < 1e-9


def test_post_reveal_equity_pair_is_near_certain():
    assert abs(post_reveal_equity(13, 13) - 25 / 26) < 1e-9
    assert abs(post_reveal_equity(1, 1) - 25 / 26) < 1e-9


def test_post_reveal_equity_no_pair():
    # Card 3 vs community 5: wins vs 1,2; ties vs 3.
    assert abs(post_reveal_equity(3, 5) - 2.5 / 13) < 1e-9
    # Card 12 vs community 3: wins vs 1-11 except 3 (10 wins), ties vs 12.
    assert abs(post_reveal_equity(12, 3) - 10.5 / 13) < 1e-9


def test_pot_odds():
    assert pot_odds(0, 10) == 0.0
    assert pot_odds(18, 32) == 18 / 50


def test_choose_action_pair_calls_even_when_only_fold_and_call():
    request = MoveRequest(
        round="post_reveal",
        your_number=7,
        community_number=7,
        to_call=100,
        pot=100,
        your_stack=100,
        legal_actions=["fold", "call"],
    )
    response = choose_action(request)
    assert response.action == "call"
    assert response.amount is None


def test_choose_action_never_returns_illegal_action():
    request = MoveRequest(
        round="post_reveal",
        your_number=2,
        community_number=9,
        to_call=150,
        pot=150,
        legal_actions=["call"],
    )
    response = choose_action(request)
    assert response.action == "call"


def test_pre_reveal_folds_medium_card_to_raise():
    request = MoveRequest(
        round="pre_reveal",
        your_number=7,
        to_call=20,
        pot=20,
        your_stack=200,
        your_seat=0,
        button_seat=1,
        min_raise_to=40,
        max_raise_to=200,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action == "fold"


def test_pre_reveal_calls_with_premium_to_raise():
    request = MoveRequest(
        round="pre_reveal",
        your_number=12,
        to_call=10,
        pot=20,
        your_stack=200,
        your_seat=0,
        button_seat=1,
        min_raise_to=30,
        max_raise_to=200,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action == "call"


def test_post_reveal_non_pair_does_not_reraise_a_raise():
    request = MoveRequest(
        round="post_reveal",
        your_number=13,
        community_number=6,
        to_call=10,
        pot=54,
        your_stack=173,
        your_seat=0,
        button_seat=1,
        min_raise_to=38,
        max_raise_to=173,
        legal_actions=["fold", "call", "raise"],
        current_hand_actions=[
            {"round": "post_reveal", "seat": 0, "action": "raise", "amount": 28},
            {"round": "post_reveal", "seat": 1, "action": "raise", "amount": 55},
        ],
    )
    response = choose_action(request)
    assert response.action in ("call", "fold")


def test_small_blind_first_action_raises_strong_card():
    request = MoveRequest(
        round="pre_reveal",
        your_number=11,
        to_call=1,
        pot=3,
        your_stack=199,
        your_seat=1,
        button_seat=1,
        min_raise_to=4,
        max_raise_to=199,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action == "raise"
    assert response.amount == 4
