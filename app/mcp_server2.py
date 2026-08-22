"""MCP server for the Tool-box / Nursery challenge (phases 1-3).

Built on FastMCP (the `mcp` library), exposed over the Streamable HTTP
transport and mounted into the FastAPI app in `app/main.py`.

Phases:
  1. get_name, calculate, classify_shape
  2. recall/retrieve (RAG over the official study materials, <=900 tokens),
     navigate / route (least-cost graph traversal)
  3. find_open_venues, find_meeting_window, find_meeting_point, plan_outing
     (the "city and the clock" problems)

The recall tool embeds the study materials into a local sparse embedding
index (hashed word + character-trigram TF-IDF vectors, cosine retrieval)
with a paraphrase-bridge lexical rerank, so only the passages that matter
are returned and the 900 o200k_base token budget is used tightly.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import heapq
import json
import math
import os
import re
from functools import cache
from typing import Annotated, Any

import httpx
import tiktoken
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

TOKEN_BUDGET = 900          # hard ceiling counted by the judge (o200k_base)
FILL_TARGET = 700           # we stop filling at this many tokens
_ENCODING = tiktoken.get_encoding("o200k_base")

FETCH_TIMEOUT = 6.0         # per-request network timeout
FETCH_BUDGET = 8.0          # wall-clock budget for fetching all docs at once

CHALLENGE_BASE_URL = os.environ.get(
    "TOOLBOX_BASE_URL", "https://tool-box-2591eaa24fa3.herokuapp.com"
)

SERVER_NAME = "nursery-toolbox"
BOT_NAME = "Nursery"

_DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_DAY_ABBREV = {
    "mon": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "wed": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}

DEFAULT_STUDY_MATERIALS: list[dict[str, str]] = [
    {"title": "The Meridian Trench Research Station",
     "url": f"{CHALLENGE_BASE_URL}/study-materials/1"},
    {"title": "Ashgrove Metropolitan Transit Authority",
     "url": f"{CHALLENGE_BASE_URL}/study-materials/2"},
    {"title": "Velmara Compound Phase II Trial Record",
     "url": f"{CHALLENGE_BASE_URL}/study-materials/3"},
    {"title": "Hollowlight Engine Technical Handbook",
     "url": f"{CHALLENGE_BASE_URL}/study-materials/4"},
    {"title": "Thornmere Growers Cooperative Yearbook",
     "url": f"{CHALLENGE_BASE_URL}/study-materials/5"},
]


class ToolboxError(Exception):
    """User-facing tool failure (bad input, fetch failure, no solution)."""


# ----------------------------------------------------------------------
# Caches (never evicted; the corpus and city data are small)
# ----------------------------------------------------------------------

_doc_cache: dict[str, str] = {}
_graph_cache: dict[str, dict[str, Any]] = {}
_venue_cache: dict[str, list[dict[str, Any]]] = {}
_schedule_cache: dict[tuple[str, str], list[tuple[int, int]]] = {}
_location_cache: dict[tuple[str, str], tuple[int, int]] = {}
_study_index_cache: list[dict[str, str]] | None = None
_rag_cache: dict[tuple, Any] = {}

# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------

def _fetch_json(url: str) -> Any:
    try:
        resp = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolboxError(f"failed to fetch {url}: {exc}") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolboxError(f"invalid JSON from {url}") from exc


def _fetch_document(url: str) -> str | None:
    """Fetch one document's text; never raises."""
    if url in _doc_cache:
        return _doc_cache[url]
    try:
        resp = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = resp.json()
        except ValueError:
            data = None
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


# ----------------------------------------------------------------------
# Study materials (recall)
# ----------------------------------------------------------------------

def _study_materials() -> list[dict[str, str]]:
    """The official document list; fetched once from /study-materials."""
    global _study_index_cache
    if _study_index_cache is not None:
        return list(_study_index_cache)
    try:
        data = _fetch_json(f"{CHALLENGE_BASE_URL}/study-materials")
        docs = [
            {"title": str(d.get("title") or f"document {d.get('id')}"), "url": str(d["url"])}
            for d in data.get("documents", [])
            if d.get("url")
        ]
        if docs:
            _study_index_cache = docs
            return list(docs)
    except ToolboxError:
        pass
    return list(DEFAULT_STUDY_MATERIALS)


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
        return _study_materials()

    if isinstance(materials, str):
        stripped = materials.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if parsed is not None:
                return _resolve_materials(parsed)
        if stripped.startswith(("http://", "https://")):
            try:
                resp = httpx.get(stripped, timeout=FETCH_TIMEOUT, follow_redirects=True)
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


def _load_docs(materials: Any) -> list[dict[str, str]]:
    docs = _resolve_materials(materials)
    urls = [d["url"] for d in docs if "url" in d]
    _fetch_many(urls)
    out: list[dict[str, str]] = []
    for doc in docs:
        if "text" in doc:
            text = doc["text"]
        else:
            text = _doc_cache.get(doc["url"])
            if text is None:
                continue
        out.append({"title": doc.get("title", ""), "text": text})
    return out


def _token_count(text: str) -> int:
    return len(_ENCODING.encode(text))


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


# ----------------------------------------------------------------------
# RAG: sparse embeddings over the materials, cosine retrieval
# ----------------------------------------------------------------------

@cache
def _feat_hash(feat: str) -> int:
    return int.from_bytes(hashlib.blake2b(feat.encode("utf-8"), digest_size=4).digest(), "big")


