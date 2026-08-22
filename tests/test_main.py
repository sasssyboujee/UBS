from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "version": "1.0.0"}

def test_say_hello_valid():
    response = client.post("/hello", json={"name": "Alice"})
    assert response.status_code == 200
    assert response.json() == {"greeting": "Hello, Alice!"}

def test_say_hello_invalid_numbers():
    response = client.post("/hello", json={"name": "Alice123"})
    assert response.status_code == 422
    assert "Value error" in response.text or "Name must not contain numbers" in response.text

def test_say_hello_custom_error():
    response = client.post("/hello", json={"name": "error"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Cannot greet 'error'"}

def test_say_hello_missing_field():
    response = client.post("/hello", json={})
    assert response.status_code == 422

def test_solve_challenge_valid():
    payload = "ewoJImFkYXB0SW5wdXQiOiB7CgkJInVzZXIiOiB7CgkJCSJpZCI6ICJVNDIiLAoJCQkiZnVsbE5hbWUiOiAiSmFuZSBEb2UiCgkJfSwKCQkiYWN0aW9uIjogIkNSRUFURSIsCgkJIm1ldGFkYXRhIjogewoJCQkicHJpb3JpdHkiOiAiSElHSCIKCQl9Cgl9Cn0="
    response = client.post("/solve", json={"payload": payload})
    assert response.status_code == 200
    assert response.json() == {
        "adaptOutput": {
            "id": "U42",
            "name": "Jane Doe",
            "action": "create",
            "priority": 3
        }
    }


def make_move_payload(**overrides):
    payload = {
        "protocol_version": 2,
        "match_id": "phase1-seed7",
        "phase": 1,
        "table_rule": "standard",
        "small_blind": 1,
        "big_blind": 2,
        "starting_stack": 200,
        "your_stack": 185,
        "hand_number": 6,
        "total_hands": 100,
        "round": "post_reveal",
        "your_number": 3,
        "community_number": 5,
        "your_seat": 0,
        "button_seat": 1,
        "pot": 32,
        "to_call": 18,
        "min_raise_to": 36,
        "max_raise_to": 185,
        "legal_actions": ["fold", "call", "raise"],
        "players": [
            {
                "seat": 0,
                "name": "you",
                "folded": False,
                "chip_delta": -8,
                "bet_this_round": 0,
                "stack": 185,
                "all_in": False,
                "busted": False,
            },
            {
                "seat": 1,
                "name": "Gaston",
                "folded": False,
                "chip_delta": 8,
                "bet_this_round": 18,
                "stack": 183,
                "all_in": False,
                "busted": False,
            },
        ],
        "current_hand_actions": [
            {"round": "pre_reveal", "seat": 1, "action": "raise", "amount": 7},
            {"round": "pre_reveal", "seat": 0, "action": "call", "amount": 7},
            {"round": "post_reveal", "seat": 0, "action": "check"},
            {"round": "post_reveal", "seat": 1, "action": "bet", "amount": 18},
        ],
        "recent_hands": [],
    }
    payload.update(overrides)
    return payload


def test_move_returns_legal_action_with_amount_in_range():
    payload = make_move_payload(
        your_number=11,
        community_number=2,
        legal_actions=["fold", "call", "raise"],
        min_raise_to=36,
        max_raise_to=185,
    )
    response = client.post("/move", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["action"] in payload["legal_actions"]
    if body["action"] in ("bet", "raise"):
        assert isinstance(body["amount"], int)
        assert payload["min_raise_to"] <= body["amount"] <= payload["max_raise_to"]
    else:
        assert "amount" not in body


def test_move_weak_hand_folds_to_big_bet():
    payload = make_move_payload(
        your_number=2,
        community_number=9,
        to_call=150,
        pot=150,
        your_stack=180,
        min_raise_to=150,
        max_raise_to=180,
        legal_actions=["fold", "call", "raise"],
    )
    response = client.post("/move", json=payload)
    assert response.status_code == 200
    assert response.json()["action"] == "fold"


def test_move_pair_never_folds():
    payload = make_move_payload(
        your_number=5,
        community_number=5,
        to_call=18,
        legal_actions=["fold", "call", "raise"],
    )
    response = client.post("/move", json=payload)
    assert response.status_code == 200
    assert response.json()["action"] in ("call", "raise")


def test_move_checks_when_free_pre_reveal():
    payload = make_move_payload(
        round="pre_reveal",
        your_number=4,
        community_number=None,
        to_call=0,
        pot=3,
        min_raise_to=4,
        max_raise_to=185,
        legal_actions=["check", "raise"],
        players=[
            {
                "seat": 0,
                "name": "you",
                "folded": False,
                "chip_delta": -8,
                "bet_this_round": 2,
                "stack": 185,
                "all_in": False,
                "busted": False,
            },
            {
                "seat": 1,
                "name": "Gaston",
                "folded": False,
                "chip_delta": 8,
                "bet_this_round": 2,
                "stack": 198,
                "all_in": False,
                "busted": False,
            },
        ],
    )
    response = client.post("/move", json=payload)
    assert response.status_code == 200
    assert response.json()["action"] == "check"


def test_move_unknown_table_rule_stays_conservative():
    payload = make_move_payload(
        table_rule="mystery",
        your_number=13,
        community_number=13,
        to_call=60,
        pot=20,
        your_stack=180,
        legal_actions=["fold", "call", "raise"],
    )
    response = client.post("/move", json=payload)
    assert response.status_code == 200
    assert response.json()["action"] in ("fold", "call")


def test_move_ignores_unknown_fields():
    payload = make_move_payload(extra_future_field={"anything": True})
    response = client.post("/move", json=payload)
    assert response.status_code == 200


def test_move_legal_action_guard():
    payload = make_move_payload(
        your_number=2,
        community_number=9,
        to_call=150,
        pot=150,
        legal_actions=["call"],
    )
    response = client.post("/move", json=payload)
    assert response.status_code == 200
    assert response.json()["action"] == "call"
