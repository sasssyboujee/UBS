"""Core logic for the school-days challenge tools: recall and navigate."""

from __future__ import annotations

import concurrent.futures
import heapq
import json
import os
import re
from typing import Any

import httpx
import tiktoken

TOKEN_BUDGET = 900
_ENCODING = tiktoken.get_encoding("o200k_base")

DEFAULT_STUDY_MATERIALS: list[dict[str, str]] = [
    {"title": "The Meridian Trench Research Station",
     "url": "https://tool-box-2591eaa24fa3.herokuapp.com/study-materials/1"},
    {"title": "Ashgrove Metropolitan Transit Authority",
     "url": "https://tool-box-2591eaa24fa3.herokuapp.com/study-materials/2"},
    {"title": "Velmara Compound Phase II Trial Record",
     "url": "https://tool-box-2591eaa24fa3.herokuapp.com/study-materials/3"},
    {"title": "Hollowlight Engine Technical Handbook",
     "url": "https://tool-box-2591eaa24fa3.herokuapp.com/study-materials/4"},
    {"title": "Thornmere Growers Cooperative Yearbook",
     "url": "https://tool-box-2591eaa24fa3.herokuapp.com/study-materials/5"},
]

# Fallback host for /graph if GRAPH_BASE_URL isn't set and map_id isn't a full URL.
# Verify this matches wherever the graph endpoint is actually served.
_DEFAULT_GRAPH_HOST = "https://tool-box-2591eaa24fa3.herokuapp.com"

_PER_DOC_TIMEOUT = 6.0     # per-request network timeout
_FETCH_BUDGET = 8.0        # overall wall-clock budget for fetching ALL docs in one call

_doc_cache: dict[str, str] = {}
_graph_cache: dict[str, dict[str, Any]] = {}



class ToolboxError(Exception):
    """Raised for any user-facing tool failure (bad input, fetch failure, no path)."""


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------

def _token_count(text: str) -> int:
    return len(_ENCODING.encode(text))


def _fetch_document(url: str) -> str | None:
    """Fetch and cache one document. Returns None on failure (never raises)."""
    if url in _doc_cache:
        return _doc_cache[url]
    try:
        resp = httpx.get(url, timeout=_PER_DOC_TIMEOUT)
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


def prefetch_default_materials() -> None:
    """Warm the document cache. Call this once at app startup so real
    requests almost never touch the network."""
    urls = [doc["url"] for doc in DEFAULT_STUDY_MATERIALS if "url" in doc]
    _fetch_many(urls)


def _fetch_many(urls: list[str]) -> None:
    """Fetch multiple URLs concurrently, populating _doc_cache. Never raises."""
    missing = [u for u in urls if u not in _doc_cache]
    if not missing:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(missing)) as pool:
        futures = {pool.submit(_fetch_document, u): u for u in missing}
        concurrent.futures.wait(futures, timeout=_FETCH_BUDGET)


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
        # Accept a raw JSON blob (e.g. the exact {"documents": [...]} shape)
        # passed back as a string, not just a URL.
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if parsed is not None:
                return _resolve_materials(parsed)

        if stripped.startswith("http://") or stripped.startswith("https://"):
            try:
                resp = httpx.get(stripped, timeout=_PER_DOC_TIMEOUT)
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

        # Plain inline text.
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
    """Last-resort split so no single returned chunk can ever exceed the budget."""
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
                # One source failing/timing out shouldn't kill the whole answer.
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
    """Return passages (<=900 o200k_base tokens total, each individually
    guaranteed <=900 tokens) relevant to `question`."""
    if not question:
        raise ToolboxError("question is required")

    terms = _question_terms(question)
    chunks = _load_chunks(materials)

    scored = sorted(
        ((_score_chunk(c, terms), _token_count(c), c) for c in chunks),
        key=lambda x: (-x[0], x[1]),
    )

    selected = []
    # Return only the highest scoring chunk (if any)
    for score, tok, chunk in scored:
        if score > 0:
            selected.append(chunk)
            break
    # If nothing matched, take the shortest chunk
    if not selected and chunks:
        selected.append(min(chunks, key=_token_count))
    return selected


# ---------------------------------------------------------------------------
# navigate
# ---------------------------------------------------------------------------

def _fetch_graph(map_id: str, base_url: str | None) -> dict[str, Any]:
    if map_id.startswith("http://") or map_id.startswith("https://"):
        url = map_id
    else:
        host = (base_url or os.environ.get("GRAPH_BASE_URL") or _DEFAULT_GRAPH_HOST).rstrip("/")
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


def _bounded_next(adjacency, tolls, start, dest, max_hops, forbidden):
    # DP: reachable[h][node] = (cost, parent)
    reachable = [dict() for _ in range(max_hops + 1)]
    reachable[0][start] = (0.0, None)
    
    for h in range(1, max_hops + 1):
        # start with the previous level's nodes (allow fewer hops)
        reachable[h] = dict(reachable[h-1])
        for u, (cost_u, _) in reachable[h-1].items():
            for v, w in adjacency.get(u, {}).items():
                if v in forbidden and v != dest:
                    continue
                new_cost = cost_u + w + tolls.get(v, 0.0)
                if v not in reachable[h] or new_cost < reachable[h][v][0]:
                    reachable[h][v] = (new_cost, u)
    
    # Find best cost to destination across all hop counts
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
            # This can happen if the node was carried over from previous level
            # without moving. We need to go back one hop.
            h -= 1
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

    # Auto-track visited nodes per (map_id, destination) so a single missed
    # 'visited' argument from the agent can't cause an illegal revisit.
    journey_key = (map_id, destination)
    session_visited = _visited_by_journey.setdefault(journey_key, set())
    session_visited.add(current)

    forbidden = set(visited or []) | session_visited
    forbidden.discard(destination)

    if hops_left is not None:
        next_node = _bounded_next(adjacency, tolls, current, destination, hops_left, forbidden)
    else:
        next_node = _dijkstra_next(adjacency, tolls, current, destination, forbidden)

    if next_node is None:
        raise ToolboxError("no valid path found within constraints")

    return next_node, [], 0.0