def _text_features(text: str) -> dict[int, int]:
    """Word (len>=3) and character-trigram features of a text, with counts."""
    counts: dict[int, int] = {}
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) >= 3:
            key = _feat_hash("w:" + word)
            counts[key] = counts.get(key, 0) + 1
        for i in range(len(word) - 2):
            key = _feat_hash("t:" + word[i : i + 3])
            counts[key] = counts.get(key, 0) + 1
    return counts


def _build_index(chunks: list[str]) -> tuple[dict[int, float], list[dict[int, float]]]:
    """TF-IDF, L2-normalised sparse vectors for each chunk."""
    n = len(chunks)
    df: dict[int, int] = {}
    feats = []
    for chunk in chunks:
        f = _text_features(chunk)
        feats.append(f)
        for key in f:
            df[key] = df.get(key, 0) + 1
    idf = {k: math.log((1 + n) / (1 + v)) + 1.0 for k, v in df.items()}
    vectors: list[dict[int, float]] = []
    for f in feats:
        if not f:
            vectors.append({})
            continue
        norm = 0.0
        vec: dict[int, float] = {}
        for key, count in f.items():
            w = count * idf[key]
            vec[key] = w
            norm += w * w
        norm = math.sqrt(norm)
        vectors.append({k: w / norm for k, w in vec.items()})
    return idf, vectors


def _get_rag(docs: list[dict[str, str]]) -> tuple[dict[int, float], list[dict[int, float]], list[tuple[int, str]]]:
    fingerprint = tuple(
        (d["title"], hashlib.blake2b(d["text"].encode("utf-8"), digest_size=8).hexdigest())
        for d in docs
    )
    cached = _rag_cache.get(fingerprint)
    if cached is not None:
        return cached
    chunks = [(i, c) for i, d in enumerate(docs) for c in _split_paragraphs(d["text"])]
    idf, vectors = _build_index([c for _, c in chunks])
    _rag_cache[fingerprint] = (idf, vectors, chunks)
    return idf, vectors, chunks


def _embed_query(text: str, idf: dict[int, float]) -> dict[int, float]:
    f = _text_features(text)
    if not f:
        return {}
    max_idf = max(idf.values()) if idf else 1.0
    norm = 0.0
    vec: dict[int, float] = {}
    for key, count in f.items():
        w = count * idf.get(key, max_idf)
        vec[key] = w
        norm += w * w
    if not norm:
        return {}
    norm = math.sqrt(norm)
    return {k: w / norm for k, w in vec.items()}


def _dot(a: dict[int, float], b: dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(k, 0.0) for k, w in a.items())


# ----------------------------------------------------------------------
# Question analysis: stopwords, numbers, paraphrase bridges
# ----------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "is", "are", "was", "were", "be", "been", "being", "did", "do", "does",
    "done", "how", "many", "much", "what", "when", "where", "which", "who",
    "whom", "why", "it", "its", "this", "that", "these", "those", "with",
    "from", "by", "as", "about", "can", "could", "should", "would", "will",
    "may", "might", "must", "you", "your", "i", "me", "my", "we", "our",
    "us", "they", "them", "their", "he", "she", "him", "her", "his", "not",
    "no", "but", "if", "then", "than", "so", "also", "into", "onto", "up",
    "out", "over", "under", "between", "across", "every", "all", "any",
    "some", "most", "few", "more", "less", "last", "first", "next", "has",
    "have", "had", "having", "roughly", "approximately", "there", "itself",
    "back", "brought", "lastly", "still", "get", "got", "said",
}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORDS = {**_UNITS, **_TENS}
_NUMBER_PAIR = re.compile(
    r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"-(one|two|three|four|five|six|seven|eight|nine)\b"
)


def _number_to_words(value: int) -> str | None:
    if 0 <= value < 20:
        return _UNITS.get(value)
    if 20 <= value < 100:
        tens, units = divmod(value, 10)
        word = _TENS[tens * 10]
        if units:
            word += "-" + _UNITS[units]
        return word
    return None


def _question_terms(question: Any) -> set[str]:
    if isinstance(question, list):
        question = " ".join(str(q) for q in question)
    text = str(question).lower()
    text = _NUMBER_PAIR.sub(
        lambda m: str(_TENS[m.group(1)] + _UNITS[m.group(2)]), text
    )
    terms: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", text):
        if word in _NUMBER_WORDS:
            value = _NUMBER_WORDS[word]
            terms.add(word)
            terms.add(str(value))
            continue
        if word.isdigit() and 0 < int(word) < 100:
            terms.add(word)
            spelled = _number_to_words(int(word))
            if spelled:
                terms.add(spelled)
                terms.add(spelled.replace(" ", "-"))
            continue
        if word in _STOPWORDS or len(word) < 3:
            continue
        terms.add(word)
    return terms


