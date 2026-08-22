from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def reset():
    response = client.post("/ghost-chains/reset", json={"clearTransactions": True})
    assert response.status_code == 200
    assert response.json() == {"clearTransactions": True}


def post_transactions(transactions):
    response = client.post("/ghost-chains/transactions", json={"transactions": transactions})
    assert response.status_code == 200
    return response.json()["transactions"]


def make_tx(tx_id, from_user, to_user, created_at, amount=100.0, **extra):
    tx = {
        "txId": tx_id,
        "fromUserId": from_user,
        "toUserId": to_user,
        "amount": amount,
        "createdAt": created_at,
    }
    tx.update(extra)
    return tx


def timestamp(minutes_after_t0):
    dt = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC) + timedelta(minutes=minutes_after_t0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_health():
    response = client.get("/ghost-chains/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_transactions_echo_order_and_score_range():
    reset()
    results = post_transactions([
        make_tx("t1", "a", "b", timestamp(0)),
        make_tx("t2", "b", "c", timestamp(1)),
        make_tx("t3", "c", "d", timestamp(2)),
    ])
    assert [r["txId"] for r in results] == ["t1", "t2", "t3"]
    assert all(0.0 <= r["riskScore"] <= 1.0 for r in results)


def test_structural_ordering_examples():
    reset()
    example_sequences = [
        # Example 1 - isolated
        [("m", "a")],
        # Example 2 - extension
        [("m", "a"), ("a", "c")],
        # Example 3 - convergence
        [("m", "a"), ("m", "h"), ("a", "s"), ("h", "s")],
        # Example 4 - return
        [("m", "a"), ("a", "c"), ("c", "o"), ("o", "a")],
        # Example 5 - multi-loop
        [("m", "a"), ("a", "c"), ("c", "m"), ("a", "n"), ("n", "m")],
    ]

    final_scores = []
    for edges in example_sequences:
        reset()
        results = post_transactions([
            make_tx(f"tx_{i}", u, v, timestamp(i)) for i, (u, v) in enumerate(edges)
        ])
        final_scores.append(results[-1]["riskScore"])

    e1, e2, e3, e4, e5 = final_scores
    assert e1 < e2 < e3 < e4 < e5
    assert e1 == 0.0
    assert e4 - e2 > 0.5
    assert e5 - e4 > 0.05


def test_batch_processing_is_sequential():
    reset()
    # A -> B followed by B -> A closes a cycle and must score higher than an
    # isolated first edge.
    results = post_transactions([
        make_tx("t1", "a", "b", timestamp(0)),
        make_tx("t2", "b", "a", timestamp(1)),
    ])
    assert results[0]["riskScore"] == 0.0
    assert results[1]["riskScore"] > 0.5


def test_idempotency_duplicate_returns_original_score_and_no_mutation():
    reset()
    first = post_transactions([
        make_tx("t1", "a", "b", timestamp(0)),
        make_tx("t2", "b", "a", timestamp(1)),
    ])

    # Duplicate txId with identical payload: original score, no state change.
    dup = post_transactions([make_tx("t1", "a", "b", timestamp(0))])
    assert dup[0]["riskScore"] == first[0]["riskScore"]

    # The next transaction must see the same graph as without the duplicate.
    results = post_transactions([make_tx("t3", "c", "a", timestamp(2))])
    # C -> A after A<->B: C is new, A already has edges from/to B.
    # With the duplicate ignored the score is identical to a clean run.
    reset()
    clean = post_transactions([
        make_tx("t1", "a", "b", timestamp(0)),
        make_tx("t2", "b", "a", timestamp(1)),
        make_tx("t3", "c", "a", timestamp(2)),
    ])
    assert results[0]["riskScore"] == clean[2]["riskScore"]


def test_idempotency_conflicting_payload_does_not_mutate():
    reset()
    first = post_transactions([make_tx("t1", "a", "b", timestamp(0))])
    # Same txId with a different amount: ignored, original score returned.
    conflicting = post_transactions([make_tx("t1", "a", "b", timestamp(0), amount=999.0)])
    assert conflicting[0]["riskScore"] == first[0]["riskScore"]
    # State unchanged: A -> B still the only edge, so B -> A is a cycle.
    cycle = post_transactions([make_tx("t2", "b", "a", timestamp(1))])
    assert cycle[0]["riskScore"] > 0.5


def test_missing_optionals_and_unknown_fields_do_not_fail():
    reset()
    results = post_transactions([
        {
            "txId": "t1",
            "fromUserId": "a",
            "toUserId": "b",
            "amount": 10.0,
            "createdAt": timestamp(0),
        },
        {
            "txId": "t2",
            "fromUserId": "b",
            "toUserId": "c",
            "amount": 10.0,
            "createdAt": timestamp(1),
            "ipAddress": "203.0.113.7",
            "deviceId": "device-42",
            "futureField": {"anything": True},
        },
    ])
    assert len(results) == 2
    assert all(0.0 <= r["riskScore"] <= 1.0 for r in results)


def test_lookback_exactly_24_hours_is_active():
    reset()
    post_transactions([make_tx("t1", "a", "b", timestamp(0))])
    # Exactly 24 hours later the first edge is still inside the window.
    t2 = timestamp(24 * 60)
    results = post_transactions([make_tx("t2", "b", "a", t2)])
    assert results[0]["riskScore"] > 0.5


def test_lookback_older_than_24_hours_is_expired():
    reset()
    post_transactions([make_tx("t1", "a", "b", timestamp(0))])
    # 24 hours + 1 minute later the first edge is outside the window.
    t2 = timestamp(24 * 60 + 1)
    results = post_transactions([make_tx("t2", "b", "a", t2)])
    assert results[0]["riskScore"] == 0.0


def test_reset_clears_state():
    reset()
    post_transactions([
        make_tx("t1", "a", "b", timestamp(0)),
        make_tx("t2", "b", "a", timestamp(1)),
    ])
    reset()
    # After reset the same first transaction scores 0.0 again.
    results = post_transactions([make_tx("t3", "a", "b", timestamp(2))])
    assert results[0]["riskScore"] == 0.0


# ---------------------------------------------------------------------------
# Phase 2 - identity signal
# ---------------------------------------------------------------------------


def test_consistent_identity_flow_reinforces_structure():
    reset()
    anonymous = post_transactions([
        make_tx("t1", "m", "a", timestamp(0)),
        make_tx("t2", "a", "c", timestamp(1)),
        make_tx("t3", "c", "h", timestamp(2)),
    ])
    reset()
    consistent = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), deviceId="dev_i"),
        make_tx("t2", "a", "c", timestamp(1), deviceId="dev_i"),
        make_tx("t3", "c", "h", timestamp(2), deviceId="dev_i"),
    ])
    # The same structural flow carrying a consistent device is at least as
    # suspicious as an anonymous flow.
    assert consistent[0]["riskScore"] == anonymous[0]["riskScore"] == 0.0
    assert consistent[1]["riskScore"] >= anonymous[1]["riskScore"]
    assert consistent[2]["riskScore"] > anonymous[2]["riskScore"]


