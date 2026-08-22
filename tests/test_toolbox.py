"""Unit and integration coverage for the Tool-box Phase 2 engines."""

import json

import pytest

from app import toolbox
from app.toolbox import (
    RECALL_TOKEN_BUDGET,
    ToolboxError,
    build_graph_url,
    count_tokens,
    extract_text,
    fetch_study_materials,
    least_cost_path,
    navigate,
    parse_graph,
    recall,
    split_passages,
    total_tokens,
)

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def test_count_tokens_uses_o200k_base():
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    assert count_tokens("hello world") == len(encoding.encode("hello world"))
    assert count_tokens("") == 0


def test_total_tokens_sums_chunks():
    chunks = ["hello", "world"]
    assert total_tokens(chunks) == count_tokens("hello") + count_tokens("world")


# ---------------------------------------------------------------------------
# Text extraction and passage splitting
# ---------------------------------------------------------------------------


def test_extract_text_strips_html():
    html = "<html><body><p>The sensor grid was aligned on 14 March.</p><script>bad()</script></body></html>"
    text = extract_text(html, "text/html")
    assert "14 March" in text
    assert "bad()" not in text


def test_extract_text_flattens_json():
    payload = {"facts": [{"title": "alignment", "answer": "14 March"}]}
    text = extract_text(json.dumps(payload), "application/json")
    assert "alignment" in text
    assert "14 March" in text


def test_split_passages_keeps_sentences_together():
    text = "First sentence here. Second sentence here. Third sentence here."
    passages = split_passages(text, target_tokens=50)
    assert passages
    for passage in passages:
        assert count_tokens(passage) <= 100


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


def test_recall_returns_small_corpus_verbatim():
    materials = "The sensor grid was last brought back into alignment on 14 March."
    question = "When was the sensor grid last brought back into alignment?"
    chunks = recall(question, materials)
    assert isinstance(chunks, list)
    assert chunks
    assert total_tokens(chunks) <= RECALL_TOKEN_BUDGET
    assert any("14 March" in chunk for chunk in chunks)


def test_recall_respects_token_budget_on_large_corpus():
    fact = "The sensor grid was last brought back into alignment on 14 March 2026."
    filler = "The android keeps a daily log of weather readings across the campus garden."
    materials = [fact] + [f"{filler} Entry {i}." for i in range(200)]
    question = "When was the sensor grid last brought back into alignment?"
    chunks = recall(question, materials)
    assert isinstance(chunks, list)
    assert chunks
    assert total_tokens(chunks) <= RECALL_TOKEN_BUDGET
    assert any("14 March" in chunk for chunk in chunks)


def test_recall_accepts_json_materials_string():
    materials = json.dumps(
        [{"title": "Log", "text": "The reactor coolant was flushed on 3 June."}]
    )
    chunks = recall("When was the reactor coolant flushed?", materials)
    assert any("3 June" in chunk for chunk in chunks)


def test_recall_without_materials_raises():
    with pytest.raises(ToolboxError):
        recall("A question with no materials attached?", None)


def test_fetch_study_materials_follows_index_links(monkeypatch):
    pages = {
        "https://school.example/index": (
            '<a href="https://school.example/doc1">Doc 1</a> <a href="https://school.example/doc2">Doc 2</a>',
            "text/html",
        ),
        "https://school.example/doc1": ("The library opens at 8 am.", "text/plain"),
        "https://school.example/doc2": ("The canteen opens at 7 am.", "text/plain"),
    }

    monkeypatch.setattr(toolbox, "_fetch_many", lambda urls: {url: pages[url] for url in urls})
    docs = fetch_study_materials("https://school.example/index")
    texts = {doc["text"] for doc in docs}
    assert any("8 am" in text for text in texts)
    assert any("7 am" in text for text in texts)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

EXAMPLE_GRAPH = {
    "adjacency": {"A": {"B": 4.0, "C": 2.0}, "B": {"D": 3.0}, "C": {"D": 2.0}},
    "tolls": {"A": 5.0, "B": 1.0, "C": 9.0, "D": 2.0},
}


def test_parse_graph_ok():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    assert adjacency["A"]["B"] == 4.0
    assert tolls["D"] == 2.0


def test_parse_graph_requires_adjacency():
    with pytest.raises(ToolboxError):
        parse_graph({"tolls": {}})


def test_least_cost_path_uses_edge_weights_and_entry_tolls():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    path, cost = least_cost_path(adjacency, tolls, "A", "D")
    assert path == ["A", "B", "D"]
    assert cost == 10.0


def test_least_cost_path_respects_hop_allowance():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    with pytest.raises(ToolboxError):
        least_cost_path(adjacency, tolls, "A", "D", max_edges=1)
    path, cost = least_cost_path(adjacency, tolls, "A", "D", max_edges=2)
    assert path == ["A", "B", "D"]
    assert cost == 10.0


def test_least_cost_path_prefers_longer_cheaper_route_when_allowed():
    adjacency = {"S": {"A": 100.0, "B": 1.0}, "B": {"A": 1.0}}
    tolls = {"S": 0.0, "A": 0.0, "B": 0.0}
    path, cost = least_cost_path(adjacency, tolls, "S", "A", max_edges=1)
    assert path == ["S", "A"]
    assert cost == 100.0
    path, cost = least_cost_path(adjacency, tolls, "S", "A", max_edges=2)
    assert path == ["S", "B", "A"]
    assert cost == 2.0


def test_least_cost_path_avoids_visited_nodes():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    path, cost = least_cost_path(adjacency, tolls, "A", "D", visited=["A", "B"])
    assert path == ["A", "C", "D"]
    assert cost == 15.0


def test_least_cost_path_rejects_visited_destination():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    with pytest.raises(ToolboxError):
        least_cost_path(adjacency, tolls, "A", "D", visited=["D"])


def test_least_cost_path_same_node_returns_start():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    path, cost = least_cost_path(adjacency, tolls, "A", "A")
    assert path == ["A"]
    assert cost == 0.0


def test_least_cost_path_no_hops_left_raises():
    adjacency, tolls = parse_graph(EXAMPLE_GRAPH)
    with pytest.raises(ToolboxError):
        least_cost_path(adjacency, tolls, "A", "D", max_edges=0)


def test_build_graph_url():
    assert build_graph_url("abc-123", "https://maps.example") == (
        "https://maps.example/graph?map_id=abc-123"
    )
    assert build_graph_url("https://maps.example/graph?map_id=abc-123") == (
        "https://maps.example/graph?map_id=abc-123"
    )
    with pytest.raises(ToolboxError):
        build_graph_url("abc-123")


def test_navigate_with_graph_returns_next_node():
    next_node, path, cost = navigate(
        map_id="unused", current="A", destination="D", graph=EXAMPLE_GRAPH
    )
    assert next_node == "B"
    assert path == ["A", "B", "D"]
    assert cost == 10.0


def test_navigate_fetches_graph_from_base_url(monkeypatch):
    def fake_fetch(url, timeout):
        assert url == "https://maps.example/graph?map_id=abc-123"
        return json.dumps(EXAMPLE_GRAPH), "application/json"

    monkeypatch.setattr(toolbox, "_fetch_text", fake_fetch)
    next_node, _, _ = navigate(
        map_id="abc-123",
        current="A",
        destination="D",
        base_url="https://maps.example",
    )
    assert next_node == "B"
