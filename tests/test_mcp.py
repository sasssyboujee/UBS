import base64

import cv2
import numpy as np


def rpc(client, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        "/mcp",
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
    )


def tool_result(response):
    return response.json()["result"]["content"][0]["text"]


def tool_texts(response):
    return [item["text"] for item in response.json()["result"]["content"] if item["type"] == "text"]


def png_b64(shape):
    image = np.full((100, 100, 3), 255, np.uint8)
    if shape == "rectangle":
        cv2.rectangle(image, (25, 30), (75, 70), (0, 0, 0), -1)
    elif shape == "circle":
        cv2.circle(image, (50, 50), 25, (0, 0, 0), -1)
    elif shape == "triangle":
        points = np.array([[50, 20], [80, 80], [20, 80]], np.int32)
        cv2.fillPoly(image, [points], (0, 0, 0))
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


ALL_TOOLS = {
    "get_name",
    "calculate",
    "classify_shape",
    "recall",
    "retrieve",
    "navigate",
    "route",
    "find_open_venues",
    "venues_open",
    "where_to_eat",
    "find_meeting_window",
    "meeting_window",
    "find_meeting_point",
    "meeting_point",
    "plan_outing",
    "outing",
}


def test_mcp_get_info(client):
    response = client.get("/mcp")
    # Streamable HTTP: GET without an SSE Accept header is refused.
    assert response.status_code == 406


def test_mcp_initialize(client):
    response = rpc(
        client,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "nursery-toolbox"
    assert "tools" in result["capabilities"]


def test_mcp_notification_returns_202(client):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 202


def test_mcp_tools_list(client):
    response = rpc(client, "tools/list")
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == ALL_TOOLS


def test_mcp_get_name(client):
    response = rpc(client, "tools/call", {"name": "get_name", "arguments": {}})
    assert response.status_code == 200
    name = tool_result(response)
    assert 3 <= len(name) <= 30
    assert all(c.isalnum() or c in " _-'" for c in name)


def test_mcp_calculate(client):
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "2 + 2 + 5"}})) == "9"
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "2 + 3 * 5"}})) == "17"
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "5 * 20 / 10"}})) == "10"
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "-9 * 2 + 2"}})) == "-16"
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "7 / 2"}})) == "3.5"
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "4 / 2"}})) == "2"
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "(2 + 3) * 5"}})) == "25"


def test_mcp_calculate_ignores_question_text(client):
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "What is 2 + 3 * 5?"}})) == "17"


def test_mcp_calculate_legacy_binary_fallback(client):
    assert tool_result(rpc(client, "tools/call", {"name": "calculate", "arguments": {"a": 2, "b": 2, "operator": "+"}})) == "4"


def test_mcp_calculate_division_by_zero_is_error(client):
    response = rpc(client, "tools/call", {"name": "calculate", "arguments": {"expression": "4 / 0"}})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "division by zero" in result["content"][0]["text"]


def test_mcp_calculate_missing_expression_is_error(client):
    response = rpc(client, "tools/call", {"name": "calculate", "arguments": {}})
    result = response.json()["result"]
    assert result["isError"] is True


def test_mcp_classify_rectangle(client):
    response = rpc(client, "tools/call", {"name": "classify_shape", "arguments": {"image": png_b64("rectangle")}})
    assert response.status_code == 200
    assert tool_result(response) == "rectangle"


def test_mcp_classify_triangle(client):
    response = rpc(client, "tools/call", {"name": "classify_shape", "arguments": {"image": png_b64("triangle")}})
    assert response.status_code == 200
    assert tool_result(response) == "triangle"


def test_mcp_classify_circle(client):
    response = rpc(client, "tools/call", {"name": "classify_shape", "arguments": {"image": png_b64("circle")}})
    assert response.status_code == 200
    assert tool_result(response) == "circle"


def test_mcp_recall_returns_list_of_strings(client):
    response = rpc(
        client,
        "tools/call",
        {
            "name": "recall",
            "arguments": {
                "question": "When was the sensor grid last brought back into alignment?",
                "materials": "The sensor grid was last brought back into alignment on 14 March.",
            },
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert tool_texts(response) == ["The sensor grid was last brought back into alignment on 14 March."]


def test_mcp_retrieve_is_recall_alias(client):
    response = rpc(
        client,
        "tools/call",
        {
            "name": "retrieve",
            "arguments": {
                "question": "When was the sensor grid last brought back into alignment?",
                "materials": "The sensor grid was last brought back into alignment on 14 March.",
            },
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert tool_texts(response) == ["The sensor grid was last brought back into alignment on 14 March."]


def test_mcp_navigate_returns_next_node(client):
    graph = {
        "adjacency": {"A": {"B": 4.0, "C": 2.0}, "B": {"D": 3.0}, "C": {"D": 2.0}},
        "tolls": {"A": 5.0, "B": 1.0, "C": 9.0, "D": 2.0},
    }
    response = rpc(
        client,
        "tools/call",
        {
            "name": "navigate",
            "arguments": {"map_id": "x", "from_node": "A", "to": "D", "graph": graph},
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "B"


def test_mcp_navigate_missing_args_is_error(client):
    response = rpc(client, "tools/call", {"name": "navigate", "arguments": {"map_id": "x"}})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "from_node" in result["content"][0]["text"]


def test_mcp_unknown_method_returns_jsonrpc_error(client):
    response = rpc(client, "no/such/method")
    assert response.status_code == 200
    assert response.json()["error"]["code"] in (-32601, -32602)


def test_mcp_invalid_json_returns_4xx(client):
    response = client.post(
        "/mcp",
        content="not json",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    assert 400 <= response.status_code < 500