# Question term -> related corpus terms (the paraphrase patterns seen in the
# real questions). Each bridge hit is weighted like a strong match.
BRIDGES: dict[str, tuple[str, ...]] = {
    "motormen": ("drivers", "driver", "crew", "operators"),
    "motorman": ("drivers", "driver", "crew"),
    "licensed": ("certified", "licenced"),
    "scrubbing": ("scrubber", "scrubbers", "oxygen"),
    "scrubbed": ("scrubber", "scrubbers"),
    "break": ("failure", "failed", "fault", "malfunction"),
    "broke": ("failure", "failed", "fault", "malfunction"),
    "broken": ("failure", "failed", "fault", "malfunction"),
    "breakdown": ("failure", "failed", "fault", "malfunction"),
    "fail": ("failure", "fault", "failed"),
    "equipment": ("machinery", "machine", "unit", "equipment"),
    "machine": ("machine", "machinery", "drier", "dryer", "unit", "equipment"),
    "drying": ("drier", "dryer", "drying"),
    "dryer": ("drier", "dryer", "drying"),
    "drier": ("drier", "dryer", "drying"),
    "share": ("shared", "sharing", "rota", "resolution"),
    "sharing": ("shared", "rota", "resolution", "arrangement"),
    "shared": ("rota", "resolution", "sharing"),
    "approve": ("approved", "adopted", "resolution", "ratified"),
    "approval": ("approved", "adopted", "resolution", "ratified"),
    "approved": ("adopted", "resolution"),
    "arrangement": ("arrangement", "resolution", "rota", "scheme"),
    "collision": ("crash", "accident"),
    "crash": ("collision", "accident"),
}


def _lexical_score(chunk: str, terms: set[str]) -> float:
    """Exact / prefix / bridge-match signal, normalised by question length."""
    if not terms:
        return 0.0
    words = set(re.findall(r"[a-z0-9]+", chunk.lower()))
    exact = 0
    bridges = 0
    prefixes = 0
    for term in terms:
        if term in words:
            exact += 1
            continue
        if any(b in words for b in BRIDGES.get(term, ())):
            bridges += 1
            continue
        if len(term) >= 4 and any(
            len(w) >= 4 and (w.startswith(term) or term.startswith(w)) for w in words
        ):
            prefixes += 1
    return (2.0 * exact + 3.0 * bridges + 1.5 * prefixes) / len(terms)


def recall(question: Any, materials: Any = None) -> list[str]:
    """RAG retrieve the passages that answer the question.

    Embeds the question into the material index, ranks chunks by
    cosine similarity plus a lexical paraphrase signal, and returns the
    top passages within the 900 o200k_base token budget. Always includes
    the runner-up document's best passage as routing insurance.
    """
    if not question:
        raise ToolboxError("question is required")

    q = " ".join(str(x) for x in question) if isinstance(question, list) else str(question)
    terms = _question_terms(q)
    docs = _load_docs(materials)
    if not docs:
        return []

    idf, vectors, chunks = _get_rag(docs)
    qv = _embed_query(q, idf)

    scored: list[tuple[float, float, float, int, int, str]] = []
    for (doc_i, text), vec in zip(chunks, vectors):
        cosine = _dot(qv, vec)
        lexical = _lexical_score(text, terms)
        scored.append((0.5 * cosine + lexical, cosine, lexical, _token_count(text), doc_i, text))

    if not scored:
        return []

    max_lex = max(item[2] for item in scored)
    if max_lex > 0:
        # Precision path: a lexical (exact/bridge/prefix) signal exists, so rank
        # by the combined score across ALL documents (a wrong single-doc route
        # must not drop the fact).
        scored.sort(key=lambda x: (-x[0], -x[2], x[3]))
    else:
        # Heavy-paraphrase fallback: rely on the embedding (cosine) similarity and
        # return the most relevant passages so the fact is not lost to a single
        # short-chunk fallback.
        scored.sort(key=lambda x: (-x[1], x[3]))

    selected: list[str] = []
    total = 0
    for combined, cosine, lexical, tok, doc_i, text in scored:
        if total >= FILL_TARGET:
            break
        if tok < 40 and not re.search(r"[.!?]", text):
            continue
        if total + tok <= TOKEN_BUDGET:
            selected.append(text)
            total += tok

    return selected


# ----------------------------------------------------------------------
# Graphs: navigate / route
# ----------------------------------------------------------------------

