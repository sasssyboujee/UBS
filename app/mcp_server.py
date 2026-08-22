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
            "Evaluate an arithmetic expression with correct operator precedence "
            "(* and / are evaluated before + and -). Pass the whole expression "
            "exactly as written, e.g. for 'What is 2 + 3 * 5?' call "
            "calculate with expression='2 + 3 * 5' (the answer is 17, not 25). "
            "Supports integers, +, -, *, /, parentheses, and unary minus."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": (
                        "The full arithmetic expression to evaluate, "
                        "e.g. '2 + 3 * 5' or '(4 + 6) / 2'."
                    ),
                },
            },
            "required": ["expression"],
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


class _ExpressionParser:
    """Tiny recursive-descent parser for + - * / with parentheses and unary minus."""

    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str | None:
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    def parse(self) -> int | float:
        if not self.tokens:
            raise ValueError("empty expression")
        value = self.parse_expression()
        if self.peek() is not None:
            raise ValueError(f"unexpected token {self.peek()!r}")
        return value

    def parse_expression(self) -> int | float:
        value = self.parse_term()
        while self.peek() in ("+", "-"):
            operator = self.next()
            rhs = self.parse_term()
            if operator == "+":
                value += rhs
            else:
                value -= rhs
        return value

    def parse_term(self) -> int | float:
        value = self.parse_factor()
        while self.peek() in ("*", "/"):
            operator = self.next()
            rhs = self.parse_factor()
            if operator == "*":
                value *= rhs
            else:
                if rhs == 0:
                    raise ValueError("division by zero")
                value /= rhs
        return value

    def parse_factor(self) -> int | float:
        token = self.peek()
        if token in ("+", "-"):
            self.next()
            value = self.parse_factor()
            return -value if token == "-" else value
        if token == "(":
            self.next()
            value = self.parse_expression()
            if self.next() != ")":
                raise ValueError("missing closing parenthesis")
            return value
        if token is None:
            raise ValueError("unexpected end of expression")
        if token.replace(".", "", 1).isdigit():
            self.next()
            return float(token) if "." in token else int(token)
        raise ValueError(f"unexpected token {token!r}")


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char in "+-*/()":
            tokens.append(char)
            index += 1
            continue
        if char.isdigit() or char == ".":
            end = index
            while end < len(expression) and (expression[end].isdigit() or expression[end] == "."):
                end += 1
            tokens.append(expression[index:end])
            index = end
            continue
        raise ValueError(f"unexpected character {char!r}")
    return tokens


def _format_number(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.10f}".rstrip("0").rstrip(".")


def _evaluate_expression(expression: str) -> str:
    sanitized = "".join(
        char for char in expression if char.isdigit() or char in "+-*/(). "
    )
    if not sanitized.strip():
        raise ValueError("empty expression")
    parser = _ExpressionParser(_tokenize(sanitized))
    return _format_number(parser.parse())


def _calculate(arguments: dict[str, Any]) -> tuple[str, bool]:
    expression = arguments.get("expression")
    if isinstance(expression, str) and expression.strip():
        try:
            return _evaluate_expression(expression), False
        except ValueError as exc:
            return f"Error: {exc}", True

    # Legacy fallback: a single binary operation, in case the agent still
    # passes the old a/b/operator shape.
    a = arguments.get("a")
    b = arguments.get("b")
    operator = arguments.get("operator")
    if a is not None and b is not None and operator is not None:
        return _calculate_binary(a, b, operator)

    return (
        "Error: pass the full arithmetic expression in the 'expression' parameter, e.g. calculate(expression='2 + 3 * 5')",
        True,
    )


def _calculate_binary(a: Any, b: Any, operator: Any) -> tuple[str, bool]:
    try:
        a_int = int(a)
        b_int = int(b)
    except (TypeError, ValueError):
        return "Error: a and b must be integers", True

    op = str(operator)
    if not -100 <= a_int <= 100 or not -100 <= b_int <= 100:
        return "Error: operands must be between -100 and 100", True

    if op == "+":
        result: int | float = a_int + b_int
    elif op == "-":
        result = a_int - b_int
    elif op == "*":
        result = a_int * b_int
    elif op == "/":
        if b_int == 0:
            return "Error: division by zero", True
        result = a_int / b_int
    else:
        return f"Error: unsupported operator {op!r}", True

    return _format_number(result), False


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
