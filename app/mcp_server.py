from __future__ import annotations

import asyncio
import base64
import json
import heapq
import re
import concurrent.futures
import os
from typing import Any

import httpx
import tiktoken
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

TOKEN_BUDGET = 900
_ENCODING = tiktoken.get_encoding("o200k_base")
PER_DOC_TIMEOUT = 6.0
FETCH_BUDGET = 8.0
DEFAULT_GRAPH_HOST = "https://tool-box-2591eaa24fa3.herokuapp.com"   # as per your challenge

# Study materials – the host is the same, but we'll fetch dynamically via /study-materials list.
# We'll keep the default list as a fallback, but the agent will pass the actual materials.
DEFAULT_STUDY_MATERIALS = [
    {"title": "The Meridian Trench Research Station",
     "url": f"{DEFAULT_GRAPH_HOST}/study-materials/1"},
    {"title": "Ashgrove Metropolitan Transit Authority",
     "url": f"{DEFAULT_GRAPH_HOST}/study-materials/2"},
    {"title": "Velmara Compound Phase II Trial Record",
     "url": f"{DEFAULT_GRAPH_HOST}/study-materials/3"},
    {"title": "Hollowlight Engine Technical Handbook",
     "url": f"{DEFAULT_GRAPH_HOST}/study-materials/4"},
    {"title": "Thornmere Growers Cooperative Yearbook",
     "url": f"{DEFAULT_GRAPH_HOST}/study-materials/5"},
]

# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------

app = FastAPI(title="School Days MCP", version="2.0.1")

SERVER_INFO = {"name": "nursery-toolbox", "version": "2.0.1"}
PROTOCOL_VERSION = "2025-03-26"
BOT_NAME = "Nursery"

# ----------------------------------------------------------------------
# Tool definitions (as required by MCP)
# ----------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_name",
        "description": "Return the name of this assistant.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression with correct precedence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The full arithmetic expression to evaluate.",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "classify_shape",
        "description": "Classify a base64‑encoded PNG as rectangle, triangle, or circle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "Base64‑encoded PNG image"},
            },
            "required": ["image"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Search the study materials and return the passages needed to answer the question(s). "
            "Returns a JSON array of strings (at most 900 tokens total)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question(s) to find material for."},
                "questions": {"type": "array", "items": {"type": "string"}},
                "materials": {
                    "type": ["string", "array", "object"],
                    "description": "Optional study materials; if omitted, the official set is used.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "retrieve",
        "description": "Alias of recall.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "questions": {"type": "array", "items": {"type": "string"}},
                "materials": {"type": ["string", "array", "object"]},
            },
            "required": ["question"],
        },
    },
    {
        "name": "navigate",
        "description": (
            "Return the next node to move to on the least‑cost route to the destination. "
            "The map is fetched automatically using map_id. "
            "Pass 'visited' (all nodes already visited, including current) and 'hops_left' when given."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "map_id": {"type": "string", "description": "The map_id from the question."},
                "from": {"type": "string", "description": "Current node label."},
                "to": {"type": "string", "description": "Destination node label."},
                "hops_left": {"type": "integer", "description": "Edges still allowed, including next move."},
                "visited": {"type": "array", "items": {"type": "string"}},
                "base_url": {"type": "string", "description": "Optional override for graph endpoint."},
                "graph": {"type": "object", "description": "Optional: pre‑fetched graph JSON."},
            },
            "required": ["map_id", "from", "to"],
        },
    },
]

# ----------------------------------------------------------------------
# Caches (documents and graphs, no visited cache)
# ----------------------------------------------------------------------

_doc_cache: dict[str, str] = {}
_graph_cache: dict[str, dict[str, Any]] = {}

# ----------------------------------------------------------------------
# Toolbox: recall
# ----------------------------------------------------------------------

def _token_count(text: str) -> int:
    return len(_ENCODING.encode(text))

def _fetch_document(url: str) -> str | None:
    if url in _doc_cache:
        return _doc_cache[url]
    try:
        resp = httpx.get(url, timeout=PER_DOC_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()
        if isinstance(data, dict):
            text = data.get("content") or data.get("text") or data.get("body") or str(data)
        else:
            text = str(data)
    else:
        text = resp.text

    _doc_cache[url] = text
    return text

def _fetch_many(urls: list[str]) -> None:
    missing = [u for u in urls if u not in _doc_cache]
    if not missing:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(missing)) as pool:
        futures = {pool.submit(_fetch_document, u): u for u in missing}
        concurrent.futures.wait(futures, timeout=FETCH_BUDGET)

