"""Minimal MCP (Model Context Protocol) server for the Tool-box / Nursery challenge.

Exposes three tools over the Streamable HTTP transport at POST /mcp:

- get_name:        returns the assistant's name (a string)
- calculate:       arithmetic on two integers in [-100, 100]
- classify_shape:  classifies a base64 PNG as rectangle, triangle, or circle

The server is stateless: no sessions, no server-initiated messages. It also
answers GET /mcp with an SSE stream (endpoint event) for clients that use the
older GET-first handshake, and POST responses can be SSE-encoded when the
client asks for text/event-stream.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

SERVER_INFO = {"name": "nursery-toolbox", "version": "1.0.0"}
PROTOCOL_VERSION = "2025-03-26"
BOT_NAME = "Nursery"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_name",
        "description": "Return the name of this assistant.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "calculate",
        "description": (
            "Perform an arithmetic operation on two integers between -100 and 100. "
            "operator is one of +, -, *, /."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "minimum": -100, "maximum": 100},
                "b": {"type": "integer", "minimum": -100, "maximum": 100},
                "operator": {"type": "string", "enum": ["+", "-", "*", "/"]},
            },
            "required": ["a", "b", "operator"],
        },
    },
    {
        "name": "classify_shape",
        "description": (
            "Classify a base64-encoded PNG image as one of: rectangle, triangle, circle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Base64-encoded PNG image",
                },
            },
            "required": ["image"],
        },
    },
]


def _calculate(arguments: dict[str, Any]) -> tuple[str, bool]:
    try:
        a = int(arguments.get("a"))
        b = int(arguments.get("b"))
    except (TypeError, ValueError):
        return "Error: a and b must be integers", True

    operator = str(arguments.get("operator", ""))
    if not -100 <= a <= 100 or not -100 <= b <= 100:
        return "Error: operands must be between -100 and 100", True

    if operator == "+":
        result: int | float = a + b
    elif operator == "-":
        result = a - b
    elif operator == "*":
        result = a * b
    elif operator == "/":
        if b == 0:
            return "Error: division by zero", True
        result = a / b
    else:
        return f"Error: unsupported operator {operator!r}", True

    if isinstance(result, float) and result.is_integer():
        return str(int(result)), False
    return str(result), False


def _classify(arguments: dict[str, Any]) -> tuple[str, bool]:
    raw = arguments.get("image")
    if not isinstance(raw, str) or not raw:
        return "Error: image must be a base64 string", True

    try:
        data = base64.b64decode(raw)
    except ValueError:
        return "Error: invalid base64 image", True

    try:
        import cv2
        import numpy as np
    except ImportError:
        return "Error: image processing unavailable", True

    array = np.frombuffer(data, np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return "Error: could not decode image", True

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Assume the shape is the non-background color; pick polarity via mean.
    if float(gray.mean()) > 127:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return "Error: no shape found", True

    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    perimeter = float(cv2.arcLength(contour, True))
    if area <= 0 or perimeter <= 0:
        return "Error: shape too small", True

    approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
    vertices = len(approx)
    circularity = 4.0 * np.pi * area / (perimeter * perimeter)

    if vertices == 3:
        shape = "triangle"
    elif vertices == 4:
        shape = "rectangle"
    elif circularity >= 0.85:
        shape = "circle"
    elif circularity >= 0.68:
        shape = "rectangle"
    else:
        shape = "triangle"
    return shape, False


def call_tool(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Execute a tool and return (text, is_error)."""
    if name == "get_name":
        return BOT_NAME, False
    if name == "calculate":
        return _calculate(arguments)
    if name == "classify_shape":
        return _classify(arguments)
    return f"Error: unknown tool {name!r}", True


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_rpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    msg_id = msg.get("id")
    method = str(msg.get("method", ""))
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(
            msg_id,
            {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _err(msg_id, -32602, "arguments must be an object")
        text, is_error = call_tool(name, arguments)
        return _ok(
            msg_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )
    if method == "resources/list":
        return _ok(msg_id, {"resources": []})
    if method == "prompts/list":
        return _ok(msg_id, {"prompts": []})
    if method.startswith("notifications/"):
        return None
    return _err(msg_id, -32601, f"Method not found: {method}")


def handle_rpc(body: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Handle a JSON-RPC request body (single message or batch)."""
    if isinstance(body, list):
        responses = [r for msg in body if isinstance(msg, dict) and (r := handle_rpc_message(msg)) is not None]
        return responses or None
    if isinstance(body, dict):
        return handle_rpc_message(body)
    return _err(None, -32600, "Invalid Request")


def sse_encode(payload: dict[str, Any] | list[dict[str, Any]]) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: message\ndata: {data}\n\n"


async def sse_response_stream(payload: dict[str, Any] | list[dict[str, Any]]):
    yield sse_encode(payload)


async def sse_endpoint_stream():
    """GET /mcp SSE stream: announce the POST endpoint, then keepalive."""
    yield "event: endpoint\ndata: /mcp\n\n"
    while True:
        await asyncio.sleep(15)
        yield ": keepalive\n\n"