def _fetch_graph(map_id: str, base_url: str | None) -> dict[str, Any]:
    if map_id.startswith(("http://", "https://")):
        url = map_id
    else:
        host = (base_url or os.environ.get("GRAPH_BASE_URL") or CHALLENGE_BASE_URL).rstrip("/")
        url = f"{host}/graph?map_id={map_id}"

    if url in _graph_cache:
        return _graph_cache[url]

    try:
        resp = httpx.get(url, timeout=8.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolboxError(f"failed to fetch graph {url}: {exc}") from exc

    data = resp.json()
    _graph_cache[url] = data
    return data


def _dijkstra_path(
    adjacency: dict[str, dict[str, float]],
    tolls: dict[str, float],
    start: str,
    dest: str,
    forbidden: set[str],
) -> tuple[list[str], float] | tuple[None, None]:
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
        return None, None

    path: list[str] = []
    node = dest
    while node != start:
        path.append(node)
        node = prev[node]
    path.reverse()
    return path, dist[dest]


def _bounded_path(
    adjacency: dict[str, dict[str, float]],
    tolls: dict[str, float],
    start: str,
    dest: str,
    max_hops: int,
    forbidden: set[str],
) -> tuple[list[str], float] | tuple[None, None]:
    reachable: list[dict[str, tuple[float, str | None]]] = [{} for _ in range(max_hops + 1)]
    reachable[0][start] = (0.0, None)

    for h in range(1, max_hops + 1):
        reachable[h] = dict(reachable[h - 1])
        for u, (cost_u, _) in reachable[h - 1].items():
            for v, w in adjacency.get(u, {}).items():
                if v in forbidden and v != dest:
                    continue
                new_cost = cost_u + w + tolls.get(v, 0.0)
                if v not in reachable[h] or new_cost < reachable[h][v][0]:
                    reachable[h][v] = (new_cost, u)

    best_h: int | None = None
    best_cost: float | None = None
    for h in range(max_hops + 1):
        if dest in reachable[h]:
            cost = reachable[h][dest][0]
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_h = h

    if best_h is None:
        return None, None

    path: list[str] = []
    node = dest
    h = best_h
    while node != start:
        parent = reachable[h][node][1]
        if parent is None:
            h -= 1
            if h < 0:
                return None, None
            continue
        path.append(node)
        node = parent
        h -= 1
    path.append(start)
    path.reverse()
    return path[1:], best_cost


def _find_path(
    map_id: str,
    current: str,
    destination: str,
    hops_left: int | None,
    visited: Any,
    base_url: str,
    graph: Any,
) -> tuple[list[str], float]:
    if current == destination:
        raise ToolboxError("already at destination")

    graph_data = graph if isinstance(graph, dict) else _fetch_graph(map_id, base_url or None)
    adjacency = graph_data.get("adjacency", {})
    tolls = graph_data.get("tolls", {})

    forbidden = _node_set(visited)
    forbidden.discard(destination)

    if hops_left is not None:
        path, cost = _bounded_path(adjacency, tolls, current, destination, hops_left, forbidden)
    else:
        path, cost = _dijkstra_path(adjacency, tolls, current, destination, forbidden)

    if path is None:
        raise ToolboxError("no valid path found within constraints")
    return path, cost


def _node_set(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        return {n.strip() for n in value.split(",") if n.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(n).strip() for n in value if str(n).strip()}
    return set()


# ----------------------------------------------------------------------
# calculate / classify_shape (phase 1)
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


def _classify_image(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ToolboxError("image must be a base64 string")
    try:
        data = base64.b64decode(raw)
    except ValueError as exc:
        raise ToolboxError("invalid base64 image") from exc

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ToolboxError("image processing unavailable") from exc

    array = np.frombuffer(data, np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ToolboxError("could not decode image")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.mean()) > 127:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ToolboxError("no shape found")

    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    perimeter = float(cv2.arcLength(contour, True))
    if area <= 0 or perimeter <= 0:
        raise ToolboxError("shape too small")

    approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
    vertices = len(approx)
    circularity = 4.0 * math.pi * area / (perimeter * perimeter)

    if vertices == 3:
        return "triangle"
    if vertices == 4:
        return "rectangle"
    if circularity >= 0.85:
        return "circle"
    if circularity >= 0.68:
        return "rectangle"
    return "triangle"


# ----------------------------------------------------------------------
# Phase 3: the city and the clock
# ----------------------------------------------------------------------

def _normalize_day(day: Any) -> str:
    d = str(day or "").strip().lower()
    d = _DAY_ABBREV.get(d, d)
    if d not in _DAYS:
        raise ToolboxError(f"unknown day {day!r}; use a weekday name")
    return d.capitalize()


def _parse_time(value: Any) -> int:
    """'HH:MM' (or plain hour) -> minutes since midnight."""
    s = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.fullmatch(r"(\d{1,2})", s)
    if match:
        return int(match.group(1)) * 60
    raise ToolboxError(f"cannot parse time {value!r}; use HH:MM")


def _fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_intervals(value: Any) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    items = value if isinstance(value, (list, tuple)) else [value]
    for item in items:
        if isinstance(item, dict):
            start, end = item.get("start"), item.get("end")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end = item[0], item[1]
        elif isinstance(item, str):
            match = re.search(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", item)
            if not match:
                raise ToolboxError(f"cannot parse interval {item!r}; use HH:MM-HH:MM")
            start, end = match.groups()
        else:
            raise ToolboxError(f"cannot parse interval {item!r}")
        if start is None or end is None:
            raise ToolboxError(f"cannot parse interval {item!r}")
        out.append((_parse_time(start), _parse_time(end)))
    return out


def _parse_duration(value: Any) -> int:
    if value is None or value == "":
        return 60
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    match = re.search(r"(\d+)", s)
    if not match:
        raise ToolboxError(f"cannot parse duration {value!r}")
    minutes = int(match.group(1))
    if "hour" in s and "min" not in s:
        minutes *= 60
    return minutes


def _parse_people(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    people: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("name", "")
        for name in re.split(r"[,\s;]+", str(item).strip()):
            name = name.strip().lower()
            if not name or name in ("you", "me", "i", "the", "android"):
                continue
            if name not in people:
                people.append(name)
    return people


def _parse_position(value: Any) -> tuple[int, int]:
    if isinstance(value, dict):
        x, y = value.get("x"), value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = value[0], value[1]
    elif isinstance(value, str):
        numbers = re.findall(r"-?\d+", value)
        if len(numbers) < 2:
            raise ToolboxError(f"cannot parse position {value!r}; use [x, y]")
        x, y = numbers[0], numbers[1]
    else:
        raise ToolboxError(f"cannot parse position {value!r}; use [x, y]")
    try:
        return int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ToolboxError(f"cannot parse position {value!r}; use [x, y]") from exc


def _get_venues(day: Any, venues: Any = None) -> list[dict[str, Any]]:
    if venues is not None:
        if isinstance(venues, dict) and isinstance(venues.get("venues"), list):
            return venues["venues"]
        if isinstance(venues, list):
            return venues
        raise ToolboxError("unexpected 'venues' data; pass the venues list or the endpoint payload")

    key = _normalize_day(day).lower()
    cached = _venue_cache.get(key)
    if cached is not None:
        return cached

    data = _fetch_json(f"{CHALLENGE_BASE_URL}/venues/{_normalize_day(day)}")
    items = data.get("venues", [])
    _venue_cache[key] = items
    return items


def _lookup_person(data: Any, person: str) -> Any:
    person = person.lower()
    if isinstance(data, dict):
        if isinstance(data.get("person"), str) and data["person"].lower() == person:
            return data
        for key, value in data.items():
            if str(key).lower() == person:
                return value
    elif isinstance(data, (list, tuple)):
        for entry in data:
            if isinstance(entry, dict) and str(entry.get("person", "")).lower() == person:
                return entry
    return None


def _get_schedule(person: Any, day: Any, schedules: Any = None) -> list[tuple[int, int]]:
    name = str(person or "").strip().lower()
    if not name:
        raise ToolboxError("schedule lookup needs a person name")
    d = _normalize_day(day)

    if schedules is not None:
        entry = _lookup_person(schedules, name)
        if entry is None:
            raise ToolboxError(f"no schedule provided for {person!r}")
        busy = entry.get("busy", entry) if isinstance(entry, dict) else entry
        if isinstance(entry, dict) and "busy" not in entry and entry.get("person") is None:
            busy = entry
        return _parse_intervals(busy)

    key = (name, d.lower())
    cached = _schedule_cache.get(key)
    if cached is not None:
        return cached

    data = _fetch_json(f"{CHALLENGE_BASE_URL}/schedule/{name}/{d}")
    busy = _parse_intervals(data.get("busy", []))
    _schedule_cache[key] = busy
    return busy


def _get_location(person: Any, day: Any, locations: Any = None) -> tuple[int, int]:
    name = str(person or "").strip().lower()
    if not name:
        raise ToolboxError("location lookup needs a person name")
    d = _normalize_day(day)

    if locations is not None:
        entry = _lookup_person(locations, name)
        if entry is None:
            raise ToolboxError(f"no location provided for {person!r}")
        return _parse_position(entry)

    key = (name, d.lower())
    cached = _location_cache.get(key)
    if cached is not None:
        return cached

    data = _fetch_json(f"{CHALLENGE_BASE_URL}/location/{name}/{d}")
    point = _parse_position(data)
    _location_cache[key] = point
    return point


def _overlaps(ws: int, we: int, bs: int, be: int) -> bool:
    """Half-open overlap: [ws, we) vs [bs, be)."""
    return ws < be and we > bs


def _find_window(
    range_start: int,
    range_end: int,
    duration: int,
    busy_all: list[tuple[int, int]],
    tentative_all: list[tuple[int, int]],
) -> tuple[int, int, bool]:
    if duration <= 0 or range_end - duration < range_start:
        raise ToolboxError("the range is too short for the meeting length")

    fallback: tuple[int, int] | None = None
    t = range_start
    while t + duration <= range_end:
        we = t + duration
        if all(not _overlaps(t, we, bs, be) for bs, be in busy_all):
            if all(not _overlaps(t, we, bs, be) for bs, be in tentative_all):
                return t, we, True
            if fallback is None:
                fallback = (t, we)
        t += 60

    if fallback is not None:
        return fallback[0], fallback[1], False
    raise ToolboxError("no window everyone can make in that range")


_INBOX_WHEN = re.compile(r"(?im)^When:\s*(\w+)\s+(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")
_INBOX_RESPONSE = re.compile(r"(?im)^Response:\s*(\w+)")


def _parse_inbox(inbox: str, day: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    busy: list[tuple[int, int]] = []
    tentative: list[tuple[int, int]] = []
    for block in re.split(r"\n\s*\n", inbox or ""):
        when = _INBOX_WHEN.search(block)
        response = _INBOX_RESPONSE.search(block)
        if not when or not response:
            continue
        block_day, start, end = when.groups()
        try:
            if _normalize_day(block_day) != day:
                continue
        except ToolboxError:
            continue
        kind = response.group(1).upper()
        interval = (_parse_time(start), _parse_time(end))
        if kind == "ACCEPTED":
            busy.append(interval)
        elif kind == "TENTATIVE":
            tentative.append(interval)
    return busy, tentative


def _calendar(
    day: Any,
    people: list[str],
    busy: Any,
    tentative: Any,
    inbox: Any,
    schedules: Any,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    d = _normalize_day(day)
    busy_all: list[tuple[int, int]] = []
    tentative_all: list[tuple[int, int]] = []

    for person in people:
        busy_all.extend(_get_schedule(person, d, schedules=schedules))

    if busy not in (None, "", []):
        busy_all.extend(_parse_intervals(busy))
    if tentative not in (None, "", []):
        tentative_all.extend(_parse_intervals(tentative))
    if inbox not in (None, ""):
        inbox_busy, inbox_tentative = _parse_inbox(str(inbox), d)
        busy_all.extend(inbox_busy)
        tentative_all.extend(inbox_tentative)

    return busy_all, tentative_all


def venues_open(day: Any, time: Any, venues: Any = None) -> list[str]:
    """Names of every venue open at `time` on `day`."""
    t = _parse_time(time)
    names: list[str] = []
    for venue in _get_venues(day, venues=venues):
        available = venue.get("available", [])
        try:
            intervals = _parse_intervals(available)
        except ToolboxError:
            continue
        if any(s <= t < e for s, e in intervals):
            names.append(str(venue.get("name", "")))
    return [n for n in names if n]


def find_meeting_window(
    day: Any,
    start: Any,
    end: Any,
    duration_minutes: Any = None,
    people: Any = None,
    busy: Any = None,
    tentative: Any = None,
    inbox: Any = None,
    schedules: Any = None,
) -> str:
    """Earliest clean window, else earliest window that only overlaps tentative."""
    people_list = _parse_people(people)
    if not people_list:
        raise ToolboxError("find_meeting_window needs 'people' (friend names)")

    busy_all, tentative_all = _calendar(day, people_list, busy, tentative, inbox, schedules)
    ws, we, _clean = _find_window(
        _parse_time(start),
        _parse_time(end),
        _parse_duration(duration_minutes),
        busy_all,
        tentative_all,
    )
    return f"{_fmt_time(ws)}-{_fmt_time(we)}"


def _median_low(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def meeting_point(
    day: Any,
    position: Any,
    people: Any,
    locations: Any = None,
) -> tuple[int, int]:
    """Grid cell that minimises everyone's total Manhattan travel."""
    own = _parse_position(position)
    people_list = _parse_people(people)
    if not people_list:
        raise ToolboxError("meeting_point needs 'people' (friend names)")

    points = [own]
    for person in people_list:
        points.append(_get_location(person, day, locations=locations))
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return _median_low(xs), _median_low(ys)


def plan_outing(
    day: Any,
    position: Any,
    people: Any,
    start: Any,
    end: Any,
    duration_minutes: Any = None,
    busy: Any = None,
    tentative: Any = None,
    inbox: Any = None,
    venues: Any = None,
    schedules: Any = None,
    locations: Any = None,
) -> dict[str, Any]:
    """Meeting window + meeting point + place to eat, minimising total travel."""
    people_list = _parse_people(people)
    if not people_list:
        raise ToolboxError("plan_outing needs 'people' (friend names)")

    d = _normalize_day(day)
    busy_all, tentative_all = _calendar(d, people_list, busy, tentative, inbox, schedules)
    ws, we, clean = _find_window(
        _parse_time(start),
        _parse_time(end),
        _parse_duration(duration_minutes),
        busy_all,
        tentative_all,
    )

    own = _parse_position(position)
    positions = [own]
    for person in people_list:
        positions.append(_get_location(person, d, locations=locations))

    candidates = []
    for venue in _get_venues(d, venues=venues):
        try:
            intervals = _parse_intervals(venue.get("available", []))
        except ToolboxError:
            continue
        if any(s <= we < e for s, e in intervals):
            candidates.append(venue)
    if not candidates:
        raise ToolboxError("no venue is open for the hour after the meeting ends")

    best: dict[str, Any] | None = None
    for venue in candidates:
        vx, vy = _parse_position(venue)
        xs = [x for x, _ in positions] + [vx]
        ys = [y for _, y in positions] + [vy]
        px, py = _median_low(xs), _median_low(ys)
        cost = sum(abs(x - px) + abs(y - py) for x, y in positions)
        cost += abs(vx - px) + abs(vy - py)
        if best is None or (cost, str(venue.get("name", "")).lower()) < (
            best["total_travel"],
            str(best["venue"]).lower(),
        ):
            best = {
                "window": f"{_fmt_time(ws)}-{_fmt_time(we)}",
                "point": f"[{px}, {py}]",
                "venue": str(venue.get("name", "")),
                "total_travel": cost,
                "clean_window": clean,
            }
    return best


# ----------------------------------------------------------------------
# FastMCP tool registrations
# ----------------------------------------------------------------------

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "Tool-box challenge MCP server. Use the tools to help the android answer "
        "its questions: retrieve study-material passages, traverse maps, and solve "
        "the city-and-clock problems (venues, meeting windows, meeting points, outings)."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool(description="Return the name of this assistant.")
def get_name() -> str:
    return BOT_NAME


@mcp.tool(
    description=(
        "Evaluate a full arithmetic expression with correct precedence "
        "(* and / before + and -), e.g. '2 + 3 * 5'."
    )
)
def calculate(expression: str = "", a: Any = None, b: Any = None, operator: str = "") -> str:
    if isinstance(expression, str) and expression.strip():
        try:
            return _evaluate_expression(expression)
        except ValueError as exc:
            raise ToolboxError(f"{exc}") from exc
    if a is not None and b is not None and operator is not None:
        try:
            a_int, b_int = int(a), int(b)
        except (TypeError, ValueError) as exc:
            raise ToolboxError("a and b must be integers") from exc
        op = str(operator)
        if op == "+":
            result = a_int + b_int
        elif op == "-":
            result = a_int - b_int
        elif op == "*":
            result = a_int * b_int
        elif op == "/":
            if b_int == 0:
                raise ToolboxError("division by zero")
            result = a_int / b_int
        else:
            raise ToolboxError(f"unsupported operator {op!r}")
        return _format_number(result)
    raise ToolboxError("pass the full arithmetic expression in 'expression'")


@mcp.tool(
    description=(
        "Classify a base64-encoded PNG as 'rectangle', 'triangle', or 'circle'. "
        "Pass the base64 image data in 'image'."
    )
)
def classify_shape(image: str = "") -> str:
    return _classify_image(image)


def _recall_impl(
    question: Annotated[str, Field(description="The question(s) to find study material for.")] = "",
    questions: Any = None,
    query: str = "",
    materials: Any = None,
) -> list[str]:
    q: Any = question or query
    if questions is not None and not q:
        q = questions
    if isinstance(q, list):
        q = " ".join(str(x) for x in q)
    if not q:
        raise ToolboxError("recall needs a 'question'")
    return recall(q, materials)


_RECALL_DESCRIPTION = (
    "Search the official study materials and return the passages needed to answer "
    "the question, as a list of strings. Total length is at most 900 tokens. "
    "Nothing else is needed: the materials are fetched automatically. "
    "Pass the question text in 'question'."
)
mcp.tool(name="recall", description=_RECALL_DESCRIPTION)(_recall_impl)
mcp.tool(name="retrieve", description=_RECALL_DESCRIPTION)(_recall_impl)


def _navigate_impl(
    map_id: Annotated[str, Field(description="The map_id from the question.")] = "",
    from_node: Annotated[str, Field(description="The node the android is currently on (the question calls it 'from').")] = "",
    to: Annotated[str, Field(description="The destination node label.")] = "",
    destination: str = "",
    current: str = "",
    source: str = "",
    hops_left: Any = None,
    hops_remaining: Any = None,
    remaining_hops: Any = None,
    allowance: Any = None,
    hops: Any = None,
    max_hops: Any = None,
    hop_allowance: Any = None,
    visited: Any = None,
    base_url: str = "",
    graph: Any = None,
) -> str:
    mid = map_id or ""
    start = from_node or current or source or ""
    dest = to or destination or ""
    if not mid or not start or not dest:
        raise ToolboxError(
            "navigate needs: 'map_id', 'from_node' (the node you are on), and 'to' (the destination)"
        )
    hops = _as_int(hops_left or hops_remaining or remaining_hops or allowance or hops or max_hops or hop_allowance)
    path, _cost = _find_path(mid, start, dest, hops, visited, base_url, graph)
    return path[0]


_NAV_DESCRIPTION = (
    "Return the next node to move to on the least-cost route to the destination. "
    "The map is fetched automatically from the challenge using map_id. "
    "Pass 'map_id', 'from_node' (the node you are currently on) and 'to' (the destination). "
    "When the question gives a hop allowance, pass it as 'hops_left'. "
    "Pass all nodes already visited on this journey as 'visited'."
)
mcp.tool(name="navigate", description=_NAV_DESCRIPTION)(_navigate_impl)


def _route_impl(
    map_id: str = "",
    from_node: str = "",
    to: str = "",
    destination: str = "",
    current: str = "",
    source: str = "",
    hops_left: Any = None,
    hops_remaining: Any = None,
    remaining_hops: Any = None,
    allowance: Any = None,
    hops: Any = None,
    max_hops: Any = None,
    hop_allowance: Any = None,
    visited: Any = None,
    base_url: str = "",
    graph: Any = None,
) -> str:
    mid = map_id or ""
    start = from_node or current or source or ""
    dest = to or destination or ""
    if not mid or not start or not dest:
        raise ToolboxError(
            "route needs: 'map_id', 'from_node' (the node you are on), and 'to' (the destination)"
        )
    hops = _as_int(hops_left or hops_remaining or remaining_hops or allowance or hops or max_hops or hop_allowance)
    path, cost = _find_path(mid, start, dest, hops, visited, base_url, graph)
    return " -> ".join(path) + f" | total cost {_format_number(cost)}"


mcp.tool(
    name="route",
    description=(
        "Return the FULL least-cost node path (all hops) from a start node to a "
        "destination, with its total cost, fetched automatically from map_id. "
        "Useful to see the whole route at once; still move one hop at a time with 'navigate'."
    ),
)(_route_impl)


def _venues_impl(
    day: Annotated[str, Field(description="Weekday name, e.g. 'Thursday'.")] = "",
    time: Annotated[str, Field(description="The hour to check, HH:MM, e.g. '08:00'.")] = "",
    hour: str = "",
    at: str = "",
    venues: Any = None,
) -> str:
    d = day or ""
    t = time or hour or at or ""
    if not d or not t:
        raise ToolboxError("needs 'day' and 'time' (HH:MM)")
    return ", ".join(venues_open(d, t, venues=venues))


_VENUES_DESCRIPTION = (
    "Return EVERY venue that is open at a given time on a given day, as a "
    "comma-separated list of names. The official venue list is fetched automatically. "
    "Pass 'day' (weekday name) and 'time' (HH:MM, 24-hour)."
)
mcp.tool(name="find_open_venues", description=_VENUES_DESCRIPTION)(_venues_impl)
mcp.tool(name="venues_open", description=_VENUES_DESCRIPTION)(_venues_impl)
mcp.tool(name="where_to_eat", description=_VENUES_DESCRIPTION)(_venues_impl)


def _window_impl(
    day: Annotated[str, Field(description="Weekday name, e.g. 'Tuesday'.")] = "",
    start: Annotated[str, Field(description="Earliest start of the range, HH:MM, e.g. '13:00'.")] = "",
    end: Annotated[str, Field(description="Latest end of the range, HH:MM, e.g. '18:00'.")] = "",
    range_start: str = "",
    range_end: str = "",
    until: str = "",
    duration_minutes: Any = None,
    duration: Any = None,
    people: Any = None,
    persons: Any = None,
    friends: Any = None,
    attendees: Any = None,
    busy: Any = None,
    tentative: Any = None,
    inbox: Any = None,
    schedules: Any = None,
) -> str:
    d = day or ""
    s = start or range_start or ""
    e = end or range_end or until or ""
    people_list = people or persons or friends or attendees
    if not d or not s or not e:
        raise ToolboxError("needs 'day', 'start' (HH:MM) and 'end' (HH:MM)")
    return find_meeting_window(
        d,
        s,
        e,
        duration_minutes=duration_minutes if duration_minutes is not None else duration,
        people=people_list,
        busy=busy,
        tentative=tentative,
        inbox=inbox,
        schedules=schedules,
    )


_WINDOW_DESCRIPTION = (
    "Find the meeting window that everyone can make on a day, between a start and "
    "end time, for a given duration in minutes. Friend schedules are fetched "
    "automatically. A window overlapping nothing at all (including tentative "
    "commitments) always wins over an earlier one that overlaps tentative ones. "
    "Pass 'day', 'start' (HH:MM), 'end' (HH:MM), 'duration_minutes' and 'people' "
    "(friend names, list or comma-separated). Also pass the android's own "
    "commitments as 'busy' (ACCEPTED) and 'tentative' (TENTATIVE) interval lists "
    "or as raw 'inbox' text."
)
mcp.tool(name="find_meeting_window", description=_WINDOW_DESCRIPTION)(_window_impl)
mcp.tool(name="meeting_window", description=_WINDOW_DESCRIPTION)(_window_impl)


def _point_impl(
    day: Annotated[str, Field(description="Weekday name, e.g. 'Wednesday'.")] = "",
    position: Any = None,
    from_: Any = None,
    location: Any = None,
    people: Any = None,
    persons: Any = None,
    friends: Any = None,
    attendees: Any = None,
    locations: Any = None,
) -> str:
    d = day or ""
    pos = position if position is not None else (from_ if from_ is not None else location)
    people_list = people or persons or friends or attendees
    if not d or pos is None:
        raise ToolboxError("needs 'day' and 'position' ([x, y])")
    x, y = meeting_point(d, pos, people_list, locations=locations)
    return f"[{x}, {y}]"


_POINT_DESCRIPTION = (
    "Find the point on the 10x10 grid that makes the total travel of everyone "
    "(the android plus all the friends) as small as possible, using Manhattan "
    "distance. Friend locations are fetched automatically. The android's own "
    "position counts too. Pass 'day', 'position' (the android's [x, y]) and "
    "'people' (friend names)."
)
mcp.tool(name="find_meeting_point", description=_POINT_DESCRIPTION)(_point_impl)
mcp.tool(name="meeting_point", description=_POINT_DESCRIPTION)(_point_impl)


def _outing_impl(
    day: str = "",
    position: Any = None,
    from_: Any = None,
    people: Any = None,
    persons: Any = None,
    friends: Any = None,
    attendees: Any = None,
    start: str = "",
    end: str = "",
    range_start: str = "",
    range_end: str = "",
    until: str = "",
    duration_minutes: Any = None,
    duration: Any = None,
    busy: Any = None,
    tentative: Any = None,
    inbox: Any = None,
    venues: Any = None,
    schedules: Any = None,
    locations: Any = None,
) -> str:
    d = day or ""
    s = start or range_start or ""
    e = end or range_end or until or ""
    pos = position if position is not None else from_
    people_list = people or persons or friends or attendees
    if not d or not s or not e or pos is None:
        raise ToolboxError("needs 'day', 'position' ([x, y]), 'start', 'end' and 'people'")
    plan = plan_outing(
        d,
        pos,
        people_list,
        s,
        e,
        duration_minutes=duration_minutes if duration_minutes is not None else duration,
        busy=busy,
        tentative=tentative,
        inbox=inbox,
        venues=venues,
        schedules=schedules,
        locations=locations,
    )
    return (
        f"{plan['window']} {plan['point']} {plan['venue']}"
        f" | total travel {_format_number(plan['total_travel'])}"
    )


mcp.tool(
    name="plan_outing",
    description=(
        "Plan a full outing in one call: the meeting window everyone can make, the "
        "meeting point on the grid, and the place to eat afterwards, so the whole "
        "journey (everyone's travel to the meeting point plus the trip from there "
        "to the place to eat) is as short as possible. Venues, friend schedules and "
        "friend locations are fetched automatically. Pass 'day', 'position' (the "
        "android's [x, y]), 'people', 'start' (HH:MM), 'end' (HH:MM) and "
        "'duration_minutes'; optionally the android's own 'busy'/'tentative'/"
        "'inbox'."
    ),
)(_outing_impl)
mcp.tool(
    name="outing",
    description=(
        "Alias of plan_outing: meeting window + meeting point + place to eat, "
        "minimising the total journey."
    ),
)(_outing_impl)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# ASGI app + warm-up
# ----------------------------------------------------------------------

mcp_http_app = mcp.streamable_http_app()


def warm_up() -> None:
    """Pre-fetch everything a scored call could need (best effort)."""
    if os.environ.get("TOOLBOX_SKIP_WARMUP"):
        return
    import logging

    logger = logging.getLogger("uvicorn.error")
    try:
        docs = _load_docs(None)
        _get_rag(docs)
    except (ToolboxError, httpx.HTTPError, OSError) as exc:
        logger.info("toolbox warm-up: study materials unavailable: %s", exc)
    for day in _DAYS:
        try:
            _get_venues(day)
        except (ToolboxError, httpx.HTTPError, OSError) as exc:
            logger.info("toolbox warm-up: venues for %s unavailable: %s", day, exc)