def test_identity_shift_mid_flow_scores_higher_than_consistent():
    reset()
    consistent = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), deviceId="dev_i"),
        make_tx("t2", "a", "c", timestamp(1), deviceId="dev_i"),
        make_tx("t3", "c", "h", timestamp(2), deviceId="dev_i"),
    ])
    reset()
    shifted = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), deviceId="dev_i"),
        make_tx("t2", "a", "c", timestamp(1), deviceId="dev_i"),
        make_tx("t3", "c", "h", timestamp(2), deviceId="dev_a"),
    ])
    assert shifted[2]["riskScore"] > consistent[2]["riskScore"]


def test_identity_shift_at_branch_edge():
    reset()
    results = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), deviceId="dev_i"),
        make_tx("t2", "a", "c", timestamp(1), deviceId="dev_i"),
        make_tx("t3", "a", "s", timestamp(2), deviceId="dev_i"),
        make_tx("t4", "c", "o", timestamp(3), deviceId="dev_a"),
    ])
    # The branch that keeps the incoming device is less anomalous than the
    # branch that introduces a new device.
    assert results[3]["riskScore"] > results[2]["riskScore"]


def test_shared_identity_across_disconnected_components():
    reset()
    no_identity = post_transactions([
        make_tx("t1", "m", "a", timestamp(0)),
        make_tx("t2", "c", "h", timestamp(1)),
        make_tx("t3", "o", "s", timestamp(2)),
    ])
    reset()
    shared = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), ipAddress="10.0.0.1"),
        make_tx("t2", "c", "h", timestamp(1), ipAddress="10.0.0.1"),
        make_tx("t3", "o", "s", timestamp(2), ipAddress="10.0.0.1"),
    ])
    # First use of an identity is free; each additional disconnected component
    # that reuses it adds evidence of coordination.
    assert shared[0]["riskScore"] == no_identity[0]["riskScore"] == 0.0
    assert shared[1]["riskScore"] > no_identity[1]["riskScore"]
    assert shared[2]["riskScore"] > shared[1]["riskScore"]