def _normalize_material_list(items: list[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            if item.startswith("http"):
                result.append({"title": item, "url": item})
            else:
                result.append({"title": "inline", "text": item})
        elif isinstance(item, dict):
            title = item.get("title") or item.get("url") or "untitled"
            if "url" in item:
                result.append({"title": title, "url": item["url"]})
            elif "text" in item:
                result.append({"title": title, "text": item["text"]})
    return result

def _resolve_materials(materials: Any) -> list[dict[str, str]]:
    if materials is None or materials == "":
        return list(DEFAULT_STUDY_MATERIALS)

    if isinstance(materials, str):
        stripped = materials.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if parsed is not None:
                return _resolve_materials(parsed)

        if stripped.startswith("http://") or stripped.startswith("https://"):
            try:
                resp = httpx.get(stripped, timeout=PER_DOC_TIMEOUT)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ToolboxError(f"failed to fetch materials from {stripped}: {exc}") from exc

            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("documents"), list):
                    return _normalize_material_list(data["documents"])
                if isinstance(data, list):
                    return _normalize_material_list(data)
                return [{"title": stripped, "text": str(data)}]
            return [{"title": stripped, "text": resp.text}]

        # Plain inline text
        return [{"title": "inline", "text": materials}]

    if isinstance(materials, list):
        return _normalize_material_list(materials)

    if isinstance(materials, dict):
        if isinstance(materials.get("documents"), list):
            return _normalize_material_list(materials["documents"])
        result: list[dict[str, str]] = []
        for title, value in materials.items():
            if isinstance(value, str) and value.startswith("http"):
                result.append({"title": title, "url": value})
            else:
                result.append({"title": title, "text": str(value)})
        return result

    raise ToolboxError(f"unsupported materials format: {type(materials)!r}")

def _hard_split_by_tokens(text: str) -> list[str]:
    tokens = _ENCODING.encode(text)
    return [
        _ENCODING.decode(tokens[i : i + TOKEN_BUDGET])
        for i in range(0, len(tokens), TOKEN_BUDGET)
    ]

def _split_paragraphs(text: str) -> list[str]:
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if _token_count(para) <= TOKEN_BUDGET:
            chunks.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if _token_count(sentence) <= TOKEN_BUDGET:
                chunks.append(sentence)
            else:
                chunks.extend(_hard_split_by_tokens(sentence))
    return chunks

def _load_chunks(materials: Any) -> list[str]:
    docs = _resolve_materials(materials)
    urls = [d["url"] for d in docs if "url" in d]
    _fetch_many(urls)

    chunks: list[str] = []
    for doc in docs:
        if "text" in doc:
            text = doc["text"]
        else:
            text = _doc_cache.get(doc["url"])
            if text is None:
                continue
        chunks.extend(_split_paragraphs(text))
    return chunks

def _question_terms(question: Any) -> set[str]:
    if isinstance(question, list):
        terms: set[str] = set()
        for q in question:
            terms |= set(re.findall(r"\w+", str(q).lower()))
        return terms
    return set(re.findall(r"\w+", str(question).lower()))

def _score_chunk(chunk: str, terms: set[str]) -> int:
    chunk_terms = set(re.findall(r"\w+", chunk.lower()))
    return len(terms & chunk_terms)

def recall(question: Any, materials: Any = None) -> list[str]:
    """
    Return the single most relevant passage (<=900 tokens) that answers the question.
    """
    if not question:
        raise ToolboxError("question is required")

    terms = _question_terms(question)
    chunks = _load_chunks(materials)

    scored = sorted(
        ((_score_chunk(c, terms), _token_count(c), c) for c in chunks),
        key=lambda x: (-x[0], x[1]),
    )

    # Return the highest scoring chunk that has any term overlap
    for score, tok, chunk in scored:
        if score > 0:
            return [chunk]

    # If no overlap, return the shortest chunk as a fallback
    if chunks:
        shortest = min(chunks, key=_token_count)
        return [shortest]

    return []

# ----------------------------------------------------------------------
# Toolbox: navigate
# ----------------------------------------------------------------------

class ToolboxError(Exception):
    pass

def _fetch_graph(map_id: str, base_url: str | None) -> dict[str, Any]:
    if map_id.startswith("http://") or map_id.startswith("https://"):
        url = map_id
    else:
        host = (base_url or os.environ.get("GRAPH_BASE_URL") or DEFAULT_GRAPH_HOST).rstrip("/")
        url = f"{host}/graph?map_id={map_id}"

    if url in _graph_cache:
        return _graph_cache[url]

    try:
        resp = httpx.get(url, timeout=8.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolboxError(f"failed to fetch graph {url}: {exc}") from exc

    data = resp.json()
    _graph_cache[url] = data
    return data

def _dijkstra_next(adjacency: dict, tolls: dict, start: str, dest: str, forbidden: set[str]) -> str | None:
    dist = {start: 0.0}
    prev: dict[str, str] = {}
    heap = [(0.0, start)]
    seen: set[str] = set()

    while heap:
        d, u = heapq.heappop(heap)
        if u in seen:
            continue
        seen.add(u)
        if u == dest:
            break
        for v, w in adjacency.get(u, {}).items():
            if v in forbidden and v != dest:
                continue
            cost = d + w + tolls.get(v, 0.0)
            if v not in dist or cost < dist[v]:
                dist[v] = cost
                prev[v] = u
                heapq.heappush(heap, (cost, v))

    if dest not in dist:
        return None

    node = dest
    path = [node]
    while node != start:
        node = prev[node]
        path.append(node)
    path.reverse()
    return path[1] if len(path) > 1 else None

def _bounded_next(adjacency: dict, tolls: dict, start: str, dest: str, max_hops: int, forbidden: set[str]) -> str | None:
    # DP over hop count: reachable[h][node] = (cost, parent)
    reachable = [dict() for _ in range(max_hops + 1)]
    reachable[0][start] = (0.0, None)

    for h in range(1, max_hops + 1):
        # carry over from previous hop (allows using fewer hops)
        reachable[h] = dict(reachable[h-1])
        for u, (cost_u, _) in reachable[h-1].items():
            for v, w in adjacency.get(u, {}).items():
                if v in forbidden and v != dest:
                    continue
                new_cost = cost_u + w + tolls.get(v, 0.0)
                if v not in reachable[h] or new_cost < reachable[h][v][0]:
                    reachable[h][v] = (new_cost, u)

    # Find best (minimum cost) path to destination within hop limit
    best_h = None
    best_cost = None
    for h in range(max_hops + 1):
        if dest in reachable[h]:
            cost = reachable[h][dest][0]
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_h = h

    if best_h is None:
        return None

    # Reconstruct path
    path = []
    node = dest
    h = best_h
    while node != start:
        parent = reachable[h][node][1]
        if parent is None:
            # Node was carried over without moving; go back one hop
            h -= 1
            if h < 0:
                return None  # should not happen
            continue
        path.append(node)
        node = parent
        h -= 1
    path.append(start)
    path.reverse()
    return path[1] if len(path) > 1 else None

def navigate(
    map_id: str,
    current: str,
    destination: str,
    hops_left: int | None = None,
    visited: list[str] | None = None,
    base_url: str | None = None,
    graph: dict[str, Any] | None = None,
) -> tuple[str, list[str], float]:
    if current == destination:
        raise ToolboxError("already at destination")

    graph_data = graph or _fetch_graph(map_id, base_url)
    adjacency = graph_data.get("adjacency", {})
    tolls = graph_data.get("tolls", {})

    # Use only the visited list provided by the agent – no cross‑journey cache.
    forbidden = set(visited or [])
    forbidden.discard(destination)   # destination is always allowed

    if hops_left is not None:
        next_node = _bounded_next(adjacency, tolls, current, destination, hops_left, forbidden)
    else:
        next_node = _dijkstra_next(adjacency, tolls, current, destination, forbidden)

    if next_node is None:
        raise ToolboxError("no valid path found within constraints")

    return next_node, [], 0.0

# ----------------------------------------------------------------------
# calculate and classify_shape (unchanged from your original)
# ----------------------------------------------------------------------

class _ExpressionParser:
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

    # Legacy fallback
    a = arguments.get("a")
    b = arguments.get("b")
    operator = arguments.get("operator")
    if a is not None and b is not None and operator is not None:
        return _calculate_binary(a, b, operator)

    return (
        "Error: pass the full arithmetic expression in the 'expression' parameter",
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
        result = a_int + b_int
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

# ----------------------------------------------------------------------
# Tool dispatcher
# ----------------------------------------------------------------------

def _first(arguments: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = arguments.get(key)
        if value is not None and value != "":
            return value
    return None

def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _recall_tool(arguments: dict[str, Any]) -> tuple[list[str] | str, bool]:
    question = _first(arguments, ["question", "questions", "query"])
    materials = _first(arguments, ["materials", "documents", "sources"])
    try:
        chunks = recall(question if question is not None else "", materials)
    except ToolboxError as exc:
        return f"Error: {exc}", True
    return chunks, False

def _navigate_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    map_id = _first(arguments, ["map_id", "map", "graph_id"])
    current = _first(arguments, ["from", "from_node", "current", "source"])
    destination = _first(arguments, ["to", "to_node", "destination", "target"])
    if map_id is None or current is None or destination is None:
        return "Error: navigate requires map_id, from, and to arguments", True

    visited = _first(arguments, ["visited", "visited_nodes", "seen", "path"])
    if isinstance(visited, str):
        visited = [node.strip() for node in visited.split(",") if node.strip()]

    hops_left = _as_int(_first(arguments, ["hops_left", "hops", "remaining_hops", "allowance"]))
    base_url = _first(arguments, ["base_url", "host", "base"])
    graph = _first(arguments, ["graph", "map_data", "data"])

    try:
        next_node, _path, _cost = navigate(
            map_id=str(map_id),
            current=str(current),
            destination=str(destination),
            hops_left=hops_left,
            visited=visited or [],
            base_url=base_url,
            graph=graph,
        )
    except ToolboxError as exc:
        return f"Error: {exc}", True
    return str(next_node), False

def call_tool(name: str, arguments: dict[str, Any]) -> tuple[str | list[str], bool]:
    if name == "get_name":
        return BOT_NAME, False
    if name == "calculate":
        return _calculate(arguments)
    if name == "classify_shape":
        return _classify(arguments)
    if name in ("recall", "retrieve"):
        return _recall_tool(arguments)
    if name == "navigate":
        return _navigate_tool(arguments)
    return f"Error: unknown tool {name!r}", True

# ----------------------------------------------------------------------
# JSON‑RPC handlers
# ----------------------------------------------------------------------

def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}

def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

def handle_rpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
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

        result_data, is_error = call_tool(name, arguments)
        if isinstance(result_data, list):
            content = [{"type": "text", "text": str(chunk)} for chunk in result_data]
        else:
            content = [{"type": "text", "text": str(result_data)}]

        return _ok(
            msg_id,
            {"content": content, "isError": is_error},
        )
    if method == "resources/list":
        return _ok(msg_id, {"resources": []})
    if method == "prompts/list":
        return _ok(msg_id, {"prompts": []})
    if method.startswith("notifications/"):
        return None
    return _err(msg_id, -32601, f"Method not found: {method}")

def handle_rpc(body: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(body, list):
        responses = [r for msg in body if isinstance(msg, dict) and (r := handle_rpc_message(msg)) is not None]
        return responses or None
    if isinstance(body, dict):
        return handle_rpc_message(body)
    return _err(None, -32600, "Invalid Request")

# ----------------------------------------------------------------------
# SSE helpers and endpoint
# ----------------------------------------------------------------------

def sse_encode(payload: dict[str, Any] | list[dict[str, Any]]) -> str:
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: message\ndata: {data}\n\n"

async def sse_response_stream(payload: dict[str, Any] | list[dict[str, Any]]):
    yield sse_encode(payload)

async def sse_endpoint_stream():
    yield "event: endpoint\ndata: /mcp\n\n"
    while True:
        await asyncio.sleep(15)
        yield ": keepalive\n\n"

# ----------------------------------------------------------------------
# FastAPI route
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Optional: prefetch study materials on startup
# ----------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    # Warm the document cache with the default materials.
    urls = [doc["url"] for doc in DEFAULT_STUDY_MATERIALS if "url" in doc]
    _fetch_many(urls)