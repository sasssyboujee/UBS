import base64

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def rpc(method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body)


def tool_result(response):
    return response.json()["result"]["content"][0]["text"]


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


def test_mcp_get_info():
    response = client.get("/mcp")
    assert response.status_code == 200
    assert response.json()["service"] == "nursery-mcp"


def test_mcp_initialize():
    response = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"]["tools"] == {}
    assert result["serverInfo"]["name"] == "nursery-toolbox"


def test_mcp_notification_returns_202():
    response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202


def test_mcp_tools_list():
    response = rpc("tools/list")
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {"get_name", "calculate", "classify_shape"}


def test_mcp_get_name():
    response = rpc("tools/call", {"name": "get_name", "arguments": {}})
    assert response.status_code == 200
    name = tool_result(response)
    assert 3 <= len(name) <= 30
    assert all(c.isalnum() or c in " _-'" for c in name)


def test_mcp_calculate():
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "2 + 2 + 5"}})) == "9"
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "2 + 3 * 5"}})) == "17"
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "5 * 20 / 10"}})) == "10"
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "-9 * 2 + 2"}})) == "-16"
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "7 / 2"}})) == "3.5"
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "4 / 2"}})) == "2"
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "(2 + 3) * 5"}})) == "25"


def test_mcp_calculate_ignores_question_text():
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"expression": "What is 2 + 3 * 5?"}})) == "17"


def test_mcp_calculate_legacy_binary_fallback():
    assert tool_result(rpc("tools/call", {"name": "calculate", "arguments": {"a": 2, "b": 2, "operator": "+"}})) == "4"


def test_mcp_calculate_division_by_zero_is_error():
    response = rpc("tools/call", {"name": "calculate", "arguments": {"expression": "4 / 0"}})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "division by zero" in result["content"][0]["text"]


def test_mcp_calculate_missing_expression_is_error():
    response = rpc("tools/call", {"name": "calculate", "arguments": {}})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "expression" in result["content"][0]["text"]


def test_mcp_classify_rectangle():
    response = rpc("tools/call", {"name": "classify_shape", "arguments": {"image": png_b64("rectangle")}})
    assert response.status_code == 200
    assert tool_result(response) == "rectangle"


def test_mcp_classify_triangle():
    response = rpc("tools/call", {"name": "classify_shape", "arguments": {"image": png_b64("triangle")}})
    assert response.status_code == 200
    assert tool_result(response) == "triangle"


def test_mcp_classify_circle():
    response = rpc("tools/call", {"name": "classify_shape", "arguments": {"image": png_b64("circle")}})
    assert response.status_code == 200
    assert tool_result(response) == "circle"


def test_mcp_classify_sample_square_as_rectangle():
    sample = (
        "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAIAAAD/gAIDAAAB2ElEQVR4nO3c226CQBRG4U0hKHe+/zNioiaK4IFeNGnStE3mNzO4mVnflReCdnU24iFU8zwbwnwE3g/E0mS+sk6nU8S9VRyzil5Z5/M50Z7fsLLmea6qylboDSurWmepPMcwHWIJiJUy1u12M8eSvl5xniUofQz7vg+/MyvL68rqlX+jQ6wsQenHLAmxBMQqKdbz+VzssTjAl7SylkQsAbEExBIQS0AsAbEExBIQS0AsAbEExBIQS9BYYk3Xmj/3YfIYy8zq+8M8eTT1axsyhgJiCYhlpcea7kk+mM8zVtsk+bvyjJUIsQTEEhBLQKyVx+q6brGzgdXHGoZhsbMByfufQWq73S7WrvKPdTgcYu0q/1gREUtALAGxBMQSEEtArLxiTQ7e6KwmVuvgjc4XL89jFXKONcWe35xjtbHnN89YE1+FheOrsMiu16u6SZ5jGGK73Zqo3FgvIJaAWAJiCYglIJaAWHnFmtx8ntV4/in1l7qpnfw0nEsV5DWGfhBLQCwBsRLHGsfRluLqOtmhsY7H4/ftzWazWFZX1xbm1EHAMUtALAGxfrhcLvY/jll/G8fx9+sYsQSMoRhrv98rm5SLMRQwhgJiCYglIJaAWAJiCYglIJaF+wQjHnMtvKco2QAAAABJRU5ErkJggg=="
    )
    response = rpc("tools/call", {"name": "classify_shape", "arguments": {"image": sample}})
    assert response.status_code == 200
    assert tool_result(response) == "rectangle"


def test_mcp_unknown_method_returns_jsonrpc_error():
    response = rpc("no/such/method")
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601


def test_mcp_invalid_json_returns_400():
    response = client.post("/mcp", content="not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
