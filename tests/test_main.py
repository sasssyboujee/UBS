import base64
import json

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


def solve_inner_payload():
    return {
        "adaptInput": {
            "user": {"id": "U7", "fullName": "John Smith"},
            "action": "UPDATE",
            "metadata": {"priority": "MEDIUM"},
        }
    }


def test_solve_unpadded_base64():
    encoded = base64.b64encode(json.dumps(solve_inner_payload()).encode()).decode().rstrip("=")
    response = client.post("/solve", json={"payload": encoded})
    assert response.status_code == 200
    assert response.json() == {
        "adaptOutput": {
            "id": "U7",
            "name": "John Smith",
            "action": "update",
            "priority": 2,
        }
    }


def test_solve_urlsafe_base64():
    encoded = base64.urlsafe_b64encode(json.dumps(solve_inner_payload()).encode()).decode()
    response = client.post("/solve", json={"payload": encoded})
    assert response.status_code == 200
    assert response.json()["adaptOutput"]["priority"] == 2


def test_solve_raw_json_fallback():
    raw = json.dumps(solve_inner_payload())
    response = client.post("/solve", json={"payload": raw})
    assert response.status_code == 200
    assert response.json()["adaptOutput"]["name"] == "John Smith"


def test_solve_low_priority_and_mixed_case_action():
    inner = solve_inner_payload()
    inner["adaptInput"]["action"] = "DeLeTe"
    inner["adaptInput"]["metadata"]["priority"] = "low"
    encoded = base64.b64encode(json.dumps(inner).encode()).decode()
    response = client.post("/solve", json={"payload": encoded})
    assert response.status_code == 200
    assert response.json() == {
        "adaptOutput": {
            "id": "U7",
            "name": "John Smith",
            "action": "delete",
            "priority": 1,
        }
    }


def test_solve_invalid_payload_returns_400():
    response = client.post("/solve", json={"payload": "not-a-valid-payload!!"})
    assert response.status_code == 400


SAMPLE_PHASE2_PAYLOAD = (
    "ewoJImFkYXB0SW5wdXQiOiB7CgkJInVzZXIiOiB7CgkJCSJpZCI6ICJVNDIiLAoJCQkiZnVsbE5hbWUiOiAiSmFuZSBEb2UiCgkJfSwKCQkiYWN0aW9uIjogIkNSRUFURSIsCgkJIm1ldGFkYXRhIjogewoJCQkicHJpb3JpdHkiOiAiSElHSCIKCQl9Cgl9LAoJImhlYXJ0YmVhdHMiOiBbCgkJewoJCQkic2VydmljZSI6ICJhdXRoIiwKCQkJInRpbWVzdGFtcCI6IDE3MTAwMDAxMjMsCgkJCSJsYXRlbmN5TXMiOiAxMjAsCgkJCSJzdGF0dXMiOiAiT0siCgkJfSwKCQl7CgkJCSJzZXJ2aWNlIjogImF1dGgiLAoJCQkidGltZXN0YW1wIjogMTcxMDAwMDEyNSwKCQkJImxhdGVuY3lNcyI6IDE4MCwKCQkJInN0YXR1cyI6ICJGQUlMIgoJCX0sCgkJewoJCQkic2VydmljZSI6ICJhdXRoIiwKCQkJInRpbWVzdGFtcCI6IDE3MTAwMDAxMjEsCgkJCSJsYXRlbmN5TXMiOiA5NSwKCQkJInN0YXR1cyI6ICJPSyIKCQl9CgldLAoJInNsb1F1ZXJ5IjogewoJCSJzZXJ2aWNlIjogImF1dGgiLAoJCSJzaW5jZSI6IDE3MTAwMDAxMjMKCX0KfQ=="
)


def test_solve_phase2_sample_from_guide():
    response = client.post("/solve", json={"payload": SAMPLE_PHASE2_PAYLOAD})
    assert response.status_code == 200
    assert response.json() == {
        "adaptOutput": {
            "id": "U42",
            "name": "Jane Doe",
            "action": "create",
            "priority": 3,
        },
        "sloOutput": {"availability": 0.5, "p95LatencyMs": 180},
    }


