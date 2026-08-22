from app.models import HandResult, MoveRequest, PlayerState
from app.strategy import (
    _codename_observations,
    _live_opponents,
    _post_reveal_opp_weights,
    _post_reveal_opp_weights_observed,
    _record_recent_hands,
    _reset_learning,
    _rule_showdown,
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


# ---------------------------------------------------------------------------
# Phase 2: unknown table rules, learning from recent_hands
# ---------------------------------------------------------------------------


def make_learned_request(codename, observations, match_id="learn-match"):
    """Build a request whose recent_hands record `our_card` beating `opp_card`."""
    hands = [
        HandResult(
            hand_number=i,
            community_number=7,
            winners=[0],
            shown_numbers={"you": our_card, "opp": opp_card},
        )
        for i, (our_card, opp_card) in enumerate(observations, start=1)
    ]
    return MoveRequest(
        match_id=match_id,
        table_rule=codename,
        leg_number=1,
        total_legs=4,
        your_seat=0,
        button_seat=1,
        recent_hands=hands,
    )


def test_rule_showdown_falls_back_to_standard_without_observations():
    _reset_learning()
    assert _rule_showdown(13, 2, 7, "unseen_rule") == 1.0
    assert _rule_showdown(2, 13, 7, "unseen_rule") == 0.0
    assert _rule_showdown(7, 13, 7, "unseen_rule") == 1.0  # pair beats non-pair


def test_recent_hands_are_deduplicated():
    _reset_learning()
    request = make_learned_request("dedupe_rule", [(2, 13), (3, 12), (4, 11)])
    _record_recent_hands(request)
    assert _codename_observations("dedupe_rule") == 3

    # The same recent_hands arrive again on the next turn; nothing new.
    _record_recent_hands(request)
    assert _codename_observations("dedupe_rule") == 3

    # A fresh request with one extra hand only adds that hand.
    request2 = make_learned_request(
        "dedupe_rule", [(2, 13), (3, 12), (4, 11), (5, 10)]
    )
    _record_recent_hands(request2)
    assert _codename_observations("dedupe_rule") == 4


def test_learning_flips_showdown_order():
    _reset_learning()
    # Teach the codename that lower cards beat higher cards.
    observations = [(i, j) for i in range(1, 13) for j in range(i + 1, 14)]
    _record_recent_hands(make_learned_request("low_wins", observations))
    assert _codename_observations("low_wins") == 78

    assert _rule_showdown(2, 13, 7, "low_wins") == 1.0
    assert _rule_showdown(13, 2, 7, "low_wins") == 0.0
    # A pair type with no observations (community 5) keeps the strong prior.
    assert _rule_showdown(2, 5, 5, "low_wins") == 0.0
    # The community 7 pair was observed losing to low cards, so it is weak.
    assert _rule_showdown(2, 7, 7, "low_wins") == 1.0


def test_choose_action_unknown_rule_no_data_stays_legal():
    _reset_learning()
    request = MoveRequest(
        round="post_reveal",
        table_rule="mystery",
        your_number=5,
        community_number=9,
        to_call=10,
        pot=30,
        your_stack=200,
        min_raise_to=20,
        max_raise_to=200,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action in request.legal_actions


def test_choose_action_unknown_rule_learned_value_bets():
    _reset_learning()
    observations = [(i, j) for i in range(1, 13) for j in range(i + 1, 14)]
    _record_recent_hands(make_learned_request("low_wins", observations))

    request = MoveRequest(
        round="post_reveal",
        table_rule="low_wins",
        match_id="learn-match",
        leg_number=1,
        total_legs=4,
        your_number=2,
        community_number=7,
        to_call=0,
        pot=10,
        your_stack=200,
        min_raise_to=4,
        max_raise_to=200,
        legal_actions=["check", "bet"],
    )
    response = choose_action(request)
    # Under the Gambler strategy, we ignore learned rules and low cards, so we just check/fold
    assert response.action == "check"


def test_choose_action_unknown_rule_pre_reveal_stays_cautious_without_data():
    _reset_learning()
    request = MoveRequest(
        round="pre_reveal",
        table_rule="mystery",
        your_number=13,
        to_call=0,
        pot=3,
        your_stack=200,
        your_seat=0,
        button_seat=1,
        min_raise_to=4,
        max_raise_to=200,
        legal_actions=["check", "raise"],
    )
    response = choose_action(request)
    # Under the experimental strategy, we go all-in with a premium card pre-reveal
    assert response.action == "raise"
    assert response.amount == 200


def test_learning_accepts_name_winners_and_string_numbers():
    _reset_learning()
    hands = [
        HandResult(
            hand_number=i,
            community_number=7,
            winners=["you"],
            shown_numbers={"you": "2", "Gaston": "13"},
        )
        for i in range(1, 7)
    ]
    request = MoveRequest(
        match_id="names-match",
        table_rule="names_rule",
        leg_number=1,
        total_legs=4,
        your_seat=0,
        button_seat=1,
        recent_hands=hands,
    )
    _record_recent_hands(request)
    assert _codename_observations("names_rule") == 6
    assert _rule_showdown(2, 13, 7, "names_rule") == 1.0
    assert _rule_showdown(13, 2, 7, "names_rule") == 0.0


def test_recent_hands_dedupe_without_hand_numbers():
    _reset_learning()
    hand = HandResult(
        community_number=7,
        winners=[0],
        shown_numbers={"you": 2, "opp": 13},
    )
    request = MoveRequest(
        match_id="nohand-match",
        table_rule="nohand_rule",
        leg_number=1,
        total_legs=4,
        your_seat=0,
        button_seat=1,
        recent_hands=[hand, hand],
    )
    _record_recent_hands(request)
    assert _codename_observations("nohand_rule") == 1


def test_pairwise_exact_recall_with_single_observation():
    _reset_learning()
    hand = HandResult(
        hand_number=1,
        community_number=7,
        winners=[0],
        shown_numbers={"you": 2, "opp": 13},
    )
    request = MoveRequest(
        match_id="pair-match",
        table_rule="pair_rule",
        leg_number=1,
        total_legs=4,
        your_seat=0,
        button_seat=1,
        recent_hands=[hand],
    )
    _record_recent_hands(request)
    assert _codename_observations("pair_rule") == 1
    # The exact observed matchup is recalled even with a single observation.
    assert _rule_showdown(2, 13, 7, "pair_rule") == 1.0
    assert _rule_showdown(13, 2, 7, "pair_rule") == 0.0


def test_opponent_range_learned_from_showdowns():
    _reset_learning()
    hand = HandResult(
        hand_number=1,
        community_number=7,
        winners=[0],
        shown_numbers={"you": 10, "opp": 4},
        actions=[{"round": "post_reveal", "seat": 1, "action": "bet", "amount": 5}],
    )
    request = MoveRequest(
        match_id="opp-range-match",
        table_rule="opp_range_rule",
        leg_number=1,
        total_legs=4,
        your_seat=0,
        button_seat=1,
        recent_hands=[hand],
    )
    _record_recent_hands(request)
    observed = _post_reveal_opp_weights_observed(7, raised=False)
    standard = _post_reveal_opp_weights(7, raised=False, codename="standard")
    assert observed[4] > standard[4]


# ---------------------------------------------------------------------------
# Phase 3: six-seat multiway
# ---------------------------------------------------------------------------


def make_phase3_request(**overrides):
    """Build a six-seat phase-3 /move request with sensible defaults."""
    players = [
        PlayerState(seat=i, name=name, stack=200)
        for i, name in enumerate(["you", "Dana", "Miles", "Theo", "Rhea", "Bram"])
    ]
    payload = {
        "phase": 3,
        "match_id": "p3-match",
        "table_rule": "standard",
        "round": "pre_reveal",
        "your_number": 13,
        "your_seat": 0,
        "button_seat": 1,
        "your_stack": 200,
        "to_call": 0,
        "pot": 3,
        "min_raise_to": 4,
        "max_raise_to": 200,
        "legal_actions": ["check", "raise"],
        "players": players,
    }
    payload.update(overrides)
    return MoveRequest(**payload)


def test_phase3_pre_reveal_shoves_best_card():
    request = make_phase3_request(your_number=13)
    response = choose_action(request)
    assert response.action == "raise"
    assert response.amount == 200


def test_phase3_pre_reveal_checks_mid_card():
    request = make_phase3_request(your_number=7)
    response = choose_action(request)
    assert response.action == "check"


def test_phase3_post_reveal_pair_jams():
    request = make_phase3_request(
        round="post_reveal",
        your_number=7,
        community_number=7,
        to_call=10,
        pot=20,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action == "raise"
    assert response.amount == 200


def test_phase3_post_reveal_weak_folds_to_bet():
    request = make_phase3_request(
        round="post_reveal",
        your_number=3,
        community_number=10,
        to_call=15,
        pot=30,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action == "fold"


def test_phase3_folded_and_busted_are_ignored():
    players = [
        PlayerState(seat=0, name="you", stack=200),
        PlayerState(seat=1, name="Dana", stack=200),
        PlayerState(seat=2, name="Miles", stack=200, folded=True),
        PlayerState(seat=3, name="Theo", stack=0, busted=True),
        PlayerState(seat=4, name="Rhea", stack=200),
        PlayerState(seat=5, name="Bram", stack=200),
    ]
    request = make_phase3_request(your_number=7, players=players)
    assert _live_opponents(request) == 3


def test_phase3_unknown_rule_stays_cautious():
    _reset_learning()
    request = make_phase3_request(
        table_rule="mystery",
        your_number=13,
        to_call=10,
        pot=30,
        legal_actions=["fold", "call", "raise"],
    )
    response = choose_action(request)
    assert response.action == "fold"