def test_missing_identity_on_connected_path_is_suspicious():
    reset()
    anonymous = post_transactions([
        make_tx("t1", "m", "a", timestamp(0)),
        make_tx("t2", "a", "c", timestamp(1)),
        make_tx("t3", "c", "h", timestamp(2)),
    ])
    reset()
    dropped = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), deviceId="dev_i"),
        make_tx("t2", "a", "c", timestamp(1), deviceId="dev_i"),
        make_tx("t3", "c", "h", timestamp(2)),
    ])
    # Dropping the device on the third leg of a continuous flow is more
    # suspicious than an unrelated missing device.
    assert dropped[2]["riskScore"] > anonymous[2]["riskScore"]


def test_identity_dimensions_are_independent():
    reset()
    ip_shift_only = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), ipAddress="ip_x", deviceId="dev_p"),
        make_tx("t2", "a", "c", timestamp(1), ipAddress="ip_x", deviceId="dev_p"),
        make_tx("t3", "c", "h", timestamp(2), ipAddress="ip_y", deviceId="dev_p"),
    ])
    reset()
    both_shift = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), ipAddress="ip_x", deviceId="dev_p"),
        make_tx("t2", "a", "c", timestamp(1), ipAddress="ip_x", deviceId="dev_p"),
        make_tx("t3", "c", "h", timestamp(2), ipAddress="ip_y", deviceId="dev_q"),
    ])
    # ip and device are independent dimensions: shifting both is stronger.
    assert both_shift[2]["riskScore"] > ip_shift_only[2]["riskScore"]


def test_identity_state_expires_with_the_lookback_window():
    reset()
    post_transactions([
        make_tx("t1", "m", "a", timestamp(0), ipAddress="10.0.0.1"),
        make_tx("t2", "c", "h", timestamp(1), ipAddress="10.0.0.1"),
    ])
    # 24h + 1m later the first edge has expired, so only the second component
    # remains as identity evidence for the incoming edge.
    results = post_transactions([
        make_tx("t3", "o", "s", timestamp(24 * 60 + 1), ipAddress="10.0.0.1"),
    ])

    reset()
    post_transactions([
        make_tx("t2", "c", "h", timestamp(1), ipAddress="10.0.0.1"),
    ])
    fresh = post_transactions([
        make_tx("t3", "o", "s", timestamp(2), ipAddress="10.0.0.1"),
    ])
    assert results[0]["riskScore"] == fresh[0]["riskScore"]


def test_empty_identity_strings_are_treated_as_absent():
    reset()
    with_empty = post_transactions([
        make_tx("t1", "m", "a", timestamp(0), deviceId=""),
        make_tx("t2", "a", "c", timestamp(1), deviceId="   "),
        make_tx("t3", "c", "h", timestamp(2)),
    ])
    reset()
    anonymous = post_transactions([
        make_tx("t1", "m", "a", timestamp(0)),
        make_tx("t2", "a", "c", timestamp(1)),
        make_tx("t3", "c", "h", timestamp(2)),
    ])
    assert [r["riskScore"] for r in with_empty] == [r["riskScore"] for r in anonymous]


def test_reset_clears_identity_state():
    reset()
    post_transactions([
        make_tx("t1", "m", "a", timestamp(0), ipAddress="10.0.0.1"),
    ])
    reset()
    # After reset the same identity is seen for the first time again.
    results = post_transactions([
        make_tx("t2", "c", "h", timestamp(1), ipAddress="10.0.0.1"),
    ])
    assert results[0]["riskScore"] == 0.0