def phase2_inner_payload(**overrides):
    inner = {
        "adaptInput": {
            "user": {"id": "U7", "fullName": "John Smith"},
            "action": "UPDATE",
            "metadata": {"priority": "MEDIUM"},
        },
        "heartbeats": [
            {"service": "auth", "timestamp": 1710000123, "latencyMs": 120, "status": "OK"},
            {"service": "auth", "timestamp": 1710000125, "latencyMs": 180, "status": "FAIL"},
            {"service": "auth", "timestamp": 1710000121, "latencyMs": 95, "status": "OK"},
        ],
        "sloQuery": {"service": "auth", "since": 1710000123},
    }
    inner.update(overrides)
    return inner


def solve_phase2(inner):
    encoded = base64.b64encode(json.dumps(inner).encode()).decode()
    return client.post("/solve", json={"payload": encoded})


def test_solve_phase2_filters_by_service_and_since():
    inner = phase2_inner_payload(
        heartbeats=[
            {"service": "auth", "timestamp": 1710000123, "latencyMs": 120, "status": "OK"},
            {"service": "db", "timestamp": 1710000125, "latencyMs": 999, "status": "OK"},
            {"service": "auth", "timestamp": 1710000121, "latencyMs": 95, "status": "FAIL"},
        ]
    )
    response = solve_phase2(inner)
    assert response.status_code == 200
    # Window: only the auth heartbeat at exactly `since` (boundary inclusive).
    assert response.json()["sloOutput"] == {"availability": 1.0, "p95LatencyMs": 120}


def test_solve_phase2_empty_window_returns_zero_slo():
    inner = phase2_inner_payload(
        heartbeats=[
            {"service": "auth", "timestamp": 1710000121, "latencyMs": 95, "status": "OK"},
        ],
        sloQuery={"service": "auth", "since": 1710000123},
    )
    response = solve_phase2(inner)
    assert response.status_code == 200
    assert response.json()["sloOutput"] == {"availability": 0.0, "p95LatencyMs": 0}


def test_solve_phase2_p95_uses_nearest_rank():
    heartbeats = [
        {"service": "auth", "timestamp": 1710000123 + i, "latencyMs": i + 1, "status": "OK"}
        for i in range(10)
    ]
    inner = phase2_inner_payload(heartbeats=heartbeats)
    response = solve_phase2(inner)
    assert response.status_code == 200
    # Sorted latencies 1..10, nearest-rank p95 index ceil(0.95*10)-1 = 9 -> 10.
    assert response.json()["sloOutput"] == {"availability": 1.0, "p95LatencyMs": 10}


def test_solve_phase2_availability_is_fraction_of_window():
    heartbeats = [
        {"service": "auth", "timestamp": 1710000123, "latencyMs": 10, "status": "OK"},
        {"service": "auth", "timestamp": 1710000124, "latencyMs": 20, "status": "FAIL"},
        {"service": "auth", "timestamp": 1710000125, "latencyMs": 30, "status": "FAIL"},
    ]
    inner = phase2_inner_payload(heartbeats=heartbeats)
    response = solve_phase2(inner)
    assert response.status_code == 200
    slo = response.json()["sloOutput"]
    assert abs(slo["availability"] - 1 / 3) < 1e-9
    assert slo["p95LatencyMs"] == 30


def test_solve_phase1_payload_keeps_legacy_shape():
    encoded = base64.b64encode(json.dumps(solve_inner_payload()).encode()).decode()
    response = client.post("/solve", json={"payload": encoded})
    assert response.status_code == 200
    assert "sloOutput" not in response.json()


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


def test_move_unknown_table_rule_shoves_pair_post_reveal():
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
    body = response.json()
    # Gambling phase: a post-reveal pair is always a shove.
    assert body["action"] == "raise"
    assert payload["min_raise_to"] <= body["amount"] <= payload["max_raise_to"]


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
