from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def case_1():
    return {
        "start_coordinate": [0, 0],
        "end_coordinate": [3, 1],
        "start_time": "2026-06-10T08:30:00Z",
        "nodes": [[0, 0], [1, 0], [2, 0], [2, 1], [3, 1]],
        "edges": [
            {"edge_id": "edge_0", "node1": [0, 0], "node2": [1, 0], "base_duration_sec": 60},
            {"edge_id": "edge_1", "node1": [1, 0], "node2": [2, 0], "base_duration_sec": 60},
            {"edge_id": "edge_2", "node1": [2, 0], "node2": [2, 1], "base_duration_sec": 40},
            {"edge_id": "edge_3", "node1": [2, 1], "node2": [3, 1], "base_duration_sec": 50},
            {"edge_id": "edge_4", "node1": [1, 0], "node2": [2, 1], "base_duration_sec": 120},
        ],
        "obstructions": [
            {
                "edge_id": "edge_1",
                "edge": {"from": [1, 0], "to": [2, 0]},
                "start_time": "2026-06-10T08:00:00Z",
                "end_time": "2026-06-10T09:00:00Z",
                "speed_factor": 0.5,
            },
            {
                "edge_id": "edge_2",
                "edge": {"from": [2, 1], "to": [2, 0]},
                "start_time": "2026-06-10T08:15:00Z",
                "end_time": "2026-06-10T08:45:00Z",
                "speed_factor": 0.0,
            },
        ],
    }


def test_example_1():
    response = client.post("/kan-cheong-delivery-driver", json={"case_1": case_1()})
    assert response.status_code == 200
    result = response.json()["case_1"]
    assert result["total_duration_sec"] == 230
    assert result["arrival_time"] == "2026-06-10T08:33:50Z"
    assert result["path"] == ["edge_0", "edge_4", "edge_3"]


def test_example_2_unreachable():
    case = case_1()
    case["end_coordinate"] = [3, 3]
    response = client.post("/kan-cheong-delivery-driver", json={"case_2": case})
    assert response.status_code == 200
    result = response.json()["case_2"]
    assert result == {"total_duration_sec": None, "arrival_time": None, "path": []}


def test_example_3_cycling_no_wait():
    case = {
        "start_coordinate": [0, 0],
        "end_coordinate": [2, 0],
        "start_time": "2026-06-10T08:30:00Z",
        "nodes": [[0, 0], [1, 0], [2, 0]],
        "edges": [
            {"edge_id": "edge_0", "node1": [0, 0], "node2": [1, 0], "base_duration_sec": 10},
            {"edge_id": "edge_1", "node1": [1, 0], "node2": [2, 0], "base_duration_sec": 10},
            {"edge_id": "edge_2", "node1": [0, 0], "node2": [2, 0], "base_duration_sec": 20},
        ],
        "obstructions": [
            {
                "edge_id": "edge_1",
                "edge": {"from": [1, 0], "to": [2, 0]},
                "start_time": "2026-06-10T08:30:10Z",
                "end_time": "2026-06-10T08:30:20Z",
                "speed_factor": 0.0,
            },
            {
                "edge_id": "edge_1",
                "edge": {"from": [1, 0], "to": [2, 0]},
                "start_time": "2026-06-10T08:30:30Z",
                "end_time": "2026-06-10T08:30:40Z",
                "speed_factor": 0.0,
            },
            {
                "edge_id": "edge_2",
                "edge": {"from": [0, 0], "to": [2, 0]},
                "start_time": "2026-06-10T08:30:00Z",
                "end_time": "2026-06-10T08:32:00Z",
                "speed_factor": 0.2,
            },
        ],
    }
    response = client.post("/kan-cheong-delivery-driver", json={"case_3": case})
    assert response.status_code == 200
    result = response.json()["case_3"]
    assert result["total_duration_sec"] == 60
    assert result["arrival_time"] == "2026-06-10T08:31:00Z"
    assert result["path"] == ["edge_0", "edge_0", "edge_0", "edge_0", "edge_0", "edge_1"]


def test_example_4_blocked_at_start():
    case = {
        "start_coordinate": [0, 0],
        "end_coordinate": [1, 0],
        "start_time": "2026-06-10T08:30:00Z",
        "nodes": [[0, 0], [1, 0]],
        "edges": [
            {"edge_id": "edge_0", "node1": [0, 0], "node2": [1, 0], "base_duration_sec": 60},
        ],
        "obstructions": [
            {
                "edge_id": "edge_0",
                "edge": {"from": [0, 0], "to": [1, 0]},
                "start_time": "2026-06-10T08:00:00Z",
                "end_time": "2026-06-10T09:00:00Z",
                "speed_factor": 0.0,
            },
        ],
    }
    response = client.post("/kan-cheong-delivery-driver", json={"case_4": case})
    assert response.status_code == 200
    assert response.json()["case_4"] == {"total_duration_sec": None, "arrival_time": None, "path": []}


def test_batch_request_matches_shape():
    response = client.post(
        "/kan-cheong-delivery-driver",
        json={"a": case_1(), "b": case_1()},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"a", "b"}
    assert body["a"]["total_duration_sec"] == 230
    assert body["b"]["total_duration_sec"] == 230


def test_start_equals_end():
    case = case_1()
    case["end_coordinate"] = [0, 0]
    response = client.post("/kan-cheong-delivery-driver", json={"case_5": case})
    assert response.status_code == 200
    result = response.json()["case_5"]
    assert result["total_duration_sec"] == 0
    assert result["arrival_time"] == "2026-06-10T08:30:00Z"
    assert result["path"] == []
